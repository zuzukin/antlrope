# Getting started

This page takes you from a grammar to a working parser in three steps: generate a
parser from your grammar, generate a Python [facade], and write a listener subclass.

First, [install `antlrope` and the ANTLR tool](installation.md).

The examples below use a JSON grammar, which lives in the repository under
[`examples/json/`](https://github.com/zuzukin/antlrope/tree/dev/examples/json)
(see [Examples](examples/index.md) for the full programs). Swap in your own `.g4`
grammar and the steps are identical.

!!! warning "Before you start: will this work with your grammar?"

    **Antlrope** parses grammars that describe structure, such as data formats,
    configuration languages, and most DSLs and programming languages. It does **not**
    run [semantic predicates][semantic predicate] (`{...}?`) or [embedded actions][embedded action] (`{...}` code)
    that some grammars use, because they are target-language code that this runtime
    cannot execute. If your grammar depends on them, either write a version of the
    grammar that does not need them or use the official `antlr4-python3-runtime`. See
    [Performance & limitations](performance.md#limitation-semantic-predicates-and-embedded-actions).

    Not sure? After you generate a parser (step 1), run
    `antlrope check <parser-module>`. It reports every semantic predicate and
    embedded action, grouped by rule, and exits with status 0 if there are none.
    See [Command line](reference/cli.md#antlrope-check).

## 1. Generate a parser from your grammar

Run the stock ANTLR tool with the Python3 target. This is the ordinary ANTLR
workflow; nothing here is specific to Antlrope:

```sh
antlr4 -Dlanguage=Python3 JSON.g4 -o generated
```

This writes `generated/JSONLexer.py` and `generated/JSONParser.py` (plus a few
support files). These are the same files you'd use with the pure-Python
runtime.

## 2. Generate a facade

The *facade* is a small Python base class with one named callback per grammar
rule. Pass `antlrope gen` your generated parser module (as an importable dotted
path) and a name prefix:

```sh
antlrope gen generated.JSONParser JSON -o json_listener.py
```

That emits `json_listener.py` containing a `JsonEventListener` class with:

- an `enter`/`exit` pair of callbacks for every rule in the grammar (`enterJson` /
  `exitJson`, `enterObj` / `exitObj`, `enterPair` / …),
- `visitTerminal(self, token_type, text)` and `visitError(self, token_type, text)`,
- token-type constants like `STRING = 10`, `NUMBER = 11`, and
- a `walk(text)` method that runs everything.

The facade imports your lexer and parser and keeps references to them, so you
never pass them to `walk`. It locates the lexer module using ANTLR's
`<Grammar>Lexer` / `<Grammar>Parser` naming convention (here, `generated.JSONLexer`
next to `generated.JSONParser`). If your lexer module is named differently, pass
`--lexer` to `antlrope gen`.

Don't edit this file; subclass it instead.

!!! tip

    `antlrope rules <parser-module>` and `antlrope tokens <parser-module>` list
    the grammar's rule names and the facade's token-type constants, so you don't
    have to open the generated file. The rule names are the ones
    `walk(start_rule=...)` and the rule chunkers accept. See
    [Command line](reference/cli.md).

## 3. Write a listener

Subclass the generated facade and override only the callbacks you need. Here is a
complete program that collects every string token in a JSON document:

```python
from json_listener import JsonEventListener        # from step 2

class StringCollector(JsonEventListener):
    def __init__(self):
        self.strings = []

    def visitTerminal(self, token_type, text):
        if token_type == self.STRING:        # STRING is a generated constant
            self.strings.append(text)

c = StringCollector()
c.walk('{"name": "ada", "tags": ["x", "y"]}')
print(c.strings)
```

Running it prints every string literal, quotes included (the callback receives the
raw source slice for the token):

```text
['"name"', '"ada"', '"tags"', '"x"', '"y"']
```

That's the whole model:

- **Rule callbacks take no arguments.** There are no node objects, so you keep
  track of your own state. The common pattern is a small stack: push on `enter`, pop on `exit`.
  See [`examples/json/to_python.py`](https://github.com/zuzukin/antlrope/blob/dev/examples/json/to_python.py),
  which rebuilds a JSON document into Python objects using `enterObj` and `exitObj`,
  `enterArr` and `exitArr`, `enterPair`, and `visitTerminal`.
- **`visitTerminal(token_type, text)`** gives you the token's type (compare against
  the generated constants) and its text, already sliced from the source.
- **Override only what you need.** The C++ side sends Python only the events for
  callbacks you define, so fewer overrides mean a faster walk.

## Scope and source helpers

You don't have to track nesting by hand. While a callback runs, these helpers
describe the current position in the parse:

- `self.depth()`: the current nesting depth.
- `self.rule_stack()`: the names of the open rules, outermost first.
- `self.current_rule()`: the innermost open rule (inside `visitTerminal`, for
  example, the rule the token belongs to).

These helpers count only the rules you subscribe to. To track the full parse tree
instead, override `enterEveryRule` and `exitEveryRule`, which are no-op hooks
called for every rule.

- `self.text()`: the source text of the current event. For a token, this is the
  token text. For a rule, it is the rule's whole extent (for example, the full
  `{...}` in `exitObj`), so you don't need to keep the source and slice it with
  `span()` yourself.
- `self.token_name(t)` and `self.rule_name(i)`: the name of a token type or rule
  index, useful for logging or for a generic `visitTerminal`.

You need the "push on enter, pop on exit" stack from the JSON example only when you
are building a result. To find the current depth, the enclosing rules, or the
current text, use the helpers above.

## Handling parse errors

By default ANTLR prints `line X:Y ...` messages to stderr. The facade captures
them instead, so nothing is printed and you decide what to do. After `walk`, the
errors from that parse are on `self.syntax_errors`:

```python
c = StringCollector()
c.walk('{"a": 1 2}')     # the stray 2 is a syntax error
for err in c.syntax_errors:
    print(f"{err.line}:{err.column}: {err.message}")
```

```text
1:8: extraneous input '2' expecting {',', '}'}
```

`syntax_errors` is reset on every `walk`, and is an empty list after a clean
parse.

## Where to go next

- [Migrating from antlr4-python3-runtime](migrating.md): if you already have a
  `ParseTreeListener`, this page shows the facade equivalent of each part.
- [Parallel parsing](parallel-parsing.md): if your input consists of many
  independent pieces (records, definitions, sections), parse them concurrently
  with `walk_parallel`.
- [API reference](reference/api.md): every callback and `walk` option, the
  source-position helpers, and the raw event buffer for advanced use.
- [How it works](concepts.md): optional background on why batching events makes
  parsing fast.

[facade]: glossary.md#facade
[semantic predicate]: glossary.md#semantic-predicate
[embedded action]: glossary.md#embedded-action
