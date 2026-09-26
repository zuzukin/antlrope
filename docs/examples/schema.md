# Schema: parallel & streaming indexing

This example indexes a **schema file**: a flat sequence of independent type
definitions, shaped like a protobuf, Thrift, or Cap'n Proto IDL. It also shows how
to scale the same listener from one document to a very large file. The full program is
[`examples/schema/index.py`](https://github.com/zuzukin/antlrope/blob/dev/examples/schema/index.py).

It complements the [JSON example](json.md). There, one document drives one walk;
here, many independent records feed a pipeline that chunks and then parses them.

## The grammar

[`Schema.g4`](https://github.com/zuzukin/antlrope/blob/dev/examples/schema/Schema.g4)
defines a file as a sequence of `message` (struct) and `enum` definitions:

```antlr
schema      : definition* EOF ;
definition  : messageDef | enumDef ;

messageDef  : MESSAGE ID LBRACE field* RBRACE ;   // message Point { int x; int y; }
field       : fieldType ID SEMI ;
fieldType   : INT | STRING | BOOL | ID ;

enumDef     : ENUM ID LBRACE (ID (COMMA ID)*)? RBRACE ;   // enum Color { RED, GREEN }
```

Two properties make this a good fit for structural chunking:

- Each definition is **self-contained**: it parses on its own, independent of the
  others.
- A definition may **span many lines**, so splitting on newlines or a regular
  expression would cut it in the wrong place. However, each definition is exactly
  one `messageDef` or one `enumDef`, and
  [`chunk_by_rule`](../chunking.md#rule-based-split-on-grammar-structure) and
  `stream_by_rule` split on exactly those rules.

The keywords and punctuation get named lexer rules (`MESSAGE`, `LBRACE`, …), so the
[facade] exposes readable constants (`self.MESSAGE`, `self.ID`) instead of positional
`T__n`.

## The listener

`SchemaIndexer` collects one `TypeDef` (kind, name, member names) per definition.
As in the JSON example, there are no node objects, and state is reconstructed from
the order of events. Here, though, the scope helpers do most of the work. Each
definition starts with `enterMessageDef` or `enterEnumDef` and ends with the matching
exit callback. Inside it, `current_rule()` tells you which kind of `ID` you are
looking at, so you need neither a flag for having seen `{` nor lookahead for field
names:

```python
class SchemaIndexer(SchemaEventListener):
    def __init__(self) -> None:
        self.types: list[TypeDef] = []
        self._cur: TypeDef | None = None

    # Subscribe to every rule so current_rule() sees the inner field / fieldType
    # scopes, not just the message/enum we act on. The hook itself does nothing.
    def enterEveryRule(self, rule_index: int) -> None: ...

    def enterMessageDef(self) -> None: self._cur = TypeDef("message")
    def enterEnumDef(self) -> None:    self._cur = TypeDef("enum")
    def exitMessageDef(self) -> None:  self._commit()
    def exitEnumDef(self) -> None:     self._commit()

    def visitTerminal(self, token_type: int, text: str) -> None:
        cur = self._cur
        if cur is None or token_type != self.ID:
            return
        rule = self.current_rule()
        if rule == "field":                    # message field: `fieldType ID ';'`
            cur.members.append(text)
        elif rule in ("messageDef", "enumDef"):
            if not cur.name: cur.name = text           # the definition's name…
            else:            cur.members.append(text)  # …then enum constants
        # rule == "fieldType": the field's declared type — not indexed.
```

`current_rule()`, like `depth()` and `rule_stack()`, reflects only the rules you
subscribe to. Overriding the no-op `enterEveryRule` subscribes to all of them, so the
inner `field` and `fieldType` scopes become visible.

The same listener works whether it walks the whole file (accumulating every
definition into `self.types`) or a single definition (one entry). This is what lets
all three modes below share it.

## Three ways to run it

### 1. One whole-file walk — the everyday path

```python
def index_whole(text: str) -> list[TypeDef]:
    return SchemaIndexer().walk(text, start_rule="schema").types
```

This runs one C++ parse of the entire file. When the input fits in memory and the
per-record work is light, this is the simplest option and usually the fastest: there
is no chunking overhead, and one parse is cheaper than many.

### 2. Chunk by rule, parse across cores

```python
def index_parallel(text: str) -> list[TypeDef]:
    chunks = SchemaIndexer.chunk_by_rule(text, {"messageDef", "enumDef"})
    out: list[TypeDef] = []
    for listener in SchemaIndexer.walk_parallel(chunks, start_rule="definition"):
        out.extend(listener.types)
    return out
```

[`chunk_by_rule`](../chunking.md) parses the file once in C++ to find the span of
each top-level `messageDef` and `enumDef`; passing a **set** of rules indexes both
kinds. [`walk_parallel`](../parallel-parsing.md) then re-parses each span as a
`definition` on a worker pool, yielding one listener per definition. The native
parses release the [GIL] and overlap across cores.

### 3. Stream by rule — bounded memory

```python
def index_streaming(path: str | Path) -> list[TypeDef]:
    chunks = SchemaIndexer.stream_by_rule(path, {"messageDef", "enumDef"})
    out: list[TypeDef] = []
    for listener in SchemaIndexer.walk_parallel(chunks, start_rule="definition"):
        out.extend(listener.types)
    return out
```

`stream_by_rule` opens the file in C++ and yields **one definition at a time**
without ever holding the whole source or token stream in memory. The candidate rules
start with different tokens (`message` and `enum`), so the next token selects which
rule to parse. Combined with `walk_parallel`, which pulls chunks lazily, the whole
pipeline of reading, chunking, and parsing uses bounded memory regardless of file
size. This lets you index a schema much larger than available RAM.

## Which one to use

Run the bundled sample to see the index:

```sh
$ cd examples/schema && python index.py
  enum    Color          { RED, GREEN, BLUE }
  message Point          { x, y }
  message Pixel          { at, color, alpha }
  ...
5 types  (3 message, 2 enum)
```

`python index.py --benchmark 50000` generates 50k definitions and times all three.
Here, the single whole-file walk is the fastest, and that is the useful lesson:

- **Use chunking and `walk_parallel` when parsing each record is the expensive
  part.** The speedup comes from running the native parses in parallel with the GIL
  released, so it grows with the cost of parsing the grammar. This schema is trivial
  to parse, so the chunking overhead dominates and the single walk is faster. For a
  grammar that is expensive to parse, the result is reversed. (For scale, the
  [SystemRDL benchmark](../benchmarks/systemrdl.md) shows a ~20× speedup over the
  pure-Python runtime for a whole-file parse.)
- **Use streaming when the file does not fit in memory.** The benefit of
  `stream_by_rule` is bounded memory, not speed: it lets you index a 10 GB schema on
  a laptop.
- **Otherwise, walk the whole file.** Don't chunk for its own sake.

See [Performance & limitations](../performance.md#chunking-lexer-vs-regex) for the
measured costs and trade-offs of the chunking methods.

[facade]: ../glossary.md#facade
[GIL]: ../glossary.md#gil
