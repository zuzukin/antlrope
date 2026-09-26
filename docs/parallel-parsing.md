# Parallel parsing

When your input consists of many independent pieces that each parse on their own,
such as records, log entries, or top-level definitions,
[`walk_parallel`](reference/api.md#antlrope.FacadeListener.walk_parallel)
parses them on a thread pool. The native parse releases the [GIL], so the parses
run on multiple cores at the same time, and it produces one listener per chunk, in
input order.

```python
from my_listener import MyGrammarEventListener

chunks = MyGrammarEventListener.split_on_token(
    text, MyGrammarEventListener.RECORD, where="before"
)

for listener in MyGrammarEventListener.walk_parallel(
    chunks, start_rule="record"
):
    ...  # one fully-walked listener per chunk, in order
```

## Input: strings or positioned chunks

`walk_parallel` accepts an iterable of `str` **or**
[`Chunk`](reference/api.md#antlrope.Chunk):

- a bare `str` is treated as contiguous with the previous item, and its source
  position is computed for you;
- a `Chunk` sets an explicit `offset`, `line`, and `column` (and optional
  `sourcename`), so callbacks report
  [`span`](reference/api.md#antlrope.FacadeListener.span) and
  [`line_col`](reference/api.md#antlrope.FacadeListener.line_col) against the
  whole source rather than the chunk. A `Chunk` also re-anchors the bare strings
  that follow it.

The [chunking helpers](chunking.md) yield positioned `Chunk`s, so you can pass their
output directly to `walk_parallel`.

## Bounded, lazy, in order

The result is a lazy iterator. Chunks are pulled and parsed on demand, with at most
`max_workers` parses in progress at once; this is also the size of the window used
to keep results in order. Neither the whole input nor all the results need to be in
memory at once. Consume the iterator incrementally, or call `list(...)` on it if you
want all the results. Combined with a streaming chunker
([`stream_on_token`](reference/api.md#antlrope.FacadeListener.stream_on_token) or
[`stream_on_pattern`](reference/api.md#antlrope.FacadeListener.stream_on_pattern)), the whole
pipeline of reading, chunking, and parsing uses bounded memory regardless of input
size.

`max_workers` defaults to [os.cpu_count()]; pass `1` to run inline without a pool.

For an end-to-end pipeline over a file too large to hold in memory, see the
[streaming-records recipe](streaming-records.md). It feeds a streaming chunker into
`walk_parallel` and covers preamble handling, terminator preservation, and
recovering off-channel metadata.

## How much speedup?

The per-event Python dispatch still holds the GIL, so the speedup depends on how
much of the work is native parsing compared with Python code in your callbacks. See
the
[Parallel parsing](performance.md#parallel-parsing) section of Performance &
limitations for the scaling details and measured numbers.

[GIL]: glossary.md#gil
[os.cpu_count()]: https://docs.python.org/3/library/os.html#os.cpu_count
