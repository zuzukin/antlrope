# Performance & limitations

## Where the speed comes from — and its ceiling

Two costs dominate consuming a large parse from Python:

1. **Per-node [FFI] crossings.** A Python `ParseTreeListener` over a C++ parse is
   called once per tree node. The bulk event stream removes this entirely: one
   transfer instead of millions of calls.
2. **Python's per-item iteration.** Even with zero call overhead, Python must
   still loop over every event it receives. For a workload that touches most
   nodes, this cost cannot be removed. The only way to reduce it is to receive
   fewer events, which is what native filtering does.

Batching removes the call overhead, and filtering reduces the number of
iterations. Neither makes a workload that reserializes everything free, because
Python still has to iterate over the events that are kept.

## How fast, in relative terms

The comparison that matters for a Python user is the official
`antlr4-python3-runtime`, since that is the other way to consume the same
generated parser. Consider a workload that touches most nodes. This is close to
the worst case for this design, because Python still iterates over every kept
event:

- The **bulk event-stream [facade]** runs roughly **10–20× faster** than the
  official pure-Python runtime on such a workload. Workloads that subscribe to
  only some rules and tokens run faster still, because native filtering drops the
  other events before Python sees them.
- A **per-node Python listener over the native parse**, which exists only as an
  internal diagnostic path and is not public API, is much slower than the facade
  because it makes one FFI crossing per tree node. The bulk stream exists to
  remove that cost.
- The practical upper bound for any approach that still hands every node to
  Python is a **pure-C++ walk that never enters Python**. The facade closes most
  of the gap to that bound; to go further, you have to receive fewer events.

The [SystemRDL benchmark](benchmarks/systemrdl.md) measures a real grammar against
both the pure-Python runtime and the `speedy-antlr` tree-translation accelerator.
On its 2.6 MB input, Antlrope is about 20–23× faster than pure-Python and about
8–9× faster than speedy-antlr, with lower peak memory.

### Underlying C++ runtime

This package bundles a patched snapshot of the ANTLR4 C++ runtime (see
`vendor/antlr4-cpp/UPDATING.md`). One patch makes the lexer's per-character [DFA]
edge lookups lock-free. On a single thread, compared with the stock C++ runtime,
that patch alone makes lexing about **1.6–1.7×** faster and a whole JSON parse
about **1.2×** faster. The whole-parse gain is smaller because lexing is only about
a third of the parse time, and it is smaller still for grammars whose parsing
outweighs their lexing. A second patch helps concurrent parses that share a spec
(see [Parallel parsing](#parallel-parsing) below and
[How it works](concepts.md#how-much-do-the-patches-contribute)).

## Limitation: semantic predicates and embedded actions

This is the most important correctness limitation, so it is stated prominently
here.

The runtime executes the **interpreted [ATN]** ([`ParserInterpreter`][ATN interpreter]
and `LexerInterpreter`). It does **not** compile or run target-language code embedded
in your grammar:

- **[Semantic predicates][semantic predicate]**: `{...}?` conditions that enable
  or disable an alternative.
- **[Embedded actions][embedded action]**: `{...}` code blocks.

A generated, compiled ANTLR parser runs these as native code. The ATN
interpreter cannot. A grammar will not parse correctly here if it depends on a
predicate to choose between alternatives, or on an action that sets state read
later in the parse. There is no error for this: the interpreter makes the
prediction the ATN encodes without the predicate, which may differ from what your
grammar intends.

If your grammar relies on predicates or actions for correct parsing, use the
official `antlr4-python3-runtime` (its generated parser executes them), or
restructure the grammar to be predicate-free.

Grammars that are purely structural, such as most data and configuration
formats and many DSLs, are unaffected.

To find out where your grammar stands, run
[`antlrope check <parser-module>`](reference/cli.md#antlrope-check) on the
generated parser: it scans the serialized ATNs and reports every semantic
predicate and embedded action by rule (exit status 0 when there are none).
Precedence predicates from left-recursive rules and the built-in lexer commands
(`-> skip`, `-> channel(...)`, ...) are fine and are not flagged.

## Other notes

- **Single streaming pass.** You get one ordered traversal, not a retained tree.
  If you need random access, re-walking, XPath, or rewriting, keep the parse
  tree from the official runtime.
- **[GIL].** The native parse **releases the GIL**, so other Python threads keep
  running during a parse and `asyncio.to_thread(listener.walk, ...)` won't block
  the event loop.
- **A full per-record listener costs about as much as the parse itself.** A
  listener that subscribes to every rule and token pays one Python loop iteration
  per kept event, so it roughly doubles the per-record cost compared with the
  native parse alone. This is the per-item iteration cost described above. To
  reduce it, subscribe to fewer rules and tokens (the facade emits only those from
  C++), or aggregate inside a rule so that fewer events cross into Python. The
  [streaming-records recipe](streaming-records.md#cost-of-a-full-per-record-listener)
  ties this to a record pipeline.

## Parallel parsing

When your input is a sequence of **independent pieces**, such as the records of
a log, the top-level definitions of a source file, or the sections of a document,
you can parse them concurrently across CPU cores. Split the text into chunks and
pass them to [walk_parallel](parallel-parsing.md). The
[chunking helpers](chunking.md) can split on token boundaries with a single,
cheap lexer pass, or on a regular expression that you supply:

```python
chunks = RecordListener.split_on_token(text, RecordListener.RECORD, where="before")
records = [
    ln.to_model()                               # one result per chunk, in order
    for ln in RecordListener.walk_parallel(
        chunks, start_rule="record"
    )
]
```

`walk_parallel` yields results lazily with a bounded number of parses running at
once, so you can consume results incrementally instead of holding the whole input in memory.

Each chunk parses on a worker thread with the GIL released, so the parses overlap.
Worker threads use independent specs internally, so they never contend on a shared
ATN.

**How much speedup?** It depends on how much of the per-chunk work is the native
parse and how much is Python code in your callbacks. The parse runs in parallel,
but the per-event Python dispatch still holds the GIL, so it limits the speedup
(Amdahl's law). Measured on an 18-core M5 Max with hundreds of small chunks:

- **Parse-bound** work (light callbacks such as counting or validation): **~3×**,
  and rising with more cores.
- **Callback-heavy** work (rebuilding a rich object per node): **~2×**, plateauing
  early because the Python dispatch serializes.

To speed up callback-heavy workloads further, you currently need a
**free-threaded (no-GIL) CPython** build, where the dispatch also runs in
parallel. `walk_parallel` takes advantage of it automatically, with no code
changes. Process-based parallelism is the other
option, at the cost of pickling results back.

Two implementation notes:

- **Per-DFA locks.** A spec owns a mutable ATN. In the stock runtime, concurrent
  parses that share one spec serialize on a single per-ATN lock that guards DFA
  construction, which makes them slower than serial parsing. The vendored runtime
  moves those write locks onto each DFA (see `vendor/antlr4-cpp/UPDATING.md`,
  Patch 2), so a shared spec now scales as well as independent specs. Edge reads
  were already lock-free. This matters only when your own code shares one spec
  across threads. `walk_parallel` gives each worker thread its own spec, so it does
  not depend on this patch.
- **Cold DFA per parse.** Each parse builds its own prediction DFA from scratch, so
  very small chunks spend proportionally more time warming up. Larger chunks
  amortize this cost, and real-world chunks are usually large enough that it is
  negligible.

### Chunking: lexer vs regex

The [chunking helpers](chunking.md) come in three kinds with very different
costs: regex-based (`split_on_pattern` and `chunk_by_pattern`, a text scan),
token-based (`split_on_token` and `split_between_tokens`, a lexer pass), and
rule-based (`chunk_by_rule`, a full parse). The table below shows each producing
the same chunks (one per object in a JSON array of 200k flat objects, about
11 MB) on an 18-core M5 Max:

| method (~11 MB / 200k objects) | scan/parse stage | full chunking |
| ------------------------------ | ---------------: | ------------: |
| regex (`chunk_by_pattern`)         |  ~220 MB/s | ~99 MB/s |
| lexer (`split_between_tokens`)     |   ~30 MB/s | ~23 MB/s |
| rule  (`chunk_by_rule`)            |   ~14 MB/s | ~13 MB/s |

The regex only scans the text. The lexer tokenizes the entire input and then
drops all but the boundary tokens in C++. The rule chunker runs a full structural
parse. The underlying stage is about 7× slower with the lexer than with the
regex, and about 2× slower again with a full parse. That last ratio grows with
grammar complexity: prediction is cheap for JSON, but an ambiguous grammar parses
much more slowly than it lexes. End to end, the gaps narrow a little, because all
three pay the same Python cost: building the `SourceMap`, and then slicing,
trimming, and looking up the position of each chunk. Reproduce these numbers with
`scripts/bench_chunking.py`.

Choose by your constraints, not by speed alone:

- **Regex** is much faster, but it is **not token-aware**: a delimiter inside a
  string literal or comment still matches and causes a wrong split. Use it only
  when the delimiter text cannot appear anywhere else in the input.
- **Token-based** chunking never splits inside a string or comment token, and
  handles whitespace and [channels][token channel] the same way the grammar does.
- **Rule-based** chunking splits on actual grammar structure, with no delimiter
  heuristic. It costs a full parse, but only the chunk spans cross into Python.
  Use it when the per-chunk `walk_parallel` callback work outweighs this initial
  parse, or when no token or pattern marks the record boundaries.

Whichever you choose, the splitting cost is usually small compared with the
per-chunk parse that follows.

For a file too large to hold in memory, `stream_on_token`
([chunking](chunking.md)) is the streaming form of the token splitter. It opens
the file in C++ and lexes it over a sliding window, so it holds one chunk at a
time instead of the whole text and a `SourceMap` for the whole input. Combined
with `walk_parallel`, which already pulls chunks lazily with a bounded number of
parses running at once, peak memory is roughly one chunk plus the parses in
progress, and it does not grow with file size. It still tokenizes the entire
input, so its throughput matches the token-based row above; what changes is the
memory profile, not the speed. `stream_on_pattern` does the same for the regex
splitter. It reads the source incrementally in Python, so it supports any text
encoding, and it commits a delimiter once it has read at least one character past
it. Its throughput matches the regex row, and its memory use stays flat.

[FFI]: glossary.md#ffi
[facade]: glossary.md#facade
[DFA]: glossary.md#dfa
[ATN]: glossary.md#atn
[ATN interpreter]: glossary.md#atn-interpreter
[semantic predicate]: glossary.md#semantic-predicate
[embedded action]: glossary.md#embedded-action
[GIL]: glossary.md#gil
[token channel]: glossary.md#token-channel
