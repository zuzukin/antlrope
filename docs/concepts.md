# How it works

## The problem with a per-node listener

The classic ANTLR consumption model builds a [parse tree] and walks it, calling
`enterEveryRule`, `exitEveryRule`, or `visitTerminal` once per node. With a
Python listener over a C++ parse, every one of those calls is a
[foreign-function crossing][FFI]. For a document that produces tens of millions of
tree nodes, the cost of crossing once per node, rather than the parse itself,
dominates the runtime. Moving the parse to C++ barely helps if Python is still
called once per node.

## The bulk filtered event stream

**Antlrope** removes the per-node crossing. After the C++ runtime finishes parsing,
native code traverses the tree depth-first and appends one fixed-size record per
visited item to a single contiguous buffer. The buffer is handed to Python in one
transfer. The traversal matches ANTLR's `IterativeParseTreeWalker`: rule entries
and terminals are emitted in pre-order, and rule exits in post-order.

Each record is four `int32` values:

| field | meaning |
|---|---|
| `kind` | `0 = ENTER_RULE`, `1 = EXIT_RULE`, `2 = TERMINAL`, `3 = ERROR` |
| `payload` | rule index (enter or exit) or token type (terminal or error) |
| `start` | source char index of the item's first codepoint (`-1` if none) |
| `stop` | source char index of the item's last codepoint (`-1` if none) |

For a terminal or error, the span is the token. For a rule, it is the rule's full
extent, from the start of its first token to the end of its last token. Items with
no span, such as an empty rule or an error token that recovery inserted for a
missing token, report `-1`.

Token text is never copied across the boundary. The runtime returns only the
integer `(start, stop)` span; Python recovers the text on demand by slicing the
original source string, `text[start : stop + 1]`. Most consumers need the text of
only a few token types, so most tokens are never sliced.

## Native filtering via masks

The second technique is filtering in C++. A consumer usually cares about only
some of the rules and tokens. The native event builder takes a `rule_mask` and a
`token_mask` (lists of indices to keep), and events that are not in the masks are
dropped before they are appended to the buffer. This reduces the number of records
Python must iterate over, not just the cost of each call.

The generated [facade] sets this up automatically. `walk` checks which callbacks
your subclass overrides by comparing each `enter<Rule>`, `exit<Rule>`, and
`visitTerminal` against the no-op stub in the generated base class. It then builds
the masks from exactly those callbacks. If you subscribe to three rules and one
token type, the C++ side emits only those events. Callbacks you don't override add
no cost.

To get the complete, unfiltered stream, call `walk(..., filtered=False)`. This is
useful for diagnostics and for correctness testing.

## Off-channel tokens are not in the stream

The event stream is built from the parse tree, which holds only the tokens the
parser consumed, that is, tokens on the default [token channel]. Tokens the lexer
routed to a hidden channel (comments, directives, alignment metadata) are not in the
tree, so they never appear as `TERMINAL` events and never reach `visitTerminal`. The
walk never sees other channels. Even `filtered=False` only restores the on-channel
events that the masks would have dropped.

To recover off-channel tokens, lex the source separately:
[`lex`](reference/api.md#antlrope.FacadeListener.lex) returns every token with its
`channel` and `(start, stop)` span. You can keep the off-channel tokens and match
them to parse events by offset. The
[streaming-records recipe](streaming-records.md#recover-off-channel-metadata) shows
how to do this with per-record chunks.

## What runs where

- **C++**: [ATN] deserialization, lexing and parsing with `LexerInterpreter` and
  `ParserInterpreter` to build a full parse tree, and the masked depth-first
  traversal that builds the event buffer.
- **Python**: one loop over the buffer (`struct.iter_unpack("<4i", raw)`),
  dispatching your overridden callbacks and slicing token text as needed.

There is no generated C++ parser and no compilation of your grammar. The C++
runtime is driven entirely from the serialized ATN that the stock
`-Dlanguage=Python3` ANTLR tool already emits, plus the rule and token names read
from the generated Python classes.

## The vendored runtime and its patches

Antlrope builds against a vendored copy of the official ANTLR4 C++ runtime rather
than a system-installed one, so the wheel is self-contained. The copy is kept
verbatim under `vendor/antlr4-cpp/src` and remains under its BSD-3-Clause license.
Antlrope applies two performance patches on top of it. Both are intended to be
contributed upstream as pull requests; they are tracked on the `zuzukin/antlr4`
fork, and `vendor/antlr4-cpp/UPDATING.md` lists the exact branches and commit.

- **Lock-free DFA-edge reads.** The ATN simulator's most frequent operation is
  looking up the next DFA edge for each input character. The patch replaces the
  mutex-guarded hash map used for this lookup with a lazily allocated array of
  `std::atomic<DFAState*>`, so reading an edge takes no lock. This speeds up
  single-threaded parsing.
- **Per-DFA write locks.** The stock runtime guards changes to a DFA with a lock
  stored on the ATN. Every interpreter that shares a spec also shares that ATN, so
  concurrent parses of one grammar wait on a single lock even though their DFAs are
  independent. This patch moves the locks for writing DFA states and edges from the
  ATN to each DFA, so independent parses can proceed in parallel. This is what lets
  threads that share one spec parse in parallel; without it, they run slower than
  serial parsing. [walk_parallel](parallel-parsing.md) gives each worker thread its
  own spec, so it does not depend on this patch.

Neither patch changes parse results; both only improve concurrency and throughput.
Each is kept as a small, isolated diff so it can be removed from the snapshot if
and when it is accepted upstream.

### How much do the patches contribute?

To isolate the effect of each patch, we rebuilt Antlrope against the unpatched
upstream runtime and then added each patch in turn. The benchmark parses a 4.7 MB
JSON document on an Apple M5 Max (`scripts/bench_runtime_patches.py`):

| runtime | single-thread parse | shared-spec parse, 4 threads |
| --- | ---: | ---: |
| pristine upstream | 551 ms | **0.5×** (parallel *slower* than serial) |
| + lock-free DFA reads | 486 ms | 0.6× |
| + per-DFA write locks | ~480 ms | **~2.0×** |

The results show two things:

- **The single-threaded speedup comes mostly from the design, not the runtime
  patches.** Lock-free reads cut this JSON parse time by about 12–15%: they make
  the lexer about 1.6–1.7× faster, but lexing is only about a third of the total.
  The rest of Antlrope's ~20× advantage over the pure-Python runtime (see
  [the SystemRDL benchmark](benchmarks/systemrdl.md)) comes from the bulk event
  stream, not from the patched C++. If the patch saved the same share there, Antlrope
  would still be about 18× faster than pure Python on the unpatched runtime. JSON is
  lexer-heavy, which favors this patch; a grammar whose parsing outweighs its lexing
  gains less.
- **The shared-spec parallel speedup comes entirely from the runtime patches.** On the
  unpatched runtime, parsing with a shared spec on four threads is twice as slow as
  serial parsing, because the ATN-wide write lock serializes every thread. With
  per-DFA write locks, it is about 2× faster than serial, and about 4× faster in
  wall-clock time than the unpatched runtime on the same four threads. Without this
  patch, sharing one spec across threads is not worthwhile.
  [walk_parallel](parallel-parsing.md) is unaffected either way, because each of its
  worker threads builds its own spec.

[parse tree]: glossary.md#parse-tree
[FFI]: glossary.md#ffi
[facade]: glossary.md#facade
[ATN]: glossary.md#atn
[token channel]: glossary.md#token-channel
