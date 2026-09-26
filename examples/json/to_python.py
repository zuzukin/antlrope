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

"""Example: reconstruct a Python object from JSON using the generated facade.

Subclasses the generated `JsonEventListener` and overrides only the
`enter<Rule>`/`exit<Rule>` callbacks and `visitTerminal` that it needs.
Demonstrates the typical shape of a consumer: a small value stack driven by the
bulk event stream, with token text recovered by the runtime via index slicing (no
node objects, no per-node FFI crossings).

Run from this directory:

    python to_python.py '{"a": [1, true, null], "b": "hi"}'
"""

from __future__ import annotations

import json
import sys

from json_listener import JsonEventListener

# Scalar token types (see the generated facade's token-type constants). The
# anonymous literals follow ANTLR's positional naming: 'true', 'false', and
# 'null' are token types 7, 8, and 9, named T__6, T__7, and T__8.
_TRUE, _FALSE, _NULL = (
    JsonEventListener.T__6,
    JsonEventListener.T__7,
    JsonEventListener.T__8,
)
_STRING, _NUMBER = JsonEventListener.STRING, JsonEventListener.NUMBER

_MISSING = object()


class JsonValueBuilder(JsonEventListener):
    """Rebuild the parsed JSON document as native Python objects."""

    def __init__(self) -> None:
        # Stack of partially built containers. Object keys wait on _keys until
        # their value arrives; _expect_key marks that the next STRING is a key.
        self._stack: list = []
        self._keys: list = []
        self._expect_key = False
        self.result = _MISSING

    # --- containers ---------------------------------------------------------

    def enterObj(self) -> None:
        self._push({})

    def exitObj(self) -> None:
        self._pop()

    def enterArr(self) -> None:
        self._push([])

    def exitArr(self) -> None:
        self._pop()

    def enterPair(self) -> None:
        self._expect_key = True

    # --- scalars ------------------------------------------------------------

    def visitTerminal(self, token_type: int, text: str) -> None:
        if token_type == _STRING:
            value = json.loads(text)  # unquote and unescape
            if self._expect_key:
                self._keys.append(value)
                self._expect_key = False
                return
        elif token_type == _NUMBER:
            value = json.loads(text)
        elif token_type == _TRUE:
            value = True
        elif token_type == _FALSE:
            value = False
        elif token_type == _NULL:
            value = None
        else:
            return  # structural punctuation: '{', '}', '[', ']', ':', ','
        self._attach(value)

    # --- machinery ----------------------------------------------------------

    def _push(self, container: dict | list) -> None:
        self._attach(container)
        self._stack.append(container)

    def _pop(self) -> None:
        self._stack.pop()

    def _attach(self, value: object) -> None:
        if not self._stack:
            self.result = value
            return
        top = self._stack[-1]
        if isinstance(top, list):
            top.append(value)
        else:  # dict: pair key was captured first
            top[self._keys.pop()] = value


def parse(text: str) -> object:
    builder = JsonValueBuilder()
    builder.walk(text)
    return builder.result


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else '{"a": [1, true, null], "b": "hi"}'
    print(parse(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
