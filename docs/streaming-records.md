# Recipe: stream a file of records

When a file is a long sequence of independent records, such as log lines, NDJSON,
sensor readings, or the top-level definitions of a source file, and is too large to
hold in memory, you can read, split, and parse it in a single pipeline with bounded
memory. Peak memory is about one record plus the parses in progress, and does not
grow with file size. This recipe connects a streaming chunker to
[`walk_parallel`](parallel-parsing.md) and covers the details that commonly cause
problems: the preamble, a whitespace terminator, absolute positions, and metadata
that the parse tree does not contain.

The running example: a file whose records are one per line, each parsed by a grammar
rule `record` (which ends in a mandatory `EOL`), generated as `RecordEventListener`.

## The pipeline

Split on the newline with `where="after"` so each chunk *ends* with its terminator,
and pass the lazy stream directly to `walk_parallel`:

```python
from record_listener import RecordEventListener as R

chunks = R.stream_on_pattern("big.log", r"\n", where="after", trim=False)
for listener in R.walk_parallel(chunks, start_rule="record"):
    ...  # one fully-walked listener per record, in input order
```

Both ends pull lazily. `stream_on_pattern` reads the file over a sliding window and
produces one chunk at a time, and
[`walk_parallel`](reference/api.md#antlrope.FacadeListener.walk_parallel) keeps a
bounded number of parses in progress. Nothing holds the whole file.

## Keep the terminator: `trim=False`

By default every chunker trims surrounding whitespace from each chunk and drops
whitespace-only regions. That is wrong here: a `record` rule that ends in `EOL`
needs the trailing newline. The default would strip it, and the parse would then
fail on the missing `EOL`. Pass `trim=False` to keep each region exactly as it
appears (only empty, zero-length regions are dropped):

```python
# trim=True (default):  '{"a": 1}'      -> EOL gone, record won't match
# trim=False:           '{"a": 1}\n'    -> terminator preserved
```

`trim=False` is available on every delimiter chunker
([`split_on_token`](reference/api.md#antlrope.FacadeListener.split_on_token),
[`stream_on_token`](reference/api.md#antlrope.FacadeListener.stream_on_token),
[`split_on_pattern`](reference/api.md#antlrope.FacadeListener.split_on_pattern),
`stream_on_pattern`, …).

## The preamble (content before the first delimiter)

With `where="before"`, the region before the first delimiter becomes its own
leading chunk, such as a header or preamble. With `where="after"`, the region after
the last delimiter becomes a trailing chunk. So a file that starts with a header line
followed by records, split on a record marker, yields the header as the first chunk:

```python
stream = R.stream_on_pattern("big.log", r"^record\b", flags=re.MULTILINE, where="before")
header = next(stream)                                   # the pre-first-record preamble
for listener in R.walk_parallel(stream, start_rule="record"):
    ...                                                 # stream now yields only records
```

If the file may or may not have a preamble, don't assume the first chunk is one.
Either check for it (a record chunk starts with the marker, and a preamble does not),
or choose a `where` value and pattern that exclude it. The same leading and trailing
regions appear with the in-memory `split_*` chunkers; see
[Chunking](chunking.md#leading-and-trailing-regions-the-preamble).

## Absolute positions

Each [`Chunk`](reference/api.md#antlrope.Chunk) records its `offset`, `line`, and
`column` relative to the whole file, not the chunk, and `walk_parallel` preserves
them.
So inside a callback,
[`span`](reference/api.md#antlrope.FacadeListener.span) and
[`line_col`](reference/api.md#antlrope.FacadeListener.line_col) report whole-file
positions, and [`sourcename`](reference/api.md#antlrope.FacadeListener.sourcename)
(defaulting to the file path) lets you log `sourcename:line:column`, exactly as if
you had parsed the file in one piece.

## Recover off-channel metadata

The walk ignores hidden channels. `walk_parallel` walks the parse tree, which
contains only the tokens the parser consumed, and those are on the default channel.
Tokens the lexer sent to a hidden [channel][token channel] (comments, directives,
alignment metadata) are never in the tree, so they never reach `visitTerminal`. To
recover them, lex the chunk text yourself and keep the off-channel tokens;
[`lex`](reference/api.md#antlrope.FacadeListener.lex) returns every token with its
`channel`. The chunk text is only available while you hold the chunk, so do this in
an ordinary loop instead of passing the chunks straight to `walk_parallel`:

```python
for chunk in R.stream_on_pattern("big.log", r"\n", where="after", trim=False):
    listener = R().walk(chunk.text, start_rule="record")        # on-channel structure
    comments = [t for t in R.lex(chunk.text) if t.channel != 0]  # 0 = default channel
    # a LexToken's start/stop are offsets *within the chunk*; add chunk.offset
    # for whole-file positions:
    spans = [(chunk.offset + t.start, chunk.offset + t.stop) for t in comments]
```

This loop gives up the thread pool in exchange for access to the chunk text. To keep
both, lex each chunk in a wrapper generator that yields `(chunk, comments)`, and parse
the chunks in parallel separately.[^1]

## When records are not delimiter-marked

If records are defined by grammar structure rather than by a delimiter, and the
file is a top-level sequence of directly adjacent records, use
[`stream_by_rule`](reference/api.md#antlrope.FacadeListener.stream_by_rule), which
parses one record at a time and needs no delimiter. It requires the whole input to be
such a sequence, with only whitespace and comments that the lexer skips between
records. If an on-channel token does not begin any candidate rule, for example an
unsupported header or separator, it raises a clear error instead of silently
truncating the stream. For records separated by commas or other on-channel tokens,
use `stream_on_token` or `stream_on_pattern` instead.

## Cost of a full per-record listener

A full Python listener pays one loop iteration per kept event, so walking a record
with everything subscribed costs roughly as much in Python as the native parse
itself. There are two ways to reduce this cost, both covered in
[Performance](performance.md#where-the-speed-comes-from-and-its-ceiling): subscribe
only to the rules and tokens you need (the facade emits only those from C++), or
aggregate inside a rule so that fewer events cross into Python.

[token channel]: glossary.md#token-channel

[^1]: The need for a first-class channel-aware callback during the walk is
[tracked in issue #2](https://github.com/zuzukin/antlrope/issues/2).
