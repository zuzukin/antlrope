# Copyright 2026 Christopher Barber
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Example: index a schema file's type definitions in three ways.

A `.schema` file is a flat sequence of independent `message` and `enum`
definitions (see `Schema.g4` and `sample.schema`). This program builds an index of
every type's name, kind, and member names, and shows three ways to run the same
`SchemaEventListener` over the input:

  1. whole-file walk: one C++ parse of the entire file (the everyday path);
  2. parallel by rule: chunk on grammar structure, parse the pieces across cores;
  3. streaming by rule: the same, reading the file incrementally (bounded memory).

Which one to use:

  * For a file you can hold in memory with light per-record work, the single walk
    (1) is simplest and usually fastest, because it has no chunking overhead and
    one parse is cheaper than many.
  * Use (2) when the per-definition Python work is heavy enough that spreading it
    across cores wins despite the GIL on event dispatch (the native parses overlap).
  * Use (3) when the file is too large to hold in memory: it never materializes the
    whole source or token stream.

Each definition is self-contained and may span many lines, so splitting on newlines
or a regex does not work. However, each definition is exactly one `messageDef` or
`enumDef`, which lets `chunk_by_rule` and `stream_by_rule` split on the grammar's
structure.

Run from this directory:

    python index.py                    # index the bundled sample.schema
    python index.py path/to/file.schema   # index your own file (streamed)
    python index.py --benchmark 50000     # generate N definitions and time all three
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from schema_listener import SchemaEventListener

HERE = Path(__file__).resolve().parent


@dataclass
class TypeDef:
    """One indexed type: its kind, its name, and the names of its members."""

    kind: str  # "message" or "enum"
    name: str = ""
    members: list[str] = field(default_factory=list)


class SchemaIndexer(SchemaEventListener):
    """Collect a `TypeDef` for every message or enum definition walked.

    There are no node objects: state is reconstructed from the order of events, as
    in any antlrope listener. Here the scope helpers do most of the work.
    `enter{Message,Enum}Def` and `exit{Message,Enum}Def` bracket each definition.
    Inside it, `current_rule()` says which kind of `ID` we're looking at, so there
    is no `{`-seen flag and no field-name lookahead:

      * an `ID` directly under `messageDef` or `enumDef` is the type's own name
        or, for an `enum` (whose constants are bare IDs), one of its members;
      * an `ID` inside a `field` (`fieldType ID ';'`) is a message field's name;
      * an `ID` inside a `fieldType` is the field's type, which is not indexed.

    `enterEveryRule` (a no-op here) subscribes the listener to every rule, so
    `current_rule()` can see the inner `field` and `fieldType` scopes. Without it,
    the innermost subscribed rule would always be the message or enum definition.
    """

    def __init__(self) -> None:
        self.types: list[TypeDef] = []
        self._cur: TypeDef | None = None

    # Subscribe to all rules so current_rule() reflects the true innermost rule
    # (field or fieldType), not just the message and enum scopes we open below.
    def enterEveryRule(self, rule_index: int) -> None: ...

    # A definition starts: open a fresh TypeDef…
    def enterMessageDef(self) -> None:
        self._cur = TypeDef("message")

    def enterEnumDef(self) -> None:
        self._cur = TypeDef("enum")

    # …and ends: commit it.
    def exitMessageDef(self) -> None:
        self._commit()

    def exitEnumDef(self) -> None:
        self._commit()

    def _commit(self) -> None:
        if self._cur is not None:
            self.types.append(self._cur)
            self._cur = None

    def visitTerminal(self, token_type: int, text: str) -> None:
        cur = self._cur
        if cur is None or token_type != self.ID:
            return
        rule = self.current_rule()
        if rule == "field":
            cur.members.append(text)  # a message field name: `fieldType ID ';'`
        elif rule in ("messageDef", "enumDef"):
            if not cur.name:
                cur.name = text  # the definition's own name (the ID before `{`)
            else:
                cur.members.append(text)  # an enum constant (bare ID in the body)
        # rule == "fieldType": the field's declared type, which is not indexed.


# --- the three ways to run it -------------------------------------------------


def index_whole(text: str) -> list[TypeDef]:
    """One parse of the entire file (start rule `schema`). The everyday path."""
    return SchemaIndexer().walk(text, start_rule="schema").types


def index_parallel(text: str) -> list[TypeDef]:
    """Chunk on grammar structure, then parse the definitions across cores.

    `chunk_by_rule` parses the whole file once (in C++) to find each top-level
    `messageDef` or `enumDef` span; `walk_parallel` re-parses each span as a
    `definition` on a worker pool, yielding one listener per definition. This is
    worth it only when the per-definition Python work is heavy. Here it is tiny, so
    `index_whole` is usually faster.
    """
    chunks = SchemaIndexer.chunk_by_rule(text, {"messageDef", "enumDef"})
    out: list[TypeDef] = []
    for listener in SchemaIndexer.walk_parallel(chunks, start_rule="definition"):
        out.extend(listener.types)
    return out


def index_streaming(path: str | Path) -> list[TypeDef]:
    """Like `index_parallel`, but read the file incrementally (bounded memory).

    `stream_by_rule` opens the file in C++ and yields one definition at a time
    without holding the whole source or token stream. The candidate rules
    (`messageDef` and `enumDef`) start with different tokens (`message` and `enum`),
    so the next token selects which to parse. Use this for files too big to load.
    """
    chunks = SchemaIndexer.stream_by_rule(path, {"messageDef", "enumDef"})
    out: list[TypeDef] = []
    for listener in SchemaIndexer.walk_parallel(chunks, start_rule="definition"):
        out.extend(listener.types)
    return out


# --- reporting and a throwaway data generator ---------------------------------


def print_index(types: list[TypeDef]) -> None:
    for t in types:
        print(f"  {t.kind:7} {t.name:14} {{ {', '.join(t.members)} }}")
    n_msg = sum(t.kind == "message" for t in types)
    print(f"{len(types)} types  ({n_msg} message, {len(types) - n_msg} enum)")


def make_schema(n: int) -> str:
    """A throwaway schema of `n` definitions, for the --benchmark mode."""
    defs = []
    for i in range(n):
        if i % 5 == 0:
            defs.append(f"enum E{i} {{ A, B, C }}")
        else:
            defs.append(f"message M{i} {{ int a; string b; bool c; }}")
    return "\n".join(defs)


def benchmark(n: int) -> None:
    text = make_schema(n)
    print(f"{n:,} definitions, {len(text) / 1e6:.1f} MB\n")

    def timed(label: str, fn) -> list[TypeDef]:
        t0 = time.perf_counter()
        types = fn()
        print(f"  {label:20} {(time.perf_counter() - t0) * 1e3:8.1f} ms")
        return types

    whole = timed("whole-file walk", lambda: index_whole(text))
    parallel = timed("parallel by rule", lambda: index_parallel(text))
    tmp = HERE / "_bench.schema"
    tmp.write_text(text, encoding="utf-8")
    try:
        streamed = timed("streaming by rule", lambda: index_streaming(tmp))
    finally:
        tmp.unlink()
    assert len(whole) == len(parallel) == len(streamed) == n  # all three agree
    print(
        "\nFor light per-definition work like this, the single walk usually wins; "
        "parallel and streaming pay off on huge files or heavy per-definition work."
    )


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--benchmark":
        benchmark(int(argv[2]) if len(argv) > 2 else 50_000)
    elif len(argv) >= 2:
        # A user-supplied file: stream it, so it works regardless of size.
        print_index(index_streaming(argv[1]))
    else:
        # No args: index the bundled sample with the everyday whole-file walk.
        print_index(index_whole((HERE / "sample.schema").read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
