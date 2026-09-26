# Examples

Two complete, runnable programs live in the
[`examples/`](https://github.com/zuzukin/antlrope/tree/dev/examples) tree of the
repository. Each pairs a small ANTLR grammar with a generated facade and a Python
consumer, so you can read the whole thing end to end:

- **[JSON: reconstruct a value](json.md)**: the basics. Subclass the generated
  facade, override a handful of callbacks, and rebuild a parsed JSON document as
  native Python objects with a small value stack. Start here.
- **[Schema: parallel & streaming indexing](schema.md)**: scaling to large input. A
  small interface-definition language, whose files are sequences of independent
  `message` and `enum` definitions, is indexed in three ways: a single whole-file
  walk, chunking and parsing across cores, and streaming with bounded memory.

## What you need

The examples are part of the repository, not the installed package. Installing
**Antlrope** gives you only the runtime, not the example grammars, their generated
parsers, or the sample data. To run an example you need just two things:

- **Antlrope installed** in your environment (`pip install antlrope` or
  `conda install -c conda-forge antlrope`). The consumers depend only on Antlrope and
  the Python standard library, with no other packages and no pixi.
- **A copy of the `examples/` files** (see below). Each parser and facade is checked
  in, so the examples run as they are. A JDK and the ANTLR tool are needed only if
  you want to regenerate a parser after editing a grammar.

Use example files from the same version as your installed Antlrope. Each generated
facade records the version that generated it. If that differs from your installed
version, the code still runs, but `antlrope up-to-date <facade>` reports the
mismatch.

## Getting the example files

Clone the repository (works everywhere):

```sh
git clone --depth 1 https://github.com/zuzukin/antlrope
cd antlrope
```

Or, without git, download just the `examples/` directory (macOS and Linux; with GNU
tar, add `--wildcards` before the pattern):

```sh
curl -L https://github.com/zuzukin/antlrope/archive/refs/heads/dev.tar.gz \
  | tar -xz --strip-components=1 '*/examples'
```

## Running them

Run each consumer from inside its example directory. The scripts import their facade
and generated parser by a name relative to that directory, so the working directory
matters:

```sh
cd examples/json    && python to_python.py '{"a": [1, true], "b": "hi"}'
cd examples/schema  && python index.py                      # the bundled sample
cd examples/schema  && python index.py --benchmark 50000    # time the three modes
```

In an installed environment, that is all you need: `python`, the example files, and
Antlrope on the path.

### From a source-tree clone (pixi)

If you are working in a clone of the repo with [pixi](https://pixi.sh) set up, the
bundled tasks wrap the same commands:

```sh
pixi run example          # JSON: reconstruct a value from argv
pixi run example-schema   # Schema: index the bundled sample.schema
```

To regenerate a parser or facade after editing a grammar (this needs the `gen`
environment, which provides the JDK and the ANTLR tool):

```sh
pixi run gen-schema           # Schema.g4      -> generated/Schema{Lexer,Parser}.py
pixi run gen-schema-facade    # generated parser -> schema_listener.py
pixi run format               # tidy the regenerated facade
```
