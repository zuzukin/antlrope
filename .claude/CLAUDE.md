# antlrope — project context

Antlrope is a fast, C++-accelerated ANTLR runtime for Python that needs **no
per-grammar C/C++ compilation by the user**. It drives the official ANTLR4 C++
runtime from the serialized ATN that the stock `-Dlanguage=Python3` ANTLR tool
already emits, and hands Python a single bulk, filtered int32 event stream instead
of a per-node parse-tree walk. Users generate a parser with the normal ANTLR tool,
install this package, generate a small facade with `antlrope gen`, and write a
pure-Python event listener.

## Layout

- `src/antlrope/`: the Python package (src/ layout).
  - `base.py`: most of the runtime.
    - `FacadeListener` is the base class of every generated facade. `walk` and
      `walk_parallel` call `_drive`, which finds the callbacks a subclass
      overrides, builds the native rule and token masks, and runs the event loop.
    - The chunkers are classmethods on `FacadeListener`: `split_on_token`,
      `split_between_tokens`, `split_on_pattern`, `chunk_by_pattern`,
      `chunk_by_rule`, the streaming `stream_on_token`, `stream_on_pattern` and
      `stream_by_rule`, and `lex`.
    - Also here: `Chunk`, `LexToken`, `ParseError`, and the spec caches.
      `walk_parallel` workers use per-thread specs (`_specs_for_thread`).
  - `location.py`: `SourceMap` (char offset ↔ line/column) and `LineCol`.
  - `cli/`: the `antlrope` command group (`main.py`) with the subcommands `gen`
    (`generate.py`), `regen`, `up-to-date`, `check`, `rules`, and `tokens`.
    `metadata.py` reads and writes the origin header of generated facades.
  - `_native.pyi`: the checked-in stub for the compiled extension.
  - `VERSION`: the single source of the version.
- `cpp/`: the nanobind extension sources (`binding.cpp`, `events.h`,
  `file_char_stream.h`). The module is named `_native`.
- `vendor/antlr4-cpp/`: the vendored ANTLR4 C++ runtime, carrying two performance
  patches (lock-free DFA-edge reads, per-DFA write locks). It is **BSD-3-Clause**,
  separate from the package's own Apache-2.0 license. Read its `UPDATING.md` before
  refreshing the snapshot.
- `examples/json/`, `examples/schema/`, `examples/predicate/`: end-to-end
  examples that double as test fixtures.
- `scripts/`: the native build, stubgen, CLI-docs generator, benchmarks, and the
  ASan harness.
- `tests/`, and `docs/` (a Zensical site, configured in `zensical.toml`).

The public API is `__all__` in `src/antlrope/__init__.py`: `Chunk`,
`FacadeListener`, `LexToken`, `LineCol`, `ParseError`, `SourceMap`, and
`__version__`.

## Event stream

Each record is four int32 values, `(kind, payload, start, stop)`, decoded with
`struct.iter_unpack("<4i", raw)`. `kind` is `0=ENTER_RULE, 1=EXIT_RULE,
2=TERMINAL, 3=ERROR`, and `payload` is the rule index or token type. `start` and
`stop` are char offsets into the source (the rule's full span for rule events), or
`-1` when there is no span. Python recovers token text by slicing
`text[start:stop + 1]`, so no strings cross the boundary. Only the rules and tokens
a listener overrides are emitted (native filtering).

## Dev workflow (pixi)

```sh
pixi install              # solve and build the editable extension
pixi run build            # rebuild _native after editing cpp/ or vendor/ (cmake)
pixi run test             # pytest suite
pixi run check            # lint, typecheck, and test
pixi run format           # ruff format (format-cpp for clang-format)
pixi run example          # JSON reconstruction example
pixi run stubgen          # regenerate _native.pyi after binding changes
pixi run gen-cli-docs     # regenerate docs/reference/cli.md after help-text changes
pixi run gen-facade       # regenerate the JSON facade (gen-schema-facade for schema)
pixi run gen-json         # regenerate the JSON example parser (gen env, needs a JDK)
pixi run docs-build       # build the docs site into site/ (docs env)
```

Environments:
- `default`: Python build and test, through the `dev` feature. No JDK.
- `gen`: OpenJDK and the ANTLR tool, kept separate.
- `docs`: Zensical only; it doesn't include the default feature.
- `recipe`: rattler-build, for the conda recipe.
- `pack`: the `dev` feature plus packaging tools.

The editable install uses no build isolation, so CMake and Ninja must be on
`PATH`; the pixi environment provides them.

## Build

The build uses scikit-build-core, nanobind, and CMake. `editable.rebuild = false`,
so importing the package never runs the toolchain, and it imports from any
interpreter, even an unactivated IDE prefix. After editing `cpp/` or `vendor/`,
recompile explicitly with `pixi run build` (`scripts/build_native.py`, a cmake
build and install).

`pixi run build` fails with "Could not match a unique build dir" if more than one
`build/cp312-abi3-*` directory exists; delete the stale one.

The extension is built with `STABLE_ABI` as a single abi3 wheel.
`MACOSX_DEPLOYMENT_TARGET = 11.0`. The minimum Python is **3.12**, so use modern
typing: `list[...]`, `X | None`, `collections.abc` rather than `typing`, `type`
aliases, and `Self`.

The version lives only in `src/antlrope/VERSION`. pyproject reads it dynamically,
and `antlrope.__version__` reads it through `importlib.resources`. IDEs and type
checkers can't follow the editable redirector, so the compiled `_native` module has
a checked-in stub, `src/antlrope/_native.pyi`. After changing the binding's
interface or its docstrings, run `pixi run stubgen`. It re-applies the hand edits
automatically (`scripts/stubgen.py`).

## Known limitation

The pure-ATN interpreter **cannot evaluate target-language semantic predicates
(`{...}?`) or embedded actions (`{...}`)**; predicates are treated as true.
Grammars that depend on them won't parse correctly. `antlrope check` reports them.
`tests/test_predicate_limitation.py` pins this behavior, and the docs state it
prominently; keep both that way.

## Conventions

- The default branch is `dev`.
- **Versioning.** The version is bumped once per release cycle. After a release,
  a "Start X.Y.Z development" commit bumps `VERSION` and opens an
  `## [X.Y.Z] - Unreleased` section in `CHANGELOG.md`. Every commit that changes
  runtime behavior or user-facing docs (`README`, `docs/`, docstrings, CLI help)
  adds an entry to that section. Build, test, and tooling-only changes don't need
  an entry. Releases follow `RELEASING.md`.
- Hand-authored `.py` and `.cpp` files carry the Apache-2.0 header.
- **Docstrings** use mkdocstrings Markdown style: single backticks and Google-style
  sections, with no reStructuredText roles or `::` directives. Cross-references are
  written `[title][antlrope.Symbol]` with a **plain** title. Never put the title in
  backticks: `` [`title`][ref] `` renders the title as inline code, which hides
  that it is a clickable link. Backticks are still correct for inline code that is
  not a cross-reference.
- The package license is **Apache-2.0**. The vendored runtime under `vendor/`
  stays **BSD-3-Clause**; never relicense vendored code.
- Public docs (README, `docs/`) compare only against alternatives the reader
  actually has, such as the official `antlr4-python3-runtime`. Don't include
  internal research context or testbed names.
- Development and build details belong in `CONTRIBUTING.md`, not the README.
- **Generated files.**
  - `docs/reference/cli.md` is generated from the argparse help between its
    `gen-cli-help` markers. Edit the help strings in `src/antlrope/cli/` and run
    `pixi run gen-cli-docs`. `tests/test_cli_docs.py` fails if it has drifted.
  - `docs/reference/api.md` renders the docstrings.
  - `_native.pyi` docstrings come from `cpp/binding.cpp`.
- **`docs/llms.txt`** is a hand-written summary for coding agents, published
  verbatim at the doc-site root (`/llms.txt`). It isn't generated, and
  `docs-build` won't flag it as stale. Keep it in sync when any of these change:
  - the public API (`__all__`)
  - the generate → facade → `walk` workflow
  - the install steps
  - the doc page set or `site_url`
- Don't change the facade banner `# GENERATED by antlrope — do not edit by hand.`
  in `cli/metadata.py`. `regen` and `up-to-date` match it in existing facades.
- Never commit `.idea/`. Commit only when explicitly asked, never push without
  asking, and never skip git hooks.

## Writing style (docs, docstrings, help text, comments)

Write plain, natural technical English, as a careful senior engineer would. A
review in September 2026 removed these recurring problems; don't reintroduce them:

- **Em-dash chains.** Don't join clauses with "—". Use a period, a colon, a
  semicolon, "because", "so", or parentheses. Write list items as
  `[link]: description`, not `[link] — description`. A single dash for a real
  aside is acceptable but rare.
- **Slang and folksy idioms.** Avoid words like "bake in" or "baked-in" (say "the
  facade's lexer and parser"), "poke", "claw back", "in disguise", "escape hatch",
  "choke point", "footgun", "knob", "reach for", "flatters", or "lands well
  short". Use the plain verb.
- **Slash and "+" compounds in prose.** Write "lexer and parser", "rules and
  tokens", "enter and exit", "parse and walk". Slashes are fine in code, tables,
  and fixed terms such as I/O.
- **Compressed noun stacks.** Unpack phrases like "the per-character lexer read
  path" into a clause.
- **Emphasis.** Use bold sparingly and italics rarely. The exception is the
  product name: **Antlrope** is bold on its first mention in a page or section,
  and plain capitalized afterwards. The lowercase, backticked `antlrope` is only
  for the module, the command, or the package id (see `CONTRIBUTING.md`, "Naming
  the product").
- **One idea per sentence.** Don't nest parentheticals inside asides. A docstring
  summary line states one thing.
- **Numbers must agree.** A performance figure should match its source table or
  benchmark and say which workload it measured. When you change a number, grep for
  the other places that quote it (README, `docs/`, `llms.txt`, docstrings, and
  chart alt text).
