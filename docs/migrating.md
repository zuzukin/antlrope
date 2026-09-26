# Migrating from antlr4-python3-runtime

**Antlrope** does not replace the official runtime. You still generate your
parser with the stock tool, and the generated modules still import
`antlr4-python3-runtime`. What changes is how you consume the parse. This
page maps the official `ParseTreeListener` model onto the [facade].

## Callback shape

| official `ParseTreeListener` | facade `<Grammar>EventListener` |
|---|---|
| `enterEveryRule(ctx)` / `enter<Rule>(ctx)` | `enter<Rule>(self)` (no `ctx`) |
| `exitEveryRule(ctx)` / `exit<Rule>(ctx)` | `exit<Rule>(self)` (no `ctx`) |
| `visitTerminal(node)` | `visitTerminal(self, token_type, text)` |
| `visitErrorNode(node)` | `visitError(self, token_type, text)` |

The main difference is that there are no node or context objects. Callbacks
receive no parse-tree handles, because no Python [parse tree] is built. You
reconstruct whatever state you need from the order of the enter, exit, and terminal
events, almost always with an explicit stack.

## Getting token text

Official:

```python
def visitTerminal(self, node):
    text = node.getSymbol().text
```

Facade:

```python
def visitTerminal(self, token_type, text):
    ...  # text is already the sliced source for this token
```

The runtime slices `text[start : stop + 1]` for you, so there is no `Token`
object. Position information is still available. Inside any callback,
`self.line_col()` returns the current event's `(line, column)` and `self.span()`
returns its raw `(start, stop)` character offsets. See
[`FacadeListener.line_col`](reference/api.md#antlrope.FacadeListener.line_col) and
[`span`](reference/api.md#antlrope.FacadeListener.span).

## Handling errors

Where official code overrides `visitErrorNode(node)`, the facade provides
`visitError(self, token_type, text)`, which is called for each error node the
parser produces during error recovery. Together with `self.line_col()`, this is
enough to report a parse failure:

```python
def visitError(self, token_type, text):
    pos = self.line_col()
    where = f"{pos[0]}:{pos[1]}" if pos else "?"
    print(f"{where}: unexpected {text!r}")
```

Error events always reach Python, even when terminals are filtered, so you can
handle errors without receiving every token.

The official runtime also installs a `ConsoleErrorListener` that prints
`line X:Y ...` to `stderr` unless you call `removeErrorListeners()`. The facade
removes it for you, so nothing is printed. After `walk`, the diagnostics are in
`self.syntax_errors`, a list of `ParseError` exceptions. Each carries ANTLR's
message and the `line`, `column`, `start`, and `stop` of the error:

```python
listener.walk(source_text)
for err in listener.syntax_errors:
    print(f"{err.line}:{err.column}: {err.message}")
```

## Identifying rules and tokens

Official code often branches on `ctx.getRuleIndex()` or token types from the
generated parser. The facade gives you a named callback for each rule
(`enterObj`, `exitPair`, …), so you usually don't need to branch: you override the
callbacks for the rules you care about. For terminals, compare `token_type` against the
generated token-type constants on the facade class (`MyListener.STRING`, etc.).

## A worked example

`examples/json/to_python.py` rebuilds a JSON document as native Python objects
using only `enterObj`, `exitObj`, `enterArr`, `exitArr`, `enterPair`, and
`visitTerminal`, with a small value stack. It is the reference example of
converting a tree-walking listener to the event-stream model.

## When to keep the pure-Python runtime

Prefer the official `antlr4-python3-runtime` (not this package) when:

- Your grammar uses [semantic predicates][semantic predicate] or
  [embedded target-language actions][embedded action]. The interpreted [ATN]
  cannot execute them (see
  [Performance & limitations](performance.md)).
- You need to keep the parse tree itself (for random access, XPath, rewriting,
  or re-walking) rather than make a single streaming pass.
- You need rich per-node context (`Token` objects, parent and child navigation)
  during the walk and can't reconstruct it from event order.
- Your inputs are small enough that the per-node [FFI] cost doesn't matter. The
  facade's advantage is throughput on large inputs.

[facade]: glossary.md#facade
[parse tree]: glossary.md#parse-tree
[semantic predicate]: glossary.md#semantic-predicate
[embedded action]: glossary.md#embedded-action
[ATN]: glossary.md#atn
[FFI]: glossary.md#ffi
