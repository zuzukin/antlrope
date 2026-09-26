# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and from 1.0 the project
follows semantic versioning (see the version policy in
[CONTRIBUTING.md](CONTRIBUTING.md)).

## [1.0.2] - Unreleased

### Changed
- Rewrote the prose of the README, the documentation site, `llms.txt`, docstrings,
  CLI help text, and code comments in plainer, more natural English. Technical
  content is unchanged.
- The `antlrope check` success line now reads `OK: no …` instead of `OK — no …`.

### Fixed
- Documentation errors: the getting-started `antlrope gen` command used a slash
  path instead of a dotted module path; the JSON example page called a class that
  does not exist; the README described rule events as having no span; several
  broken links and anchors.
- Docstrings and help text that disagreed with the code: `FacadeListener.sourcename`
  (`walk` takes no `sourcename`), the `stream_on_pattern` `sourcename` default,
  `LineCol.add`, the undocumented `text` argument of `walk`, and the exit
  statuses of `antlrope check` and `antlrope up-to-date`.
- Performance figures that disagreed across pages, re-measured where needed: the
  lock-free DFA patch speeds up lexing about 1.6–1.7× and a whole JSON parse about
  1.2× (not 1.3–1.4×); regex chunking is about 7× faster than lexer chunking at
  finding delimiters (not "an order of magnitude"); the SystemRDL speedup is
  ~20–23× on the large input; and the docs now say `walk_parallel` uses per-thread
  specs, so it does not depend on the per-DFA-lock patch.

## [1.0.1] - 2026-07-25

### Added
- README badges: CI status, coverage (Codecov), PyPI release, supported Python
  versions, conda-forge release, and a docs link.

### Fixed
- The README's repo-relative image and link URLs are now rewritten to absolute
  URLs at package build time (hatch-fancy-pypi-readme), so the logo, benchmark
  chart, and doc links render on the PyPI project page; the repo file keeps the
  relative paths GitHub renders natively.
- The vendored ANTLR4 C++ runtime's `DFASerializer` printed DFA-dump edge
  labels shifted by one token type (an offset missed by the lock-free
  DFA-edge patch, caught by the upstream testsuite on antlr/antlr4#4953).
  Diagnostic output only — parse behavior and the event stream were
  unaffected. Both patches are now proposed upstream as antlr/antlr4#4953
  and #4954.

## [1.0.0] - 2026-07-08

First stable release. **Antlrope** is a fast, C++-accelerated ANTLR runtime for
Python: it drives the official ANTLR4 C++ runtime's ATN interpreter directly from
the serialized ATN that the stock `-Dlanguage=Python3` ANTLR tool already emits —
no per-grammar codegen, nothing to compile — and delivers the parse to Python as
one bulk, filtered event stream instead of a per-node tree walk (typically 10–20×
the official pure-Python runtime). The public API — `FacadeListener` and its
generated `<Grammar>EventListener` facades, `walk` / `walk_parallel`, the chunking
and streaming family, and the `antlrope` command group — is stable as of this
release.

Final pre-release polish (over the 0.x history below):

### Changed
- The CLI prints a clean one-line error (exit status 2) for bad input — an
  unimportable module, a module that is not a generated parser, an unreadable or
  unwritable file — instead of a Python traceback.
- `FacadeListener.rule_name` now raises `IndexError` for any out-of-range rule
  index; previously a negative index silently wrapped from the end.

### Added
- Release engineering: complete PyPI metadata (development-status classifier,
  documentation/changelog/issue URLs), a documented release procedure
  ([RELEASING.md](RELEASING.md)), automated versioned-docs deployment on release
  tags, and a Windows CI job running the full test suite (Windows wheels were
  previously only import-smoke-tested).

## 0.x development history

The 0.x versions (0.1.0 2026-05-31 through 0.2.28 2026-06-28) were development
builds, never published to PyPI. Condensed highlights, roughly in order:

- **Engine** (0.1.0): the bulk filtered event stream over a vendored ANTLR4 C++
  runtime — parse in C++, cross into Python once with `(kind, payload, start,
  stop)` int32 records, filter unsubscribed rules/tokens natively; GIL released
  during the parse; `py.typed` + a checked-in `_native` stub. Two runtime patches
  written for upstream: lock-free DFA-edge reads and per-DFA write locks (shared
  specs scale across threads instead of running slower than serial). A
  heap-buffer-overflow in the patched DFA-edge cache was found and fixed under
  AddressSanitizer, which the CI now runs routinely.
- **Facade**: generated `<Grammar>EventListener` classes with per-rule callbacks,
  token-type constants (symbolic plus ANTLR's positional `T__n` literal names),
  and baked-in lexer/parser — `listener.walk(text)` needs no class arguments.
  `walk_parallel` parses chunks across cores as a lazy, order-preserving iterator.
  Consumer helpers grew in: `depth` / `rule_stack` / `current_rule`, `text`,
  `token_name` / `rule_name`, `line_col` / `span` / `sourcename`, collected
  `syntax_errors` (now `ParseError` exceptions), and the
  `enterEveryRule` / `exitEveryRule` hooks.
- **Chunking family**: token-aware `split_on_token` / `split_between_tokens` over
  a C++ `lex` pass; regex `split_on_pattern` / `chunk_by_pattern`; structural
  `chunk_by_rule`; and bounded-memory streaming counterparts `stream_on_token`,
  `stream_on_pattern`, and `stream_by_rule` (which fails loudly on malformed
  record sequences). All yield positioned `Chunk`s with optional `sourcename`;
  `trim=False` preserves whitespace terminators.
- **API consolidation**: the chunkers and spec handling moved onto
  `FacadeListener`; the low-level layer (event buffers, native specs, typing
  protocols, the `cached=` option) went private, leaving a seven-name public
  surface plus a `clear_cache()` escape hatch.
- **CLI**: grew from a single generator command into the `antlrope` command group
  — `gen` (with a deterministic origin header), `regen` and `up-to-date`
  (regenerate / staleness-check from that header), `check` (flag semantic
  predicates and embedded actions the ATN interpreter cannot run), and `rules` /
  `tokens` introspection.
- **Packaging**: renamed from `antlr-pyfacade` to **antlrope** (0.2.0); Python
  floor raised to 3.12 so a single Stable-ABI (`cp312-abi3`) wheel per platform
  covers 3.12+ (Linux x86_64/aarch64, macOS x86_64/arm64, Windows amd64).
- **Docs**: the zensical + mkdocstrings site (user guide, examples, glossary,
  generated API and CLI references), an LLM-oriented `llms.txt`, and the SystemRDL
  benchmark: ~21× the pure-Python runtime and ~8× the speedy-antlr accelerator
  end-to-end on a 2.6 MB input, at lower peak memory.
