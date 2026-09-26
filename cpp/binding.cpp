// Copyright 2026 Christopher Barber
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <chrono>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <nanobind/nanobind.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>
#include <nanobind/trampoline.h>

#include "ANTLRInputStream.h"
#include "BaseErrorListener.h"
#include "CommonTokenStream.h"
#include "LexerInterpreter.h"
#include "ParserInterpreter.h"
#include "ParserRuleContext.h"
#include "Recognizer.h"
#include "RuleContext.h"
#include "Token.h"
#include "UnbufferedTokenStream.h"
#include "Vocabulary.h"
#include "atn/ATN.h"
#include "atn/ATNDeserializer.h"
#include "atn/ATNType.h"
#include "atn/RuleStartState.h"
#include "atn/SerializedATNView.h"
#include "misc/Interval.h"
#include "misc/IntervalSet.h"
#include "tree/ErrorNode.h"
#include "tree/ParseTree.h"
#include "tree/ParseTreeListener.h"
#include "tree/ParseTreeWalker.h"
#include "tree/TerminalNode.h"

#include "events.h"
#include "file_char_stream.h"

namespace nb = nanobind;
using namespace antlr4;
using namespace antlrope_events;

// ---------------------------------------------------------------------------
// ATN shape report (kept as a check that a serialized ATN transfers and
// loads).
// ---------------------------------------------------------------------------
struct AtnShape {
    int grammar_type;
    size_t num_states;
    size_t num_decisions;
    size_t num_rules;
    size_t max_token_type;
};

// Deserialize a serialized ATN and report its dimensions, verifying the
// transfer without keeping the result.
static AtnShape atn_shape(const std::vector<int32_t> &serialized) {
    atn::ATNDeserializer deserializer;
    std::unique_ptr<atn::ATN> atn =
        deserializer.deserialize(atn::SerializedATNView(serialized));
    AtnShape s;
    s.grammar_type = static_cast<int>(atn->grammarType);
    s.num_states = atn->states.size();
    s.num_decisions = atn->decisionToState.size();
    s.num_rules = atn->ruleToStartState.size();
    s.max_token_type = atn->maxTokenType;
    return s;
}

// ---------------------------------------------------------------------------
// Grammar-agnostic parse pipeline driven by the serialized ATN and name lists
// the stock Python3-target ANTLR tool already emits. Specs own the deserialized
// ATN; interpreters hold references into it and are created per parse call.
// ---------------------------------------------------------------------------
struct LexerSpec {
    std::string grammar_file_name;
    dfa::Vocabulary vocabulary;
    std::vector<std::string> rule_names;
    std::vector<std::string> channel_names;
    std::vector<std::string> mode_names;
    std::unique_ptr<atn::ATN> atn;

    // Takes ownership of the name lists and deserializes the serialized ATN
    // once; every parse then runs interpreters over this spec.
    LexerSpec(std::string grammar_file_name,
              std::vector<std::string> literal_names,
              std::vector<std::string> symbolic_names,
              std::vector<std::string> rule_names,
              std::vector<std::string> channel_names,
              std::vector<std::string> mode_names,
              const std::vector<int32_t> &serialized)
        : grammar_file_name(std::move(grammar_file_name)),
          vocabulary(std::move(literal_names), std::move(symbolic_names)),
          rule_names(std::move(rule_names)),
          channel_names(std::move(channel_names)),
          mode_names(std::move(mode_names)) {
        atn::ATNDeserializer deserializer;
        atn = deserializer.deserialize(atn::SerializedATNView(serialized));
    }
};

struct ParserSpec {
    std::string grammar_file_name;
    dfa::Vocabulary vocabulary;
    std::vector<std::string> rule_names;
    std::unique_ptr<atn::ATN> atn;

    // Takes ownership of the name lists and deserializes the serialized ATN
    // once; every parse then runs interpreters over this spec.
    ParserSpec(std::string grammar_file_name,
               std::vector<std::string> literal_names,
               std::vector<std::string> symbolic_names,
               std::vector<std::string> rule_names,
               const std::vector<int32_t> &serialized)
        : grammar_file_name(std::move(grammar_file_name)),
          vocabulary(std::move(literal_names), std::move(symbolic_names)),
          rule_names(std::move(rule_names)) {
        atn::ATNDeserializer deserializer;
        atn = deserializer.deserialize(atn::SerializedATNView(serialized));
    }
};

// Native baseline listener: counts events without crossing into Python. The
// accumulation keeps the optimizer from eliding the walk.
struct CountingListener : public tree::ParseTreeListener {
    size_t terminals = 0;
    size_t errors = 0;
    size_t enters = 0;
    size_t exits = 0;
    size_t text_bytes = 0;

    void visitTerminal(tree::TerminalNode *node) override {
        terminals++;
        text_bytes += node->getSymbol()->getText().size();
    }
    void visitErrorNode(tree::ErrorNode * /*node*/) override { errors++; }
    void enterEveryRule(ParserRuleContext *ctx) override {
        enters++;
        text_bytes += ctx->getRuleIndex();
    }
    void exitEveryRule(ParserRuleContext * /*ctx*/) override { exits++; }
};

// A collected parse diagnostic. `start`/`stop` are the codepoint span of the
// offending token (matching the event-stream offsets), or -1 when there is no
// token (e.g. a lexer error). `line` is 1-based, `column` 0-based.
struct SyntaxError {
    size_t line;
    size_t column;
    int32_t start;
    int32_t stop;
    std::string message;
};

// Replaces ANTLR's default ConsoleErrorListener (which writes to stderr). It
// captures each syntaxError into a structured list the caller hands to Python,
// so the consumer, not the library, decides how parse errors are reported.
class CollectingErrorListener : public BaseErrorListener {
public:
    std::vector<SyntaxError> errors;

    // ANTLR error-listener hook: record the diagnostic (with the offending
    // token's span when there is one) instead of printing it.
    void syntaxError(Recognizer * /*recognizer*/,
                     Token *offendingSymbol,
                     size_t line,
                     size_t charPositionInLine,
                     const std::string &msg,
                     std::exception_ptr /*e*/) override {
        int32_t start = -1;
        int32_t stop = -1;
        if (offendingSymbol != nullptr) {
            start = static_cast<int32_t>(offendingSymbol->getStartIndex());
            stop = static_cast<int32_t>(offendingSymbol->getStopIndex());
        }
        errors.push_back({line, charPositionInLine, start, stop, msg});
    }
};

// Run the lexer and parser to a full tree. Returns the root; tree memory is
// owned by the ParserInterpreter, so the caller must keep both alive while
// walking. The default console error listeners are removed so parsing never
// writes to stderr; pass `err_listener` to collect diagnostics instead.
static ParserRuleContext *run_parse(ParserSpec &pspec,
                                    LexerSpec &lspec,
                                    ANTLRInputStream &input,
                                    LexerInterpreter &lexer,
                                    CommonTokenStream &tokens,
                                    ParserInterpreter &parser,
                                    size_t start_rule,
                                    BaseErrorListener *err_listener = nullptr) {
    (void)input;
    (void)lspec;
    (void)pspec;
    lexer.removeErrorListeners();
    parser.removeErrorListeners();
    if (err_listener != nullptr) {
        lexer.addErrorListener(err_listener);
        parser.addErrorListener(err_listener);
    }
    tokens.fill();
    return parser.parse(start_rule);
}

// Diagnostic: measure the pure native cost by parsing and walking with a C++
// listener.
static nb::dict parse_count(ParserSpec &pspec,
                            LexerSpec &lspec,
                            const std::string &text,
                            size_t start_rule) {
    CountingListener listener;
    size_t num_tokens;
    {
        // Pure native: the counting listener never crosses into Python, so the
        // whole parse and walk runs without the GIL.
        nb::gil_scoped_release release;

        ANTLRInputStream input(text);
        LexerInterpreter lexer(lspec.grammar_file_name,
                               lspec.vocabulary,
                               lspec.rule_names,
                               lspec.channel_names,
                               lspec.mode_names,
                               *lspec.atn,
                               &input);
        CommonTokenStream tokens(&lexer);
        ParserInterpreter parser(pspec.grammar_file_name,
                                 pspec.vocabulary,
                                 pspec.rule_names,
                                 *pspec.atn,
                                 &tokens);
        ParserRuleContext *tree =
            run_parse(pspec, lspec, input, lexer, tokens, parser, start_rule);

        tree::ParseTreeWalker::DEFAULT.walk(&listener, tree);
        num_tokens = tokens.size();
    }

    nb::dict d;
    d["terminals"] = listener.terminals;
    d["errors"] = listener.errors;
    d["enters"] = listener.enters;
    d["exits"] = listener.exits;
    d["num_tokens"] = num_tokens;
    return d;
}

// Diagnostic fallback: parse and walk, calling into a Python
// ParseTreeListener subclass (the slow per-node FFI path).
static void parse_walk(ParserSpec &pspec,
                       LexerSpec &lspec,
                       const std::string &text,
                       size_t start_rule,
                       tree::ParseTreeListener *listener) {
    ANTLRInputStream input(text);
    LexerInterpreter lexer(lspec.grammar_file_name,
                           lspec.vocabulary,
                           lspec.rule_names,
                           lspec.channel_names,
                           lspec.mode_names,
                           *lspec.atn,
                           &input);
    CommonTokenStream tokens(&lexer);
    ParserInterpreter parser(pspec.grammar_file_name,
                             pspec.vocabulary,
                             pspec.rule_names,
                             *pspec.atn,
                             &tokens);
    ParserRuleContext *tree;
    {
        // Release the GIL for the native parse; the walk below re-acquires it
        // because it dispatches into the Python ParseTreeListener per node.
        // (input/lexer/tokens/parser stay alive in this scope for the walk.)
        nb::gil_scoped_release release;
        tree =
            run_parse(pspec, lspec, input, lexer, tokens, parser, start_rule);
    }
    tree::ParseTreeWalker::DEFAULT.walk(listener, tree);
}

// ---------------------------------------------------------------------------
// Bulk event stream: a native iterative DFS over the finished parse tree emits
// a flat (kind, payload, start, stop) int32 record per visited item into one
// contiguous buffer, handed to Python in a single transfer. Optional rule and
// token masks let the consumer drop unwanted events in C++, which reduces the
// number of records Python must iterate. One crossing replaces a per-node FFI
// callback for every tree node. The DFS lives in events.h.
//
// Returned as a flat little-endian int32 buffer of 4*N values (N event rows of
// (kind, payload, start, stop)). Python decodes it with
// struct.iter_unpack("<4i", ...), with no numpy dependency.
//
// NOTE: internal, but the srdl-bench benchmark (github.com/zuzukin/srdl-bench)
// calls this directly (via antlrope._native) to time the raw native stage;
// update that repo if the signature ever changes.
static nb::object parse_events(ParserSpec &pspec,
                               LexerSpec &lspec,
                               const std::string &text,
                               size_t start_rule,
                               std::optional<std::vector<int32_t>> rule_mask,
                               std::optional<std::vector<int32_t>> token_mask) {
    std::vector<int32_t> buf;
    CollectingErrorListener err_listener;
    {
        // Everything in this block is pure C++ (nanobind converted the
        // arguments before entry), so release the GIL: other Python threads
        // keep running, and N threads can parse N documents in parallel.
        nb::gil_scoped_release release;

        ANTLRInputStream input(text);
        LexerInterpreter lexer(lspec.grammar_file_name,
                               lspec.vocabulary,
                               lspec.rule_names,
                               lspec.channel_names,
                               lspec.mode_names,
                               *lspec.atn,
                               &input);
        CommonTokenStream tokens(&lexer);
        ParserInterpreter parser(pspec.grammar_file_name,
                                 pspec.vocabulary,
                                 pspec.rule_names,
                                 *pspec.atn,
                                 &tokens);
        ParserRuleContext *tree = run_parse(pspec,
                                            lspec,
                                            input,
                                            lexer,
                                            tokens,
                                            parser,
                                            start_rule,
                                            &err_listener);

        size_t n_rules = pspec.atn->ruleToStartState.size();
        size_t n_toks = pspec.atn->maxTokenType + 1;
        std::vector<char> rule_keep =
            rule_mask ? make_mask(rule_mask, n_rules) : std::vector<char>();
        std::vector<char> tok_keep =
            token_mask ? make_mask(token_mask, n_toks) : std::vector<char>();

        buf.reserve(1u << 20);
        collect_events(tree,
                       buf,
                       rule_mask ? rule_keep.data() : nullptr,
                       n_rules,
                       token_mask ? tok_keep.data() : nullptr,
                       n_toks);
    }

    nb::bytes events(reinterpret_cast<const char *>(buf.data()),
                     buf.size() * sizeof(int32_t));
    return nb::make_tuple(std::move(events), std::move(err_listener.errors));
}

// Diagnostic: time each stage of the native pipeline separately so the parse
// time can be broken down into input UTF-32 decoding, lexing and token fill,
// parsing to a tree, and the DFS walk. Returns seconds per stage plus token and
// event counts.
static nb::dict parse_stage_times(ParserSpec &pspec,
                                  LexerSpec &lspec,
                                  const std::string &text,
                                  size_t start_rule) {
    using clk = std::chrono::steady_clock;
    auto secs = [](clk::time_point a, clk::time_point b) {
        return std::chrono::duration<double>(b - a).count();
    };

    auto t0 = clk::now();
    ANTLRInputStream input(text);
    auto t1 = clk::now();

    LexerInterpreter lexer(lspec.grammar_file_name,
                           lspec.vocabulary,
                           lspec.rule_names,
                           lspec.channel_names,
                           lspec.mode_names,
                           *lspec.atn,
                           &input);
    lexer.removeErrorListeners();
    CommonTokenStream tokens(&lexer);
    tokens.fill();
    auto t2 = clk::now();

    ParserInterpreter parser(pspec.grammar_file_name,
                             pspec.vocabulary,
                             pspec.rule_names,
                             *pspec.atn,
                             &tokens);
    parser.removeErrorListeners();
    ParserRuleContext *tree = parser.parse(start_rule);
    auto t3 = clk::now();

    size_t n_rules = pspec.atn->ruleToStartState.size();
    size_t n_toks = pspec.atn->maxTokenType + 1;
    std::vector<int32_t> buf;
    buf.reserve(1u << 20);
    collect_events(tree, buf, nullptr, n_rules, nullptr, n_toks);
    auto t4 = clk::now();

    nb::dict d;
    d["input_decode"] = secs(t0, t1);
    d["lex_fill"] = secs(t1, t2);
    d["parse_tree"] = secs(t2, t3);
    d["walk"] = secs(t3, t4);
    d["num_tokens"] = tokens.size();
    d["num_events"] = buf.size() / 4;
    d["input_codepoints"] = input.size();
    return d;
}

// ---------------------------------------------------------------------------
// Lexer-only pass: run just the LexerInterpreter, pulling tokens one at a time
// with no parser (the cheap stage), and return one record per token, (type,
// channel, start, stop), as a flat int32 buffer. Used to split input into
// chunks for walk_parallel without a full parse. `start` and `stop` are
// codepoint offsets that follow the same convention as the event stream and
// SourceMap (line and column are left to the caller's SourceMap). The EOF token
// is omitted. An optional token_mask (list of token types to keep) drops the
// rest in C++. The chunkers ask only for their boundary tokens, so little data
// crosses into Python.
// ---------------------------------------------------------------------------
static nb::object lex(LexerSpec &lspec,
                      const std::string &text,
                      std::optional<std::vector<int32_t>> token_mask) {
    std::vector<int32_t> buf;
    CollectingErrorListener err_listener;
    {
        // Pure C++ (arguments already converted): release the GIL so lexing of
        // independent inputs can overlap, like the parse functions.
        nb::gil_scoped_release release;

        ANTLRInputStream input(text);
        LexerInterpreter lexer(lspec.grammar_file_name,
                               lspec.vocabulary,
                               lspec.rule_names,
                               lspec.channel_names,
                               lspec.mode_names,
                               *lspec.atn,
                               &input);
        lexer.removeErrorListeners();
        lexer.addErrorListener(&err_listener);

        size_t n_toks = lspec.atn->maxTokenType + 1;
        std::vector<char> keep =
            token_mask ? make_mask(token_mask, n_toks) : std::vector<char>();
        const char *keepp = token_mask ? keep.data() : nullptr;

        // Pull tokens one at a time and keep only the requested types, so the
        // whole token stream is never buffered (unlike CommonTokenStream.fill):
        // each token is freed as the loop advances. EOF is not emitted.
        std::unique_ptr<Token> tok;
        while ((tok = lexer.nextToken())->getType() != Token::EOF) {
            size_t type = tok->getType();
            if (keepp != nullptr && (type >= n_toks || keepp[type] == 0)) {
                continue;
            }
            buf.push_back(static_cast<int32_t>(type));
            buf.push_back(static_cast<int32_t>(tok->getChannel()));
            buf.push_back(static_cast<int32_t>(tok->getStartIndex()));
            buf.push_back(static_cast<int32_t>(tok->getStopIndex()));
        }
    }

    nb::bytes toks(reinterpret_cast<const char *>(buf.data()),
                   buf.size() * sizeof(int32_t));
    return nb::make_tuple(std::move(toks), std::move(err_listener.errors));
}

// ---------------------------------------------------------------------------
// Rule-span pass: parse (entirely in C++, GIL released) and return the
// (rule_index, start, stop) character span of each parse-tree node whose rule
// is kept by rule_mask (nullptr = all). With outermost=true a matched rule's
// subtree is skipped, so only top-level occurrences are returned. This is the
// building block for rule-based chunking: the first, structural parse stays in
// C++ and only the spans cross into Python. Empty rules with no consumed token
// yield -1.
// ---------------------------------------------------------------------------
static nb::object rule_spans(ParserSpec &pspec,
                             LexerSpec &lspec,
                             const std::string &text,
                             size_t start_rule,
                             std::optional<std::vector<int32_t>> rule_mask,
                             bool outermost) {
    std::vector<int32_t> buf;
    CollectingErrorListener err_listener;
    {
        // Pure C++ parse and tree walk: release the GIL so independent inputs
        // can be scanned for rule spans in parallel.
        nb::gil_scoped_release release;

        ANTLRInputStream input(text);
        LexerInterpreter lexer(lspec.grammar_file_name,
                               lspec.vocabulary,
                               lspec.rule_names,
                               lspec.channel_names,
                               lspec.mode_names,
                               *lspec.atn,
                               &input);
        CommonTokenStream tokens(&lexer);
        ParserInterpreter parser(pspec.grammar_file_name,
                                 pspec.vocabulary,
                                 pspec.rule_names,
                                 *pspec.atn,
                                 &tokens);
        ParserRuleContext *tree = run_parse(pspec,
                                            lspec,
                                            input,
                                            lexer,
                                            tokens,
                                            parser,
                                            start_rule,
                                            &err_listener);

        size_t n_rules = pspec.atn->ruleToStartState.size();
        std::vector<char> rule_keep =
            rule_mask ? make_mask(rule_mask, n_rules) : std::vector<char>();
        collect_rule_spans(tree,
                           buf,
                           rule_mask ? rule_keep.data() : nullptr,
                           n_rules,
                           outermost);
    }

    nb::bytes spans(reinterpret_cast<const char *>(buf.data()),
                    buf.size() * sizeof(int32_t));
    return nb::make_tuple(std::move(spans), std::move(err_listener.errors));
}

// ---------------------------------------------------------------------------
// Streaming token chunker: the streaming counterpart of split_on_token. Opens a
// UTF-8 file itself and lexes it incrementally over a sliding window
// (Utf8FileCharStream), never holding the whole input. On reaching each
// delimiter token it slices that chunk's text out of the window and frees it,
// so peak memory is about one chunk. next_batch(n) pulls up to n positioned
// chunks; the object is stateful and reused across batches. Only the requested
// delimiter types start or end chunks (where = before or after), like
// split_on_token. Unless trim is false, whitespace is trimmed and
// whitespace-only regions are skipped. Line and column are tracked
// incrementally, with no whole-input SourceMap.
// ---------------------------------------------------------------------------
// One positioned chunk record as handed to Python: the chunk's absolute
// codepoint offset, 1-based line, 0-based column, and UTF-8 text.
struct ChunkRec {
    size_t offset;
    size_t line;
    size_t column;
    std::string text;
};

struct StreamChunker {
    std::unique_ptr<antlrope::Utf8FileCharStream> stream;
    std::unique_ptr<LexerInterpreter> lexer;
    CollectingErrorListener err;
    std::vector<char> keep;  // keep[type] != 0 => `type` is a delimiter
    size_t n_toks;
    int where;  // 0 = before (chunk begins at a delimiter), 1 = after (ends at
                // one)
    bool have_channel;
    int channel;
    bool trim;  // strip surrounding whitespace from each emitted chunk

    size_t chunk_start =
        0;  // absolute codepoint index the current chunk begins at
    size_t origin_idx = 0;   // absolute index for which (origin_line/col) hold
    size_t origin_line = 1;  // 1-based, like SourceMap
    size_t origin_col = 0;   // 0-based
    bool done = false;

    // Opens `path` and builds the lexer over the streaming char source.
    // `delim_types` are the token types that bound chunks; `where_` is
    // 0 = before / 1 = after; `channel_` restricts which delimiters count
    // (nullopt = any); `block` is the read size (0 = default, a test hook).
    StreamChunker(LexerSpec &spec,
                  const std::string &path,
                  std::vector<int32_t> delim_types,
                  int where_,
                  std::optional<int> channel_,
                  bool lenient,
                  bool trim_,
                  size_t block)
        : where(where_), have_channel(channel_.has_value()),
          channel(channel_.value_or(0)), trim(trim_) {
        stream = std::make_unique<antlrope::Utf8FileCharStream>(
            path, lenient, block);
        // The interpreter holds references into the spec (ATN and name lists),
        // so the spec must outlive this object (see keep_alive on the binding).
        lexer = std::make_unique<LexerInterpreter>(spec.grammar_file_name,
                                                   spec.vocabulary,
                                                   spec.rule_names,
                                                   spec.channel_names,
                                                   spec.mode_names,
                                                   *spec.atn,
                                                   stream.get());
        lexer->removeErrorListeners();
        lexer->addErrorListener(&err);
        n_toks = spec.atn->maxTokenType + 1;
        keep = make_mask(
            std::optional<std::vector<int32_t>>(std::move(delim_types)),
            n_toks);
    }

    // ASCII whitespace test used for chunk trimming (matches Python str.strip
    // for the whitespace that grammars conventionally skip).
    static bool is_ws(char32_t c) {
        return c == U' ' || c == U'\t' || c == U'\n' || c == U'\r' ||
               c == U'\f' || c == U'\v';
    }

    // Advance (origin_line, origin_col) to absolute index `to`, counting
    // newlines over the retained window. Matches SourceMap: line bumps on '\n',
    // column resets after it. Precondition: origin_idx is within the window and
    // <= `to`.
    void advance_origin(size_t to) {
        const std::u32string &buf = stream->buffer();
        size_t bs = stream->bufStart();
        for (size_t i = origin_idx - bs, end = to - bs; i < end; ++i) {
            if (buf[i] == U'\n') {
                origin_line++;
                origin_col = 0;
            } else {
                origin_col++;
            }
        }
        origin_idx = to;
    }

    // Emit the region [chunk_start, b): trim surrounding whitespace, push a
    // positioned record (skipping a whitespace-only/empty region), and leave
    // the origin at b. The window is dropped by the caller at a real boundary.
    void emit_region(size_t b, std::vector<ChunkRec> &out) {
        const std::u32string &buf = stream->buffer();
        size_t bs = stream->bufStart();
        size_t la = chunk_start - bs;
        size_t lb = b - bs;
        size_t fa = la;
        size_t lb2 = lb;
        if (trim) {
            while (fa < lb && is_ws(buf[fa])) {
                fa++;
            }
            while (lb2 > fa && is_ws(buf[lb2 - 1])) {
                lb2--;
            }
        }
        if (fa >= lb2) {  // empty (or whitespace-only when trimming): skip
            advance_origin(b);
            return;
        }
        size_t offset = bs + fa;
        advance_origin(offset);
        ChunkRec rec;
        rec.offset = offset;
        rec.line = origin_line;
        rec.column = origin_col;
        rec.text = stream->getText(antlr4::misc::Interval(
            static_cast<ssize_t>(offset), static_cast<ssize_t>(bs + lb2 - 1)));
        out.push_back(std::move(rec));
        advance_origin(b);
    }

    // Lex until `n` chunks are produced or EOF. Returns whether more may remain
    // (false once EOF's final region has been emitted).
    bool run_batch(size_t n, std::vector<ChunkRec> &out) {
        if (done) {
            return false;
        }
        while (out.size() < n) {
            std::unique_ptr<Token> tok = lexer->nextToken();
            size_t type = tok->getType();
            if (type == Token::EOF) {
                emit_region(stream->index(), out);
                done = true;
                return false;
            }
            if (type >= n_toks || keep[type] == 0) {
                continue;  // not a delimiter
            }
            if (have_channel &&
                static_cast<int>(tok->getChannel()) != channel) {
                continue;
            }
            size_t b;
            if (where == 0) {  // before
                size_t ds = tok->getStartIndex();
                if (ds <= chunk_start) {
                    continue;  // the delimiter that opens the current chunk
                }
                b = ds;
            } else {  // after
                b = tok->getStopIndex() + 1;
            }
            emit_region(b, out);
            stream->dropThrough(b);
            chunk_start = b;
        }
        return true;
    }

    // Python-facing batch pull: run_batch under a released GIL, then build the
    // ((offset, line, column, text) rows, more) result tuple.
    nb::object next_batch(size_t n) {
        std::vector<ChunkRec> recs;
        bool more;
        {
            // Pure C++ (file IO, decoding, lexing, getText): release the GIL
            // for the whole batch like lex(); only building the result list
            // below needs it.
            nb::gil_scoped_release release;
            more = run_batch(n, recs);
        }
        nb::list rows;
        for (ChunkRec &r : recs) {
            rows.append(nb::make_tuple(r.offset,
                                       r.line,
                                       r.column,
                                       nb::str(r.text.data(), r.text.size())));
        }
        return nb::make_tuple(std::move(rows), more);
    }
};

// ---------------------------------------------------------------------------
// Streaming rule chunker: the streaming counterpart of chunk_by_rule. Treats
// the input as a sequence of top-level occurrences of one or more parser rules
// and yields each, one at a time, over a bounded-memory pipeline
// (Utf8FileCharStream -> LexerInterpreter -> UnbufferedTokenStream ->
// ParserInterpreter). At each position it uses the next token and each
// candidate rule's FIRST set to choose which rule to parse. It parses exactly
// that one record, captures its span, and then frees the record's tokens
// (token-stream mark/release), parse tree (tree-tracker reset), and characters
// (char-window dropThrough). Unlike chunk_by_rule, it does not search a full
// parse for the rule. Records must form a directly adjacent top-level sequence,
// with only lexer-skipped whitespace or comments between them. next_batch(n)
// pulls up to n positioned records.
// ---------------------------------------------------------------------------
struct StreamRuleChunker {
    CollectingErrorListener err;
    std::unique_ptr<antlrope::Utf8FileCharStream> stream;
    std::unique_ptr<LexerInterpreter> lexer;
    std::unique_ptr<UnbufferedTokenStream> tokens;
    std::unique_ptr<ParserInterpreter> parser;
    // (rule index, FIRST set) candidates, tried in the given order so the first
    // whose FIRST set contains the next token is chosen.
    std::vector<std::pair<size_t, antlr4::misc::IntervalSet>> candidates;
    // Borrowed from the parser spec (kept alive via keep_alive) for the token
    // display names and candidate rule names in the loud-failure messages.
    const dfa::Vocabulary *vocab = nullptr;
    const std::vector<std::string> *prule_names = nullptr;

    size_t origin_idx = 0;   // absolute index for which (origin_line/col) hold
    size_t origin_line = 1;  // 1-based, like SourceMap
    size_t origin_col = 0;   // 0-based
    bool done = false;
    // When a malformed record is hit mid-batch we flush the records gathered so
    // far (none lost) and stash the diagnostic here; the next call throws it
    // without re-parsing (re-parsing could skip past a record the failed parse
    // consumed).
    std::string pending_error;

    // Opens `path`, chains the bounded pipeline (char stream -> lexer ->
    // unbuffered token stream -> parser), and precomputes each candidate
    // record rule's FIRST set for the per-record dispatch. `block` is the
    // read size (0 = default, a test hook).
    StreamRuleChunker(ParserSpec &pspec,
                      LexerSpec &lspec,
                      const std::string &path,
                      std::vector<int32_t> rule_indices,
                      bool lenient,
                      size_t block) {
        vocab = &pspec.vocabulary;
        prule_names = &pspec.rule_names;
        stream = std::make_unique<antlrope::Utf8FileCharStream>(
            path, lenient, block);
        // The interpreters hold references into the specs (ATN and name lists),
        // so both specs must outlive this object (see keep_alive on the
        // binding).
        lexer = std::make_unique<LexerInterpreter>(lspec.grammar_file_name,
                                                   lspec.vocabulary,
                                                   lspec.rule_names,
                                                   lspec.channel_names,
                                                   lspec.mode_names,
                                                   *lspec.atn,
                                                   stream.get());
        tokens = std::make_unique<UnbufferedTokenStream>(lexer.get());
        parser = std::make_unique<ParserInterpreter>(pspec.grammar_file_name,
                                                     pspec.vocabulary,
                                                     pspec.rule_names,
                                                     *pspec.atn,
                                                     tokens.get());
        lexer->removeErrorListeners();
        lexer->addErrorListener(&err);
        parser->removeErrorListeners();
        parser->addErrorListener(&err);
        for (int32_t ridx : rule_indices) {
            atn::RuleStartState *rs =
                pspec.atn->ruleToStartState[static_cast<size_t>(ridx)];
            candidates.emplace_back(static_cast<size_t>(ridx),
                                    pspec.atn->nextTokens(rs));
        }
    }

    // Advance (origin_line, origin_col) to absolute index `to` over the
    // retained window (same incremental SourceMap accounting as StreamChunker).
    void advance_origin(size_t to) {
        const std::u32string &buf = stream->buffer();
        size_t bs = stream->bufStart();
        for (size_t i = origin_idx - bs, end = to - bs; i < end; ++i) {
            if (buf[i] == U'\n') {
                origin_line++;
                origin_col = 0;
            } else {
                origin_col++;
            }
        }
        origin_idx = to;
    }

    // Comma-separated candidate rule names, for diagnostics.
    std::string candidate_names() const {
        std::string s;
        for (size_t i = 0; i < candidates.size(); ++i) {
            if (i) {
                s += ", ";
            }
            size_t r = candidates[i].first;
            s += (prule_names && r < prule_names->size()) ? (*prule_names)[r]
                                                          : std::to_string(r);
        }
        return s;
    }

    // "<NAME> \"<text>\" at line L:C" for the offending token. The text is read
    // from the retained window via our own char stream, not Token::getText(),
    // which calls CharStream::size() to bounds-check and so throws on an
    // unbuffered source. It is capped at a UTF-8 codepoint boundary to stay
    // valid UTF-8, and omitted if the token's characters are no longer
    // buffered.
    std::string token_desc(Token *la, ssize_t ttype) const {
        std::string name =
            vocab ? vocab->getDisplayName(static_cast<size_t>(ttype))
                  : std::to_string(ttype);
        std::string text;
        try {
            text = stream->getText(antlr4::misc::Interval(
                static_cast<ssize_t>(la->getStartIndex()),
                static_cast<ssize_t>(la->getStopIndex())));
        } catch (const std::exception &) {
            text.clear();  // characters already dropped from the window
        }
        if (text.size() > 48) {
            size_t cut = 48;
            while (cut > 0 &&
                   (static_cast<unsigned char>(text[cut]) & 0xC0) == 0x80) {
                cut--;
            }
            text = text.substr(0, cut) + "...";
        }
        std::string where = " at line " + std::to_string(la->getLine()) + ":" +
                            std::to_string(la->getCharPositionInLine());
        return text.empty() ? name + where : name + " '" + text + "'" + where;
    }

    // Loud-failure diagnostic for a token that begins no candidate rule, with
    // enough context (token, candidates, alternatives) to fix the call.
    std::string no_candidate_message(Token *la, ssize_t ttype) const {
        return "stream_by_rule: token " + token_desc(la, ttype) +
               " begins no candidate record rule (candidates: " +
               candidate_names() +
               "). The input must be a directly adjacent sequence of those "
               "rules, separated only by whitespace or comments that the "
               "lexer skips; an on-channel header or separator is not "
               "supported. Use chunk_by_rule for a whole-input parse, or "
               "stream_on_token or stream_on_pattern to split on a delimiter.";
    }

    // Loud-failure diagnostic for a rule that matched empty input, which would
    // otherwise loop forever without advancing the stream.
    std::string no_progress_message(ssize_t chosen, Token *la) const {
        size_t r = static_cast<size_t>(chosen);
        std::string rname = (prule_names && r < prule_names->size())
                                ? (*prule_names)[r]
                                : std::to_string(chosen);
        return "stream_by_rule: rule '" + rname +
               "' consumed no tokens at line " + std::to_string(la->getLine()) +
               ":" + std::to_string(la->getCharPositionInLine()) +
               "; it can match empty input, so the stream cannot advance. "
               "Give stream_by_rule a record rule that always consumes at "
               "least one token.";
    }

    // Parse up to `n` records. Stops cleanly at EOF; raises
    // (std::runtime_error) if an on-channel token begins no candidate rule, or
    // a chosen rule consumes nothing, so a malformed "file of records" fails
    // instead of being silently truncated. Any records already gathered this
    // batch are flushed first (returned with more = true) so none are lost; the
    // stashed diagnostic is thrown on the next call.
    bool run_batch(size_t n, std::vector<ChunkRec> &out) {
        if (!pending_error.empty()) {  // a flushed batch deferred this error
            done = true;
            throw std::runtime_error(pending_error);
        }
        if (done) {
            return false;
        }
        while (out.size() < n) {
            Token *la = tokens->LT(1);
            if (la->getType() == Token::EOF) {
                done = true;
                return false;
            }
            ssize_t ttype = static_cast<ssize_t>(la->getType());
            ssize_t chosen = -1;
            for (auto &cand : candidates) {
                if (cand.second.contains(ttype)) {
                    chosen = static_cast<ssize_t>(cand.first);
                    break;
                }
            }
            if (chosen < 0) {  // nothing here can begin a record
                std::string msg = no_candidate_message(la, ttype);
                if (!out.empty()) {  // flush gathered records; throw next call
                    pending_error = std::move(msg);
                    return true;
                }
                done = true;  // latch closed before throwing
                throw std::runtime_error(msg);
            }
            size_t before = tokens->index();
            // Hold a mark across the parse so UnbufferedTokenStream keeps this
            // record's tokens alive; otherwise root->getStart() and getStop(),
            // read below, would dangle. It is released immediately after,
            // which frees them.
            ssize_t mark = tokens->mark();
            ParserRuleContext *root =
                parser->parse(static_cast<size_t>(chosen));
            int32_t start, stop;
            rule_span(
                root, start, stop);  // ints, captured while tokens are marked
            tokens->release(mark);
            parser->getTreeTracker().reset();  // free this record's parse tree
            if (tokens->index() == before || start < 0 || stop < start) {
                std::string msg = no_progress_message(chosen, la);
                if (!out.empty()) {  // flush gathered records; throw next call
                    pending_error = std::move(msg);
                    return true;
                }
                done = true;
                throw std::runtime_error(msg);
            }
            size_t s = static_cast<size_t>(start);
            size_t e = static_cast<size_t>(stop);
            advance_origin(
                s);  // skip the inter-record gap (whitespace/comments)
            ChunkRec rec;
            rec.offset = s;
            rec.line = origin_line;
            rec.column = origin_col;
            rec.text = stream->getText(antlr4::misc::Interval(
                static_cast<ssize_t>(s), static_cast<ssize_t>(e)));
            out.push_back(std::move(rec));
            advance_origin(e + 1);
            stream->dropThrough(e + 1);  // free this record's characters
        }
        return true;
    }

    // Python-facing batch pull: run_batch under a released GIL, then build the
    // ((offset, line, column, text) rows, more) result tuple.
    nb::object next_batch(size_t n) {
        std::vector<ChunkRec> recs;
        bool more;
        {
            // Pure C++ (file IO, decoding, lexing, parsing, getText): release
            // the GIL for the whole batch; only building the result list below
            // needs it.
            nb::gil_scoped_release release;
            more = run_batch(n, recs);
        }
        nb::list rows;
        for (ChunkRec &r : recs) {
            rows.append(nb::make_tuple(r.offset,
                                       r.line,
                                       r.column,
                                       nb::str(r.text.data(), r.text.size())));
        }
        return nb::make_tuple(std::move(rows), more);
    }
};

// Trampoline so a Python subclass can override the 4 ParseTreeListener
// virtuals.
struct PyListener : public tree::ParseTreeListener {
    NB_TRAMPOLINE(tree::ParseTreeListener, 4);
    void visitTerminal(tree::TerminalNode *node) override {
        NB_OVERRIDE_PURE(visitTerminal, node);
    }
    void visitErrorNode(tree::ErrorNode *node) override {
        NB_OVERRIDE_PURE(visitErrorNode, node);
    }
    void enterEveryRule(ParserRuleContext *ctx) override {
        NB_OVERRIDE_PURE(enterEveryRule, ctx);
    }
    void exitEveryRule(ParserRuleContext *ctx) override {
        NB_OVERRIDE_PURE(exitEveryRule, ctx);
    }
};

// Module definition: bind the spec/diagnostic/chunker types and the parse/lex
// entry points (docstrings here surface in src/antlrope/_native.pyi via
// `pixi run stubgen`).
NB_MODULE(_native, m) {
    m.doc() = "antlrope: Python binding over the official ANTLR4 C++ runtime";
    // No __version__ here: the package version lives in the VERSION file and is
    // surfaced via antlrope.__version__ (see __init__.py).

    nb::class_<AtnShape>(m, "AtnShape")
        .def_ro("grammar_type", &AtnShape::grammar_type)
        .def_ro("num_states", &AtnShape::num_states)
        .def_ro("num_decisions", &AtnShape::num_decisions)
        .def_ro("num_rules", &AtnShape::num_rules)
        .def_ro("max_token_type", &AtnShape::max_token_type)
        .def("__repr__", [](const AtnShape &s) {
            return "AtnShape(grammar_type=" + std::to_string(s.grammar_type) +
                   ", num_states=" + std::to_string(s.num_states) +
                   ", num_decisions=" + std::to_string(s.num_decisions) +
                   ", num_rules=" + std::to_string(s.num_rules) +
                   ", max_token_type=" + std::to_string(s.max_token_type) + ")";
        });
    m.def("atn_shape",
          &atn_shape,
          nb::arg("serialized"),
          "Deserialize a serialized ATN int list and return its shape.");

    nb::class_<SyntaxError>(
        m,
        "SyntaxError",
        "A raw parse diagnostic produced by the native parser. The Python\n"
        "layer wraps these into [ParseError][antlrope.ParseError]\n"
        "exceptions on a listener's\n"
        "[syntax_errors][antlrope.FacadeListener.syntax_errors].")
        .def_ro(
            "line", &SyntaxError::line, "1-based line of the offending token.")
        .def_ro("column",
                &SyntaxError::column,
                "0-based column of the offending token.")
        .def_ro(
            "start",
            &SyntaxError::start,
            "0-based codepoint offset of the offending token's first\n"
            "character (matching the event-stream offsets), or -1 when there\n"
            "is no token (e.g. a lexer error).")
        .def_ro("stop",
                &SyntaxError::stop,
                "0-based codepoint offset of the offending token's last\n"
                "character, inclusive, or -1 when there is no token.")
        .def_ro("message",
                &SyntaxError::message,
                "ANTLR's human-readable error message.")
        .def("__repr__", [](const SyntaxError &e) {
            return "SyntaxError(line=" + std::to_string(e.line) +
                   ", column=" + std::to_string(e.column) +
                   ", start=" + std::to_string(e.start) +
                   ", stop=" + std::to_string(e.stop) +
                   ", message=" + e.message + ")";
        });

    nb::class_<LexerSpec>(
        m,
        "LexerSpec",
        "A deserialized lexer specification (the grammar's vocabulary, name\n"
        "lists, and ATN) used by the native lex and parse entry points.\n"
        "Obtain one through a generated `<Grammar>EventListener` instead of\n"
        "constructing it directly.")
        .def(nb::init<std::string,
                      std::vector<std::string>,
                      std::vector<std::string>,
                      std::vector<std::string>,
                      std::vector<std::string>,
                      std::vector<std::string>,
                      const std::vector<int32_t> &>(),
             nb::arg("grammar_file_name"),
             nb::arg("literal_names"),
             nb::arg("symbolic_names"),
             nb::arg("rule_names"),
             nb::arg("channel_names"),
             nb::arg("mode_names"),
             nb::arg("serialized"));

    nb::class_<ParserSpec>(
        m,
        "ParserSpec",
        "A deserialized parser specification (the grammar's vocabulary,\n"
        "rule names, and ATN) used by the native parse entry points.\n"
        "Obtain one through a generated `<Grammar>EventListener` instead of\n"
        "constructing it directly.")
        .def(nb::init<std::string,
                      std::vector<std::string>,
                      std::vector<std::string>,
                      std::vector<std::string>,
                      const std::vector<int32_t> &>(),
             nb::arg("grammar_file_name"),
             nb::arg("literal_names"),
             nb::arg("symbolic_names"),
             nb::arg("rule_names"),
             nb::arg("serialized"));

    nb::class_<StreamChunker>(m, "StreamChunker")
        .def(nb::init<LexerSpec &,
                      const std::string &,
                      std::vector<int32_t>,
                      int,
                      std::optional<int>,
                      bool,
                      bool,
                      size_t>(),
             nb::arg("lexer_spec"),
             nb::arg("path"),
             nb::arg("delim_types"),
             nb::arg("where"),
             nb::arg("channel").none(),
             nb::arg("lenient"),
             nb::arg("trim"),
             nb::arg("block") = 0,
             nb::keep_alive<1, 2>())
        .def("next_batch",
             &StreamChunker::next_batch,
             nb::arg("n"),
             "Pull up to n chunk records from the streaming token chunker.\n"
             "Returns (rows, more): rows is a list of (offset, line, column,\n"
             "text) tuples (whitespace trimmed unless trim=False; empty\n"
             "regions skipped) and more is False once the final region at\n"
             "EOF has been emitted.");

    nb::class_<StreamRuleChunker>(m, "StreamRuleChunker")
        .def(nb::init<ParserSpec &,
                      LexerSpec &,
                      const std::string &,
                      std::vector<int32_t>,
                      bool,
                      size_t>(),
             nb::arg("parser_spec"),
             nb::arg("lexer_spec"),
             nb::arg("path"),
             nb::arg("rule_indices"),
             nb::arg("lenient"),
             nb::arg("block") = 0,
             nb::keep_alive<1, 2>(),
             nb::keep_alive<1, 3>())
        .def(
            "next_batch",
            &StreamRuleChunker::next_batch,
            nb::arg("n"),
            "Pull up to n parsed-rule chunk records from the streaming rule\n"
            "chunker. Returns (rows, more): rows is a list of (offset, line,\n"
            "column, text) tuples and more is False once EOF (or a token that\n"
            "begins no candidate rule) is reached.");

    // Minimal node/token surface the listener callbacks need.
    nb::class_<Token>(m, "Token")
        .def("getType", &Token::getType)
        .def("getText", &Token::getText)
        .def("getLine", &Token::getLine)
        .def("getCharPositionInLine", &Token::getCharPositionInLine);

    nb::class_<tree::ParseTree>(m, "ParseTree");
    nb::class_<RuleContext, tree::ParseTree>(m, "RuleContext")
        .def("getRuleIndex", &RuleContext::getRuleIndex);
    nb::class_<ParserRuleContext, RuleContext>(m, "ParserRuleContext")
        .def("getStart", &ParserRuleContext::getStart, nb::rv_policy::reference)
        .def("getStop", &ParserRuleContext::getStop, nb::rv_policy::reference);
    nb::class_<tree::TerminalNode, tree::ParseTree>(m, "TerminalNode")
        .def("getSymbol",
             &tree::TerminalNode::getSymbol,
             nb::rv_policy::reference);
    nb::class_<tree::ErrorNode, tree::TerminalNode>(m, "ErrorNode");

    nb::class_<tree::ParseTreeListener, PyListener>(m, "ParseTreeListener")
        .def(nb::init<>())
        .def("visitTerminal", &tree::ParseTreeListener::visitTerminal)
        .def("visitErrorNode", &tree::ParseTreeListener::visitErrorNode)
        .def("enterEveryRule", &tree::ParseTreeListener::enterEveryRule)
        .def("exitEveryRule", &tree::ParseTreeListener::exitEveryRule);

    m.def("parse_count",
          &parse_count,
          nb::arg("parser_spec"),
          nb::arg("lexer_spec"),
          nb::arg("text"),
          nb::arg("start_rule"),
          "Diagnostic: parse and walk the tree with a native counting\n"
          "listener, without calling into Python.");
    m.def("parse_walk",
          &parse_walk,
          nb::arg("parser_spec"),
          nb::arg("lexer_spec"),
          nb::arg("text"),
          nb::arg("start_rule"),
          nb::arg("listener"),
          "Diagnostic fallback: parse and walk the tree, dispatching each\n"
          "node to a Python ParseTreeListener (slow per-node FFI path).");
    m.def(
        "parse_events",
        &parse_events,
        nb::arg("parser_spec"),
        nb::arg("lexer_spec"),
        nb::arg("text"),
        nb::arg("start_rule"),
        nb::arg("rule_mask") = nb::none(),
        nb::arg("token_mask") = nb::none(),
        "Parse and return (events, errors): a bulk flat int32 event buffer of\n"
        "4*N values (kind, payload, start, stop) as bytes, and a list of\n"
        "SyntaxError diagnostics collected during the parse. Optional\n"
        "rule_mask and token_mask (lists of indices to keep) filter events\n"
        "natively. The default stderr error listener is suppressed.");
    m.def("parse_stage_times",
          &parse_stage_times,
          nb::arg("parser_spec"),
          nb::arg("lexer_spec"),
          nb::arg("text"),
          nb::arg("start_rule"),
          "Diagnostic: dict of per-stage seconds (input_decode, lex_fill,\n"
          "parse_tree, walk) plus the token, event, and codepoint counts.");
    m.def(
        "lex",
        &lex,
        nb::arg("lexer_spec"),
        nb::arg("text"),
        nb::arg("token_mask") = nb::none(),
        "Run only the lexer and return (tokens, errors): a flat int32 buffer\n"
        "of 4*N values (type, channel, start, stop) as bytes (EOF omitted),\n"
        "and a list of SyntaxError diagnostics. Optional token_mask (list of\n"
        "token types to keep) drops the rest natively. The cheap stage used\n"
        "to chunk input for walk_parallel without a full parse.");
    m.def("rule_spans",
          &rule_spans,
          nb::arg("parser_spec"),
          nb::arg("lexer_spec"),
          nb::arg("text"),
          nb::arg("start_rule"),
          nb::arg("rule_mask") = nb::none(),
          nb::arg("outermost") = true,
          "Parse (entirely in C++) and return (spans, errors): a flat int32\n"
          "buffer of 3*N values (rule_index, start, stop) for each parse-tree\n"
          "rule kept by rule_mask (None = all), plus a list of SyntaxError\n"
          "diagnostics. With outermost=True, the subtree of a matched rule is\n"
          "not searched, so only top-level occurrences are returned. Used for\n"
          "rule-based chunking.");
}
