# Acknowledgements

**Antlrope** builds on the following open-source projects:

- **[ANTLR](https://www.antlr.org/)** (Terence Parr and contributors): the parser
  generator, its serialized-ATN format, and the ANTLR4 C++ runtime that this package
  vendors and drives.
- **[nanobind](https://github.com/wjakob/nanobind)** (Wenzel Jakob): the binding layer between
  C++ and Python that the native extension is built with.
- **[antlr4-python3-runtime](https://pypi.org/project/antlr4-python3-runtime/)**:
  the official Python runtime that the generated parser modules subclass.
- **[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) and
  [Zensical](https://zensical.org/)** (the Material team): the documentation theme
  and site generator, with API pages rendered by
  [mkdocstrings](https://mkdocstrings.github.io/).

Much of Antlrope's implementation, tests, and documentation was written in
close collaboration with [Claude Opus 4.8](https://www.anthropic.com/claude/opus) (Anthropic),
via [Claude Code](https://www.anthropic.com/claude-code).

## Links

- **Source:** [github.com/zuzukin/antlrope](https://github.com/zuzukin/antlrope)
- **PyPI:** [pypi.org/project/antlrope](https://pypi.org/project/antlrope/)
- **ANTLR documentation:** [github.com/antlr/antlr4/blob/master/doc/index.md](https://github.com/antlr/antlr4/blob/master/doc/index.md)
