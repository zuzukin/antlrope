<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img alt="antlrope" src="docs/assets/logo.svg" width="420">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/zuzukin/antlrope/actions/workflows/ci.yml?query=branch%3Adev"><img alt="CI" src="https://github.com/zuzukin/antlrope/actions/workflows/ci.yml/badge.svg?branch=dev"></a>
  <a href="https://app.codecov.io/gh/zuzukin/antlrope/tree/dev"><img alt="coverage" src="https://codecov.io/gh/zuzukin/antlrope/branch/dev/graph/badge.svg"></a>
  <a href="https://pypi.org/project/antlrope/"><img alt="PyPI" src="https://img.shields.io/pypi/v/antlrope"></a>
  <a href="https://anaconda.org/conda-forge/antlrope"><img alt="conda-forge" src="https://img.shields.io/conda/vn/conda-forge/antlrope?label=conda-forge"></a>
  <a href="https://pypi.org/project/antlrope/"><img alt="Python" src="https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fzuzukin%2Fantlrope%2Fmain%2Fpyproject.toml"></a>
  <a href="https://zuzukin.github.io/antlrope/"><img alt="docs" src="https://img.shields.io/badge/docs-latest-blue"></a>
</p>

A fast, C++-accelerated [ANTLR](https://www.antlr.org/) runtime for Python, for target-agnostic grammars.

*antl**rope*** = ANTLR + **O**rdered **P**arse **E**vents: your parse is delivered as one ordered stream of events instead of a per-node tree walk.

> **10–20× faster** than the official pure-Python `antlr4-python3-runtime` on
> workloads that touch most nodes, and faster still when your listener subscribes
> to only part of the grammar.

[![Parsing and reading a 2.6 MB SystemRDL file: antlrope is ~21x faster than the pure-Python runtime and ~8x faster than the speedy-antlr accelerator, at lower peak memory](docs/benchmarks/systemrdl.svg)](docs/benchmarks/systemrdl.md)

<sub>Parsing and reading a real 2.6 MB SystemRDL file, compared with the pure-Python runtime and the `speedy-antlr` accelerator. See the [full benchmark](docs/benchmarks/systemrdl.md).</sub>

Generate your parser with the ordinary ANTLR tool targeting Python, install
this package, generate a small *facade* class, and write a pure-Python event listener.
Parsing runs inside the official ANTLR4 C++ runtime, driven directly from the
serialized ATN (Augmented Transition Network) that the stock Python target already
emits. A conventional listener walks the parse tree node by node, crossing the
foreign-function boundary once per node. Here, the C++ side instead collects a
single, filtered stream of events and hands it to Python in one transfer. Rules
and tokens your listener doesn't subscribe to are dropped in C++, before they
reach Python.

It complements, rather than replaces, the official `antlr4-python3-runtime`: you
use the same generated parser and get a faster way to consume it.

## Install

From PyPI:

```sh
pip install antlrope
```

Or from conda-forge (with conda, mamba, or pixi):

```sh
conda install -c conda-forge antlrope
```

Either way you get a pre-compiled binary, so there is nothing to build. The
official `antlr4-python3-runtime` is installed automatically as a dependency.

## Quickstart

1. **Generate your parser** with the stock ANTLR tool (Python target):

   ```sh
   antlr4 -Dlanguage=Python3 MyGrammar.g4 -o generated
   ```

2. **Generate the facade** from the generated parser module:

   ```sh
   antlrope gen generated.MyGrammarParser MyGrammar -o my_listener.py
   ```

   This emits a `MyGrammarEventListener` base class with `enter<Rule>`,
   `exit<Rule>`, `visitTerminal`, and `visitError` stubs and token-type constants.
   The facade also imports your lexer and parser and stores references to them, so
   you never pass them at parse time. The lexer module name is derived from the
   parser module name using ANTLR's `<Grammar>Lexer` / `<Grammar>Parser` naming
   convention. Pass `--lexer` if your lexer module is named differently.

3. **Subclass it** and override only the callbacks you care about:

   ```python
   from my_listener import MyGrammarEventListener

   class Collector(MyGrammarEventListener):
       def enterPair(self) -> None:
           ...
       def visitTerminal(self, token_type: int, text: str) -> None:
           ...

   Collector().walk(source_text)
   ```

The callbacks you override determine which events the C++ side emits; it skips
every other rule and token. The fewer node kinds you subscribe to, the faster the
walk.

## How it works

- The C++ extension deserializes the ATN from your generated lexer and parser and
  runs it with ANTLR's `LexerInterpreter` and `ParserInterpreter`. No C++ parser is
  generated.
- After parsing, a native depth-first traversal of the parse tree appends
  fixed-size `(kind, payload, start, stop)` int32 records to a single buffer
  (`parse_events`).
- `kind`: `0=ENTER_RULE, 1=EXIT_RULE, 2=TERMINAL, 3=ERROR`; `payload` is the rule
  index or token type; `start` and `stop` are character indices into the source
  (the token's span, or a rule's full extent; `-1` for an item with no span, such
  as an empty rule). Python recovers token text by slicing `text[start:stop + 1]`,
  so no strings are copied across the boundary.
- Masks built from your overridden callbacks filter rules and tokens out of the
  stream in C++.

## Limitations

Antlrope only works with target-language-agnostic grammars. It interprets the
ATN, so it cannot run target-language semantic predicates or embedded grammar
actions, and grammars that depend on them will not parse correctly. See
[Performance & limitations](docs/performance.md) for details and for the
performance characteristics of the event-stream approach.

## Contributing

Development setup, pixi environments, and the vendored-runtime workflow live in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

Full documentation: **[zuzukin.github.io/antlrope](https://zuzukin.github.io/antlrope/)**.
In the repo, see [`docs/`](docs/index.md): [getting started](docs/getting-started.md),
[installation](docs/installation.md),
[API reference](docs/reference/api.md),
[chunking](docs/chunking.md),
[migrating from antlr4-python3-runtime](docs/migrating.md),
[performance & limitations](docs/performance.md),
[how it works](docs/concepts.md), and the
[SystemRDL benchmark](docs/benchmarks/systemrdl.md).

Using an AI coding assistant to write a listener or port a `ParseTreeListener`?
Give it the LLM-oriented summary of these docs at
[zuzukin.github.io/antlrope/llms.txt](https://zuzukin.github.io/antlrope/llms.txt),
your grammar, and, if you are porting, your existing listener.

## License

Apache-2.0. Bundles the ANTLR4 C++ runtime (BSD-3-Clause) under
`vendor/antlr4-cpp/`; see `vendor/antlr4-cpp/UPDATING.md` for provenance.
