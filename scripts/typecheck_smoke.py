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

"""Type-check guard: every public symbol must resolve as `from antlrope import X`.

`scripts/` is in Pyright's `include` (see [tool.pyright]), so `pixi run typecheck`
checks this file. If a public name stops being importable from the top-level package
under a type checker (for example, because a re-export or `__all__` entry is
dropped), this fails.

antlrope ships `py.typed` with full type information, so a correctly configured
Pyright or mypy resolves all of these against an installed wheel. If yours
reports `reportMissingImports`, point it at the environment where antlrope is
installed (see docs/installation.md, "Type checking"). This module is never run.
"""

from __future__ import annotations

from antlrope import (
    Chunk,
    FacadeListener,
    LexToken,
    LineCol,
    ParseError,
    SourceMap,
    __version__,
)

# Reference each name so it is not flagged unused; the tuple is never evaluated.
_PUBLIC: tuple[object, ...] = (
    Chunk,
    FacadeListener,
    LexToken,
    LineCol,
    ParseError,
    SourceMap,
    __version__,
)
