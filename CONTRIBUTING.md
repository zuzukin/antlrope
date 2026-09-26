# Contributing

Thanks for your interest in improving `antlrope`. This page covers the
local development setup; for what the package does and how to use it, see the
[README](README.md) and the [docs](docs/index.md).

## Development environment

This project uses [pixi](https://pixi.sh). The toolchain (C++ compiler, CMake,
Ninja, nanobind, scikit-build-core) comes from the pixi environment. CMake and Ninja
must be on `PATH` because the editable install uses no build isolation and
`pixi run build` invokes them; the pixi environment provides both.

```sh
pixi install            # solve + build the editable extension
pixi run build          # rebuild the _native C++ extension after editing C++
pixi run test           # run the pytest suite
pixi run example        # run the JSON reconstruction example
pixi run docs-serve     # preview the docs site at http://localhost:8000
pixi run docs-build     # build the static docs site into site/
```

## Environments

- **default**: the Python build and test toolchain (no JDK); `pixi run test` runs
  here.
- **gen**: adds `openjdk` and the ANTLR tool, kept separate from the runtime
  environments. Regenerate the example from the grammar with `pixi run gen-json`
  (which reruns the ANTLR Python target on `examples/json/JSON.g4`) and
  `pixi run gen-facade`.
- **docs**: the [Zensical](https://zensical.org) static site generator. This
  environment has no default feature, so building the docs installs neither the JDK
  nor the C++ toolchain. Configured by `zensical.toml`; output goes to `site/` (gitignored).

## The native extension and its type stub

The C++ engine is the nanobind module `antlrope._native`, built from
`cpp/binding.cpp` against the vendored runtime. The C++ rarely changes, so it is
**not** rebuilt on import (`editable.rebuild = false`). After editing anything
under `cpp/` or `vendor/antlr4-cpp/`, recompile explicitly:

```sh
pixi run build      # incremental cmake build + install (scripts/build_native.py)
```

Then `pixi run test` runs against the fresh build. Because importing the package
never invokes the build toolchain, it imports from any interpreter and does not need
`cmake` on `PATH`. This includes an IDE that uses `.pixi/envs/default` without
activating it, such as PyCharm configured with the environment prefix as a plain
interpreter.

`_native` is a compiled module loaded through scikit-build-core's editable
redirector, which IDEs and type checkers cannot follow. A checked-in stub,
`src/antlrope/_native.pyi`, gives them the interface (and the `py.typed`
marker advertises the package as typed). If you change the **public interface** of
`cpp/binding.cpp` (add or rename a class, method, or function, or change a
signature), rebuild, then regenerate the stub:

```sh
pixi run build
pixi run stubgen
```

`pixi run stubgen` runs `scripts/stubgen.py`, which invokes nanobind's stubgen and
then re-applies the edits nanobind cannot infer (the license header and the
`parse_events` and `lex` return types), so the committed stub is produced directly.
To change those edits, edit `scripts/stubgen.py`, not the `.pyi`.

## Naming the product

In prose, the product is **Antlrope** (capitalized). Render it **bold on the first
mention in a page or section** and plain capitalized (`Antlrope`) afterwards. Reserve
the lowercase, backticked `` `antlrope` `` form for the literal Python module
(`from antlrope import …`), the CLI command (`antlrope gen`), or the package id in an
install command (`pip install antlrope`). Headings, the site/README title, URLs, the
logo wordmark, and code stay lowercase.

## Docstrings

Docstrings are rendered into the API docs by mkdocstrings, which resolves
cross-references written as `[title][path.to.symbol]` (e.g.
`[walk_parallel][antlrope.FacadeListener.walk_parallel]`).

**Do not wrap the cross-reference title in backticks.** Write `[title][ref]`, not
`` [`title`][ref] ``. Backticks render the title as inline code, which visually
hides that the text is a clickable link. The reference still resolves, but
readers can't tell it's a link. Plain `[title][ref]` renders as a normal styled
link. Backticks are still correct for inline code that is *not* a cross-reference
(a parameter or type name with no `][ref]` after it).

## `llms.txt`

`docs/llms.txt` is a hand-written, LLM-oriented summary of the library (install, the
generate → facade → `walk` workflow, the listener model, the public API, and doc
links). It is published verbatim at the doc-site root (`/llms.txt`) for coding
agents; see [llmstxt.org](https://llmstxt.org/). It is **not generated**, so keep it in sync when
any of these change: the public API (`__all__`), the workflow or listener model,
install instructions, the supported Python/platform versions, or the doc page set and
`site_url` (its doc links are absolute). It is excluded from the rendered docs (no
nav, search, or sitemap entry), so `pixi run docs-build` will **not** flag it as
stale.

## CLI reference

The command reference in `docs/reference/cli.md` (the region between the
`gen-cli-help` markers) is **generated from the live `antlrope --help` output** by
`scripts/gen_cli_docs.py`. After any change to the CLI in `antlrope.cli` (a
command's arguments or help text, or adding or renaming a subcommand), regenerate it:

```sh
pixi run gen-cli-docs
```

The prose outside the markers (the intro and the origin-header section) is hand-written.
`tests/test_cli_docs.py` runs `gen-cli-docs --check`, so `pixi run test` fails if the
committed doc has drifted from the CLI.

## Version

The version lives in one place: `src/antlrope/VERSION`. The build reads it
(scikit-build-core's regex metadata provider) so the wheel and its
`importlib.metadata` follow it, and `antlrope.__version__` reads the same
file via `importlib.resources`. The conda recipe is the exception: it tracks the
*published* release it packages, not the dev version, so it pins its own value
(bumped per release).

**Policy: bump the version once per release cycle, and record every user-visible
change in the changelog.** After a release, a "Start X.Y.Z development" commit
sets `VERSION` to the next version and opens an `## [X.Y.Z] - Unreleased` section
in [CHANGELOG.md](CHANGELOG.md) (see [RELEASING.md](RELEASING.md)). After that,
every commit that changes runtime behavior (`src/`, `cpp/`, `vendor/antlr4-cpp/`)
or user-facing docs (`README.md`, `docs/`, docstrings, CLI help) adds an entry to
that section, without changing `VERSION`. Build-only, test-only, or dev-tooling
changes (pixi or CMake config, `scripts/`, `CONTRIBUTING.md`, CI) don't need an
entry.

Editing `VERSION` is enough to change the version: `__version__` reflects it
immediately. Run `pixi install` to update the installed package metadata as well.

## Vendored runtime

The vendored ANTLR C++ runtime is built from `vendor/antlr4-cpp/`; see
`vendor/antlr4-cpp/UPDATING.md` to refresh the snapshot.

## AI-assisted contributions

Using an AI coding assistant is welcome; much of this project was written in
close collaboration with one (see the
[acknowledgements](docs/about/acknowledgements.md)). Two expectations:

- **Disclose the model in the PR description** (e.g. "written with Claude
  Opus 4.8").
- Ideally, commits the agent makes should carry a `Co-Authored-By:` trailer
  naming the model. Most agents add this automatically when they make the
  commit themselves.

You remain the author: review, test, and understand what you submit. The
"Before submitting" checklist below applies to AI-written changes exactly as it
does to hand-written ones.

## Before submitting

- If you changed C++ (`cpp/` or `vendor/antlr4-cpp/`), run `pixi run build` first.
- If you changed `cpp/`, run `pixi run format-cpp` (clang-format, configured in
  `.clang-format`, which puts one argument per line when a call or declaration does
  not fit on one line).
- Run `pixi run test` and make sure the suite is green.
- If you changed `cpp/binding.cpp`'s public interface, run `pixi run stubgen` (it
  re-applies the hand edits automatically).
- If you changed the CLI (`antlrope.cli`), run `pixi run gen-cli-docs` to regenerate
  the `docs/reference/cli.md` command reference (`pixi run test` fails if it's stale).
- If you changed the docs, confirm `pixi run docs-build` succeeds.
- If you changed the public API, the workflow, or the doc page set, update
  `docs/llms.txt` to match (it's hand-maintained and not flagged by `docs-build`).

## Releasing

Releases are cut from `main` via a `vX.Y.Z` tag; see [RELEASING.md](RELEASING.md)
for the full procedure (CHANGELOG, version bump, wheels/PyPI, docs deploy,
conda-forge).
