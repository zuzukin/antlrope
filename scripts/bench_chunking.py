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

"""Benchmark: lexer- vs regex- vs rule-based chunking.

Generates a JSON array of many flat objects (no nested braces, no braces inside
strings) so all three chunkers produce the *same* N object chunks, and compares
the cost of producing them, both for the underlying scan or parse and for the full
chunking.
Run with:

    pixi run python scripts/bench_chunking.py
"""

from __future__ import annotations

import importlib
import re
import sys
import time
from pathlib import Path
from typing import Any

from antlrope import _native

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "json"))
_jl: Any = importlib.import_module("generated.JSONLexer")
_jp: Any = importlib.import_module("generated.JSONParser")
_jlist: Any = importlib.import_module("json_listener")
JSONLexer = _jl.JSONLexer
JSONParser = _jp.JSONParser
JsonEventListener = _jlist.JsonEventListener
LBRACE = JSONLexer.T__0  # '{'
RBRACE = JSONLexer.T__2  # '}'
OBJ = list(JSONParser.ruleNames).index("obj")
_RECORD = r"\{[^{}]*\}"  # one flat object


def make_input(n: int) -> str:
    objs = (f'{{"id": {i}, "name": "item-{i}", "value": {i * 1.5}}}' for i in range(n))
    return "[" + ", ".join(objs) + "]"


def best(fn: Any, repeat: int = 5) -> float:
    """Best (minimum) wall-clock seconds over `repeat` runs of `fn`."""
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return min(times)


def bench_one(n: int) -> None:
    text = make_input(n)
    mb = len(text) / 1e6
    parser_spec, lexer_spec = (
        JsonEventListener._parser_spec(),
        JsonEventListener._lexer_spec(),
    )

    # All three families must produce the same object chunks.
    by_tok = [
        c.text for c in JsonEventListener.split_between_tokens(text, (LBRACE, RBRACE))
    ]
    by_rx = [c.text for c in JsonEventListener.chunk_by_pattern(text, _RECORD)]
    by_rule = [c.text for c in JsonEventListener.chunk_by_rule(text, "obj")]
    assert by_tok == by_rx == by_rule, "chunkers disagree"

    print(f"\n{n:,} objects, {mb:.1f} MB, {len(by_rule):,} chunks")
    rows: list[tuple[str, Any]] = [
        # underlying scan / parse stage
        ("regex finditer", lambda: list(re.finditer(_RECORD, text))),
        (
            "lex (tokenize)",
            lambda: list(JsonEventListener.lex(text, keep=[LBRACE, RBRACE])),
        ),
        (
            "rule_spans (parse)",
            lambda: _native.rule_spans(parser_spec, lexer_spec, text, 0, [OBJ], True),
        ),
        # full chunking
        (
            "chunk_by_pattern",
            lambda: list(JsonEventListener.chunk_by_pattern(text, _RECORD)),
        ),
        (
            "split_between_tokens",
            lambda: list(
                JsonEventListener.split_between_tokens(text, (LBRACE, RBRACE))
            ),
        ),
        (
            "chunk_by_rule",
            lambda: list(JsonEventListener.chunk_by_rule(text, "obj")),
        ),
    ]
    for label, fn in rows:
        t = best(fn)
        print(f"  {label:24s} {t * 1e3:9.2f} ms   {mb / t:8.1f} MB/s")


def main() -> int:
    for n in (10_000, 50_000, 200_000):
        bench_one(n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
