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

"""
**antlrope**: a fast, C++-accelerated ANTLR Python runtime for target-agnostic grammars.

Lexing and parsing run in the ANTLR4 C++ runtime, and a single bulk, filtered event
stream crosses into Python instead of a per-node parse-tree walk.

Basic usage:

1. Generate your parser with the stock ANTLR tool (`-Dlanguage=Python3`)
2. Generate a facade class with `antlrope gen`
3. Subclass the `<Grammar>EventListener` it emits
4. Call [listener.walk(...)][antlrope.FacadeListener.walk] (the generated class already
   knows its lexer and parser).
"""

from __future__ import annotations

from importlib.resources import files as _files

from . import _native
from .base import Chunk, FacadeListener, LexToken, ParseError
from .location import LineCol, SourceMap

__version__ = _files(__name__).joinpath("VERSION").read_text(encoding="utf-8").strip()

__all__ = [
    "Chunk",
    "FacadeListener",
    "LexToken",
    "LineCol",
    "ParseError",
    "SourceMap",
    "__version__",
]
