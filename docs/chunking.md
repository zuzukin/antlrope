# Chunking

When your input consists of many independent pieces, such as records, log lines,
or top-level definitions, you can split it into [`Chunk`](reference/api.md#antlrope.Chunk)s
and parse them in parallel with
[`walk_parallel`](parallel-parsing.md). The generated `<Grammar>EventListener`
provides chunking classmethods that produce those chunks, each carrying its
exact source position so callbacks still report positions against the whole source.
The token-based and rule-based chunkers use the [facade]'s lexer and parser and
its token-type constants, so you call them on the class without passing a lexer or
parser.

There are four kinds of chunker. The first three trade correctness against speed;
the fourth, streaming, bounds memory use. Choose by your constraints rather than
by speed alone, because the splitting cost is usually small compared with the
per-chunk parse that follows.

## Token-based — split at lexer tokens

A single lexer pass in C++, with no parser, finds the boundaries, so a delimiter
inside a string or comment never causes a split. The splitter asks the lexer for
the boundary tokens only, so little data crosses into Python.

```python
# each chunk begins with a delimiter token (one type, or several):
chunks = MyGrammarEventListener.split_on_token(
    text, MyGrammarEventListener.RECORD, where="before"
)
# or one chunk per open..close region (optionally balanced):
chunks = MyGrammarEventListener.split_between_tokens(
    text, (MyGrammarEventListener.BEGIN, MyGrammarEventListener.END), nested=True
)
```

- [`split_on_token`](reference/api.md#antlrope.FacadeListener.split_on_token):
  split at each delimiter token. `where="before"` or `where="after"` puts the
  delimiter at the start or end of each chunk.
- [`split_between_tokens`](reference/api.md#antlrope.FacadeListener.split_between_tokens):
  one chunk per region between an opening and a closing token. With several
  bracket kinds, each opener matches its own closer, and `nested=True` matches
  balanced pairs.
- [`lex`](reference/api.md#antlrope.FacadeListener.lex): the underlying token pass
  without a parser, exposed directly in case you want the token stream.

## Regex-based — split on a pattern

These chunkers use no lexer. They find delimiters about 7× faster (about 4× faster
end to end), but they are not token-aware, so a match inside a string still causes a
split. Prefer them when the delimiter text cannot appear anywhere else in the
input.

- [`split_on_pattern`](reference/api.md#antlrope.FacadeListener.split_on_pattern):
  the regex counterpart of `split_on_token`; the pattern matches the delimiter.
- [`chunk_by_pattern`](reference/api.md#antlrope.FacadeListener.chunk_by_pattern):
  the pattern matches a whole record, so each match is a chunk.

## Rule-based — split on grammar structure

[`chunk_by_rule`](reference/api.md#antlrope.FacadeListener.chunk_by_rule) parses the
input entirely in C++ and yields each occurrence of a grammar rule as a chunk, so it
splits on actual grammar structure rather than on a token or pattern heuristic. It
costs a full parse, but only the chunk spans cross into Python. It is worthwhile
when the per-chunk `walk_parallel` callback work dominates, or when no delimiter
clearly marks a record.

```python
# one chunk per top-level function:
chunks = MyGrammarEventListener.chunk_by_rule(text, "function")
```

See [Performance & limitations](performance.md#chunking-lexer-vs-regex) for
measurements of the token, regex, and rule chunkers and the trade-off between speed
and correctness.

## Streaming — bounded memory

For input too large to hold in memory, the streaming chunkers read incrementally
and yield chunks without keeping the whole source. Combined with `walk_parallel`,
the whole pipeline runs in bounded memory.

- [`stream_on_token`](reference/api.md#antlrope.FacadeListener.stream_on_token):
  the streaming form of `split_on_token`. The native layer opens the file and lexes
  it over a sliding window (UTF-8).
- [`stream_on_pattern`](reference/api.md#antlrope.FacadeListener.stream_on_pattern):
  the streaming form of `split_on_pattern`. The regex runs in Python, so it reads
  any text source (a path, an open file, or an iterable of `str`) in any encoding.
- [`stream_by_rule`](reference/api.md#antlrope.FacadeListener.stream_by_rule): the
  streaming form of `chunk_by_rule`, for input that is a top-level sequence of
  records, each an occurrence of a grammar `rule` (or one of several). It parses one
  record at a time, feeding the lexer into the parser with bounded memory. Records
  must be directly adjacent, with only whitespace and comments that the lexer skips
  between them. When there are several candidate rules, the next token decides
  which one to parse, so the rules should start with different tokens (for example
  `class` and `def`). If an on-channel token does not begin any candidate rule (an
  unsupported header or separator), it raises a clear error naming the token and
  candidates rather than silently truncating. Unlike `chunk_by_rule`, it does not
  search a full parse for the rule at any depth. For arbitrarily nested or
  comma-separated records, use `chunk_by_rule` or `stream_on_token`.

## Source positions and names

Every chunk carries its start `(offset, line, column)` against the whole source, so
callbacks still report positions against the original after splitting. Each chunker
also accepts `sourcename=`, a file name for diagnostics that is recorded on every
chunk. The streaming chunkers default it to the file path. During the parse it is
available through
[`FacadeListener.sourcename()`](reference/api.md#antlrope.FacadeListener.sourcename),
so a callback can report a position as `sourcename:line:column`.

By default each chunk is trimmed of surrounding whitespace and whitespace-only
regions are dropped. Pass `trim=False` to a delimiter chunker to keep every region
exactly as it appears. This preserves leading or trailing whitespace that the
grammar needs, such as a record's mandatory newline; only empty, zero-length
regions are dropped.

## Leading and trailing regions (the preamble)

With `where="before"`, any content before the first delimiter becomes its own
leading chunk; this is often a file's header or preamble. With `where="after"`, any
content after the last delimiter becomes a trailing chunk. These chunks are not
marked in any way (each is simply the first or last chunk), so skip or handle them
explicitly. The
[streaming-records recipe](streaming-records.md) walks through preamble handling,
`trim=False` terminators, absolute positions, and recovering off-channel metadata end
to end.

[facade]: glossary.md#facade
