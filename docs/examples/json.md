# JSON: reconstruct a value

This example rebuilds a parsed JSON document as native Python objects: the dicts,
lists, and scalars you would get from `json.loads`. It builds them from the event
stream, so you can see exactly how a consumer is written. The full program is
[`examples/json/to_python.py`](https://github.com/zuzukin/antlrope/blob/dev/examples/json/to_python.py);
it is the complete version of the `StringCollector` from
[Getting started](../getting-started.md).

## The grammar

The example uses a standard, target-agnostic JSON grammar
([`examples/json/JSON.g4`](https://github.com/zuzukin/antlrope/blob/dev/examples/json/JSON.g4)).
It has no [embedded actions][embedded action] or [semantic predicates][semantic predicate],
so the interpreted [ATN] parses it correctly. The rules that matter to the consumer are `obj`, `arr`, `pair`, and
`value`:

```antlr
obj   : '{' pair (',' pair)* '}' | '{' '}' ;
pair  : STRING ':' value ;
arr   : '[' value (',' value)* ']' | '[' ']' ;
value : STRING | NUMBER | obj | arr | 'true' | 'false' | 'null' ;
```

Generate the parser and the [facade] (already checked in):

```sh
antlr4 -Dlanguage=Python3 JSON.g4 -o generated
antlrope gen generated.JSONParser JSON -o json_listener.py
```

The facade, `JsonEventListener`, has an `enter` and `exit` callback for each rule,
a `visitTerminal`, and token-type constants. The literal tokens `'true'`, `'false'`,
and `'null'` have no names in the grammar, so ANTLR gives them the positional names
`T__6`, `T__7`, and `T__8`. Their token types are 7, 8, and 9; the number in the
name is not the token type. `STRING` and `NUMBER` are named in the grammar.

## The listener

The consumer keeps a **stack of partially built containers**. There are no node
objects; you reconstruct the structure from the order of events. Entering an `obj`
or `arr` pushes a new container. Scalars and nested containers that arrive before
the matching exit are attached to it, and the exit pops it:

```python
class JsonValueBuilder(JsonEventListener):
    def __init__(self) -> None:
        self._stack: list = []      # open containers, innermost last
        self._keys: list = []       # pending object keys
        self._expect_key = False
        self.result = _MISSING

    def enterObj(self) -> None:  self._push({})
    def exitObj(self) -> None:   self._pop()
    def enterArr(self) -> None:  self._push([])
    def exitArr(self) -> None:   self._pop()
    def enterPair(self) -> None: self._expect_key = True   # next STRING is a key

    def visitTerminal(self, token_type: int, text: str) -> None:
        if token_type == _STRING:
            value = json.loads(text)            # unquote + unescape
            if self._expect_key:
                self._keys.append(value)
                self._expect_key = False
                return
        elif token_type == _NUMBER:  value = json.loads(text)
        elif token_type == _TRUE:    value = True
        elif token_type == _FALSE:   value = False
        elif token_type == _NULL:    value = None
        else:
            return                              # structural punctuation: { } [ ] : ,
        self._attach(value)
```

Two things worth noting:

- **Only the callbacks you define cross into Python.** `JsonValueBuilder` overrides
  `enterObj`, `exitObj`, `enterArr`, `exitArr`, `enterPair`, and `visitTerminal`, so
  the C++ side never sends `enterValue`, `enterJson`, and so on. The fewer node kinds
  you subscribe to, the less work crosses the boundary.
- **Token text is recovered by slicing.** `visitTerminal` receives the token's type
  and its exact source text; no node objects or per-node [foreign-function][FFI] calls are
  involved.

`_attach` puts a finished value where it belongs: into the open list, under the
pending object key, or (at the top level) as the final `result`. See the
[full file](https://github.com/zuzukin/antlrope/blob/dev/examples/json/to_python.py)
for `_push`, `_pop`, and `_attach`.

## Running it

```sh
$ cd examples/json
$ python to_python.py '{"a": [1, true, null], "b": "hi"}'
{'a': [1, True, None], 'b': 'hi'}
```

The whole parse runs in C++, and one bulk, filtered event stream drives the
callbacks above. For a single document, this is the usual approach:
`JsonValueBuilder().walk(text)`. If you have many independent documents, or one very
large one, the next example shows how to chunk them and parse the chunks in parallel.

[embedded action]: ../glossary.md#embedded-action
[semantic predicate]: ../glossary.md#semantic-predicate
[ATN]: ../glossary.md#atn
[facade]: ../glossary.md#facade
[FFI]: ../glossary.md#ffi
