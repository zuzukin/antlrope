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
Driver for generated grammar-specific event listeners.

A generated `<Grammar>EventListener` subclass declares named callbacks
(`enter<Rule>`, `exit<Rule>`, `visitTerminal`, `visitError`) just like the stock
ANTLR listener. `walk` runs the bulk native event stream and dispatches those
callbacks, instead of building a Python parse tree and walking it.

It builds the native rule and token masks from the callbacks the subclass
overrides, so only the node kinds the consumer handles cross into Python. Events
for everything else are dropped in C++ before the buffer is built.

The class is also the grammar's chunking API: classmethods like
[split_on_token][antlrope.FacadeListener.split_on_token] and
[chunk_by_rule][antlrope.FacadeListener.chunk_by_rule] split a whole source into
positioned [Chunk][antlrope.Chunk]s for
[walk_parallel][antlrope.FacadeListener.walk_parallel], using the lexer and
parser the facade was generated for.
"""

from __future__ import annotations

import codecs
import os
import re
import struct
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import ClassVar, NamedTuple, Protocol, Self, cast

from . import _native
from .location import LineCol, SourceMap

__all__ = [
    "Chunk",
    "FacadeListener",
    "LexToken",
    "ParseError",
]

# A streaming text source: a filesystem path, an open text file object, or any
# iterable of str pieces (a file object iterates as lines; a generator works).
type TextSource = str | os.PathLike[str] | Iterable[str]
# One token type, or several treated as equivalent.
type TokenTypes = int | Iterable[int]
# An (open, close) bracket pair; either side may be one or several types.
type Pair = tuple[TokenTypes, TokenTypes]
# A grammar rule name or index, or several of them.
type RuleTypes = str | int | Iterable[str | int]

EV_ENTER, EV_EXIT, EV_TERMINAL, EV_ERROR = 0, 1, 2, 3
_REC = "<4i"
# Record layout emitted by _native.lex: (type, channel, start, stop).
_TOK = struct.Struct("<4i")
# Record layout emitted by _native.rule_spans: (rule_index, start, stop).
_RULE = struct.Struct("<3i")

#: The default token channel, where the lexer routes ordinary tokens.
DEFAULT_CHANNEL = 0


# Structural types for stock generated ANTLR `<Grammar>Parser` and `<Grammar>Lexer`
# classes. The current standard ANTLR Parser and Lexer base classes do not declare
# these members, but the generated classes do, so we use Protocol classes to record
# the expected interface.
class _ParserProtocol(Protocol):
    """The stock ANTLR `<Grammar>Parser` class interface that antlrope expects."""

    literalNames: ClassVar[Sequence[str]]
    symbolicNames: ClassVar[Sequence[str]]
    ruleNames: ClassVar[Sequence[str]]
    grammarFileName: ClassVar[str]


class _LexerProtocol(Protocol):
    """The stock ANTLR `<Grammar>Lexer` class interface that antlrope expects."""

    literalNames: ClassVar[Sequence[str]]
    symbolicNames: ClassVar[Sequence[str]]
    ruleNames: ClassVar[Sequence[str]]
    channelNames: ClassVar[Sequence[str]]
    modeNames: ClassVar[Sequence[str]]
    grammarFileName: ClassVar[str]


# Cache specs by class so repeated walks of a grammar pay the ATN deserialization
# cost once. Parser and lexer specs are cached separately because the lexer-only
# chunkers need no parser spec. The Python3 ANTLR target emits a module-level
# `serializedATN()` alongside each class, resolved here via the class's module.
_PARSER_SPEC_CACHE: dict[type[_ParserProtocol], _native.ParserSpec] = {}
_LEXER_SPEC_CACHE: dict[type[_LexerProtocol], _native.LexerSpec] = {}

# token type -> name map per grammar, for FacadeListener.token_name (built on
# demand). Keyed by the _PARSER class: its literalNames and symbolicNames are indexed
# by token type and cover the whole vocabulary (the lexer's symbolicNames is a
# compact list of just its named rules, not type-indexed).
_TOKEN_NAME_CACHE: dict[type[_ParserProtocol], dict[int, str]] = {}


def _build_token_names(parser_cls: type[_ParserProtocol]) -> dict[int, str]:
    """Map each token type to its symbolic name, falling back to its literal name."""
    sym = list(parser_cls.symbolicNames)
    lit = list(parser_cls.literalNames)
    names: dict[int, str] = {}
    for ttype in range(max(len(sym), len(lit))):
        name = sym[ttype] if ttype < len(sym) else ""
        if not name or name == "<INVALID>":
            name = lit[ttype] if ttype < len(lit) else ""
        if name and name != "<INVALID>":
            names[ttype] = name
    return names


def _incompatible_antlr(cls: type, e: Exception) -> Exception:
    """Turn a raw spec-build failure into an actionable "incompatible ANTLR" error.

    Antlrope sets no upper bound on the ANTLR versions it accepts, so a future
    ANTLR that changes the serialized-ATN format or the generated-module surface
    fails in `_build_parser_spec` or `_build_lexer_spec`, which every parse path
    goes through. The raw
    errors (a C++ deserializer message, a bare AttributeError) do not name a cause
    or a fix, so we re-raise with one and chain the original for details.
    """
    if isinstance(e, RuntimeError) and "deserialize ATN" in str(e):
        return RuntimeError(
            f"{cls.__name__}'s serialized ATN uses a format this antlrope build "
            "does not support; it was probably generated by a newer ANTLR tool. "
            "Regenerate the parser with an ANTLR 4.13-line tool, or upgrade "
            "antlrope."
        )
    if isinstance(e, AttributeError):
        return TypeError(
            f"{cls.__name__} does not look like a stock ANTLR Python3-target "
            f"parser or lexer (missing `{e.name}`); it may have been generated by "
            "an ANTLR version antlrope does not support."
        )
    return e


def _build_parser_spec(parser_cls: type[_ParserProtocol]) -> _native.ParserSpec:
    mod = sys.modules[parser_cls.__module__]
    grammar_file = getattr(parser_cls, "grammarFileName", "<grammar>.g4")
    try:
        return _native.ParserSpec(
            grammar_file,
            list(parser_cls.literalNames),
            list(parser_cls.symbolicNames),
            list(parser_cls.ruleNames),
            mod.serializedATN(),
        )
    except (RuntimeError, AttributeError) as e:
        wrapped = _incompatible_antlr(parser_cls, e)
        if wrapped is e:
            raise
        raise wrapped from e


def _build_lexer_spec(lexer_cls: type[_LexerProtocol]) -> _native.LexerSpec:
    mod = sys.modules[lexer_cls.__module__]
    grammar_file = getattr(lexer_cls, "grammarFileName", "<grammar>.g4")
    try:
        return _native.LexerSpec(
            grammar_file,
            list(lexer_cls.literalNames),
            list(lexer_cls.symbolicNames),
            list(lexer_cls.ruleNames),
            list(lexer_cls.channelNames),
            list(lexer_cls.modeNames),
            mod.serializedATN(),
        )
    except (RuntimeError, AttributeError) as e:
        wrapped = _incompatible_antlr(lexer_cls, e)
        if wrapped is e:
            raise
        raise wrapped from e


# Per-thread spec cache for parallel parsing. Each worker thread builds its own
# (parser_spec, lexer_spec) once per grammar (uncached). With the vendored runtime's
# per-DFA locks a shared spec scales too, but a per-thread spec is the simple, robust
# default with no shared mutable state (see docs/performance.md "Parallel parsing").
_thread_specs = threading.local()


def _specs_for_thread(
    cls: type[FacadeListener],
) -> tuple[_native.ParserSpec, _native.LexerSpec]:
    cache = getattr(_thread_specs, "cache", None)
    if cache is None:
        cache = _thread_specs.cache = {}
    key = (cls._LEXER, cls._PARSER)
    specs = cache.get(key)
    if specs is None:
        specs = cache[key] = (
            _build_parser_spec(cls._PARSER),
            _build_lexer_spec(cls._LEXER),
        )
    return specs


def _as_set(types: TokenTypes) -> frozenset[int]:
    """Normalize one token type or an iterable of them to a frozenset."""
    return frozenset((types,)) if isinstance(types, int) else frozenset(types)


def _normalize_pairs(
    pairs: Pair | list[Pair],
) -> list[tuple[frozenset[int], frozenset[int]]]:
    """Normalize the `pairs` argument to a list of (open-set, close-set)."""
    plist = [pairs] if isinstance(pairs, tuple) else list(pairs)
    out: list[tuple[frozenset[int], frozenset[int]]] = []
    for p in plist:
        if not (isinstance(p, tuple) and len(p) == 2):
            raise ValueError(f"each pair must be an (open, close) tuple, got {p!r}")
        out.append((_as_set(p[0]), _as_set(p[1])))
    if not out:
        raise ValueError("pairs must contain at least one (open, close) pair")
    return out


def _read_increments(
    fobj: object, window_chars: int | None, window_lines: int | None
) -> Iterator[str]:
    """Yield successive text increments from a text file-like `fobj`.

    With `window_lines` set, reads that many lines per step (capped at
    `window_chars` characters if also set); otherwise reads `window_chars`-sized
    blocks. The increment size only affects how often the regex re-runs and how
    much read-ahead is buffered, never the output.
    """
    if window_lines is not None and window_lines > 0:
        cap = window_chars if window_chars and window_chars > 0 else None
        while True:
            lines: list[str] = []
            total = 0
            for _ in range(window_lines):
                line = fobj.readline()  # type: ignore[attr-defined]
                if not line:
                    break
                lines.append(line)
                total += len(line)
                if cap is not None and total >= cap:
                    break
            if not lines:
                return
            yield "".join(lines)
    else:
        size = window_chars if window_chars and window_chars > 0 else 65536
        while True:
            piece = fobj.read(size)  # type: ignore[attr-defined]
            if not piece:
                return
            yield piece


class ParseError(Exception):
    """A parse diagnostic: one syntax error collected during a `walk`.

    Antlrope replaces ANTLR's default console error listener with a collecting
    one, so parse failures are gathered into
    [syntax_errors][antlrope.FacadeListener.syntax_errors] rather than written to
    stderr. Each carries the position of the offending token and ANTLR's message.

    It subclasses `Exception`, so `str(err)` is the message and you can `raise` it
    (e.g. to turn the first collected error into a hard failure).
    """

    line: int
    """1-based line of the offending token."""
    column: int
    """0-based column of the offending token."""
    start: int
    """0-based codepoint offset of the offending token's first character (matching
    the event-stream offsets), or -1 when there is no token (e.g. a lexer error)."""
    stop: int
    """0-based codepoint offset of the offending token's last character, inclusive,
    or -1 when there is no token."""
    message: str
    """ANTLR's human-readable error message."""

    def __init__(
        self, line: int, column: int, start: int, stop: int, message: str
    ) -> None:
        super().__init__(message)
        self.line = line
        self.column = column
        self.start = start
        self.stop = stop
        self.message = message

    def __repr__(self) -> str:
        return (
            f"ParseError(line={self.line}, column={self.column}, "
            f"start={self.start}, stop={self.stop}, message={self.message!r})"
        )


class LexToken(NamedTuple):
    """One token from [lex][antlrope.FacadeListener.lex].

    The record does not include `line` or `column`, which are cheap to derive from
    `start` with a [SourceMap][antlrope.SourceMap]. Limiting it to four ints keeps
    a full-stream `lex()` light.
    """

    type: int
    channel: int
    start: int  # 0-based codepoint offset of the first character
    stop: int  # 0-based codepoint offset of the last character (inclusive)


class Chunk(NamedTuple):
    """A contiguous chunk of source text plus location information.

    These are produced by the [FacadeListener][antlrope.FacadeListener] chunking
    classmethods, such as
    [chunk_by_pattern][antlrope.FacadeListener.chunk_by_pattern], for consumption
    by [FacadeListener.walk_parallel][antlrope.FacadeListener.walk_parallel].
    """

    text: str
    """
    Text contents of the chunk.
    """

    offset: int = 0
    """
    Starting character (codepoint) offset of the chunk.
    """

    line: int = 1
    """
    Starting line number of the chunk (indexed from 1).
    """

    column: int = 0
    """
    Starting column number of the chunk (indexed from 0).
    """

    sourcename: str = ""
    """
    Name of the source (e.g. a filename or path).
    """

    def after(self, text: str) -> Chunk:
        """Return a `Chunk` for `text` positioned immediately after this one.

        Computes the starting location based on the contents and starting
        position of the current chunk, and copies the sourcename.
        """
        newlines = self.text.count("\n")
        offset = self.offset + len(self.text)
        if newlines == 0:
            return Chunk(
                text, offset, self.line, self.column + len(self.text), self.sourcename
            )
        column = len(self.text) - self.text.rfind("\n") - 1
        return Chunk(text, offset, self.line + newlines, column, self.sourcename)

    @staticmethod
    def _from_span(
        text: str,
        sm: SourceMap,
        start: int,
        stop: int,
        sourcename: str = "",
        *,
        trim: bool = True,
    ) -> Chunk | None:
        """Build a positioned `Chunk` for `text[start:stop]`.

        With `trim=True` (default) strip surrounding whitespace and skip a
        whitespace-only region (returns `None`); with `trim=False` keep the span
        verbatim (only an empty region returns `None`)."""
        seg = text[start:stop]
        if not trim:
            return Chunk(seg, start, *sm.line_col(start), sourcename) if seg else None
        stripped = seg.lstrip()
        body = stripped.rstrip()
        if not body:
            return None
        offset = start + (len(seg) - len(stripped))  # advance past leading whitespace
        line, column = sm.line_col(offset)
        return Chunk(body, offset, line, column, sourcename)

    @staticmethod
    def _split_stream(
        increments: Iterator[str],
        rx: re.Pattern[str],
        where: str,
        sourcename: str = "",
        trim: bool = True,
    ) -> Iterator[Chunk]:
        """Split the concatenation of `increments` at each `rx` match, streaming.

        Holds only the current open chunk plus one read-ahead increment: it searches
        a growing buffer for the next delimiter, and only commits a match once a
        character past it has been read (or EOF), so a match is never truncated by a
        read boundary. Reproduces `split_on_pattern` over the same text for
        delimiters that fit within the buffer.
        """
        before = where == "before"
        buf = ""
        base = 0  # absolute codepoint offset of buf[0]; kept == chunk_start
        chunk_start = 0  # absolute start of the current open (un-emitted) chunk
        resume = 0  # absolute offset to resume searching from (>= chunk_start)
        origin = 0  # absolute offset for which (oline, ocol) hold
        oline = 1  # 1-based line, like SourceMap
        ocol = 0  # 0-based column
        eof = False

        def advance_origin(to: int) -> None:
            nonlocal origin, oline, ocol
            seg = buf[origin - base : to - base]
            if seg:
                newlines = seg.count("\n")
                if newlines:
                    oline += newlines
                    ocol = len(seg) - seg.rindex("\n") - 1
                else:
                    ocol += len(seg)
                origin = to

        def make_chunk(a: int, b: int) -> Chunk | None:
            # Region [a, b): with trim, strip surrounding whitespace; otherwise keep
            # it verbatim. Advance origin to b; return None for an empty region (or,
            # when trimming, a whitespace-only one).
            seg = buf[a - base : b - base]
            if trim:
                stripped = seg.lstrip()
                body = stripped.rstrip()
                offset = a + (len(seg) - len(stripped))
            else:
                body = seg
                offset = a
            if not body:
                advance_origin(b)
                return None
            advance_origin(offset)
            line, column = oline, ocol
            advance_origin(b)
            return Chunk(body, offset, line, column, sourcename)

        while True:
            match = rx.search(buf, resume - base)
            if match is not None and (match.end() < len(buf) or eof):
                mstart = base + match.start()
                mend = base + match.end()
                boundary = mstart if before else mend
                chunk = make_chunk(chunk_start, boundary)
                if chunk is not None:
                    yield chunk
                chunk_start = boundary
                # Resume past this delimiter (non-overlapping, like finditer); guard a
                # zero-width match so the search position always advances.
                resume = mend if mend > mstart else mstart + 1
                if chunk_start > base:  # drop the emitted prefix
                    buf = buf[chunk_start - base :]
                    base = chunk_start
                continue
            if eof:
                chunk = make_chunk(chunk_start, base + len(buf))
                if chunk is not None:
                    yield chunk
                return
            piece = next(increments, None)
            if piece is None:
                eof = True
            elif piece:
                buf += piece


class FacadeListener:
    """Base for generated `<Grammar>EventListener` classes.

    Provides source-location access for the current event. While a callback is
    running, [span][antlrope.FacadeListener.span] returns its
    `(start, stop)` character offsets and
    [line_col][antlrope.FacadeListener.line_col] the 1-based line and 0-based
    column of its start. `walk` updates this state before each callback; outside
    a callback it reflects the most recent one.
    """

    # The stock ANTLR `<Grammar>Lexer` and `<Grammar>Parser` classes plus the grammar
    # metadata, all set by every generated subclass (declared here, no value).
    _LEXER: ClassVar[type[_LexerProtocol]]
    _PARSER: ClassVar[type[_ParserProtocol]]
    ruleNames: ClassVar[list[str]]
    START_RULE: ClassVar[int]

    _pyfacade_text: str = ""
    _pyfacade_start: int = -1
    _pyfacade_stop: int = -1
    _pyfacade_sourcemap: SourceMap | None = None
    # Source position of the parsed text's first character, so positions can be
    # reported against the whole source (see Chunk and walk_parallel). The
    # defaults (offset 0, line 1, column 0) leave a plain `walk` unchanged.
    _pyfacade_base_offset: int = 0
    _pyfacade_base_linecol = LineCol()
    # Name of the source being parsed (e.g. a filename), for diagnostics. Empty
    # unless a Chunk carried a sourcename or `_drive` was given one.
    _pyfacade_sourcename: str = ""
    # Indices of the rules currently open (innermost last), maintained by `_drive`
    # for depth, rule_stack, and current_rule. Reassigned to a fresh list per walk
    # (never mutated at the class level), so the shared default is safe.
    _pyfacade_scope: list[int] = []  # noqa: RUF012

    # Reassigned to a fresh list by `_drive` on every walk (never mutated in
    # place), so the shared class-level default is safe; hence the RUF012 waiver.
    syntax_errors: list[ParseError] = []  # noqa: RUF012
    """Parse diagnostics collected during the most recent `walk`.

    A list of [ParseError][antlrope.ParseError] exceptions, empty when the
    parse had no errors. The default ANTLR console error listener is suppressed, so
    these are the only report of a parse failure. Inspect them instead of watching
    stderr.
    """

    def span(self) -> tuple[int, int]:
        """Return the `(start, stop)` character offsets of the current event.

        Offsets are into the whole source: for a `walk_parallel` chunk they
        include the chunk's start offset. Returns `(-1, -1)` when the event has no
        span.
        """
        start, stop = self._pyfacade_start, self._pyfacade_stop
        if start < 0:
            return start, stop
        base = self._pyfacade_base_offset
        return start + base, stop + base

    def line_col(self) -> LineCol | None:
        """Return the `(line, column)` of the current event's start, or `None`.

        Returns:
            The 1-based line and 0-based column of the current event's start, or
            `None` when the event has no source span (e.g. an empty rule). For a
            `walk_parallel` chunk the position is reported against the whole
            source via the chunk's start. The
            [SourceMap][antlrope.SourceMap] is built once per walk on first
            use.
        """
        start = self._pyfacade_start
        if start < 0:
            return None
        sm = self._pyfacade_sourcemap
        if sm is None:
            sm = self._pyfacade_sourcemap = SourceMap(self._pyfacade_text)
        linecol = sm.line_col(start)
        # Offset into the whole source. Only the chunk's first line shares a line
        # with the chunk's start, so only it picks up the start column.
        return self._pyfacade_base_linecol.add(linecol)

    def sourcename(self) -> str:
        """Return the name of the source being parsed (empty if none was set).

        This is the `sourcename` of the [Chunk][antlrope.Chunk] being parsed
        (set by the chunkers; the streaming chunkers default it to the file path).
        Use it with [line_col][antlrope.FacadeListener.line_col] to report a
        position as `sourcename:line:column`.
        """
        return self._pyfacade_sourcename

    def text(self) -> str:
        """Return the source text of the current event (rule or terminal).

        Inside `visitTerminal` this is the token's text (the same string passed to
        it); inside an `enter<Rule>` or `exit<Rule>` callback it is the rule's full
        text, such as the whole `{...}` of an object. Empty when the event has no
        span (an empty rule, or an error token that recovery inserted or reported
        missing).
        """
        start = self._pyfacade_start
        if start < 0:
            return ""
        return self._pyfacade_text[start : self._pyfacade_stop + 1]

    def depth(self) -> int:
        """Return the current nesting depth: the number of open rule scopes.

        The [rule_stack][antlrope.FacadeListener.rule_stack] holds only the rules
        you subscribe to, because only their events cross into Python. The depth
        therefore counts the constructs you track. Override
        [enterEveryRule][antlrope.FacadeListener.enterEveryRule] or
        [exitEveryRule][antlrope.FacadeListener.exitEveryRule] to track every rule
        (the full parse-tree depth).
        """
        return len(self._pyfacade_scope)

    def rule_stack(self) -> tuple[str, ...]:
        """Return the names of the currently open rules, outermost first.

        See [depth][antlrope.FacadeListener.depth] for which rules are tracked.
        Test containment with `"obj" in self.rule_stack()` to ask "am I anywhere
        inside an `obj`?".
        """
        names = self.ruleNames
        return tuple(names[i] for i in self._pyfacade_scope)

    def current_rule(self) -> str | None:
        """Return the innermost open rule's name, or `None` outside any tracked rule.

        Inside a terminal callback this is the rule containing the token; inside an
        `enter<Rule>` or `exit<Rule>` callback it is that rule.
        """
        scope = self._pyfacade_scope
        return self.ruleNames[scope[-1]] if scope else None

    @classmethod
    def rule_name(cls, index: int) -> str:
        """Return the grammar rule name for a rule index (e.g. from `enterEveryRule`).

        Raises:
            IndexError: If `index` is not a valid rule index (negative indices are
                not treated as from-the-end positions).
        """
        if not 0 <= index < len(cls.ruleNames):
            raise IndexError(f"rule index out of range: {index}")
        return cls.ruleNames[index]

    @classmethod
    def token_name(cls, token_type: int) -> str:
        """Return the name of a token type, such as `STRING`.

        Uses the symbolic name if there is one and falls back to the literal name
        (e.g. `"'{'"`) for anonymous tokens. For an unknown type it returns the type
        number as a string. Useful for logging, debugging, and generic
        `visitTerminal` handlers.
        """
        names = _TOKEN_NAME_CACHE.get(cls._PARSER)
        if names is None:
            names = _TOKEN_NAME_CACHE[cls._PARSER] = _build_token_names(cls._PARSER)
        return names.get(token_type, str(token_type))

    def visitTerminal(self, token_type: int, text: str) -> None:
        """No-op terminal callback; override in a subclass to handle tokens."""

    def visitError(self, token_type: int, text: str) -> None:
        """No-op error callback; override in a subclass to handle error nodes."""

    def enterEveryRule(self, rule_index: int) -> None:
        """No-op hook called on entry to every rule; override it to use it.

        Overriding this (or [exitEveryRule][antlrope.FacadeListener.exitEveryRule])
        subscribes to all rule events, so [depth][antlrope.FacadeListener.depth] and
        [rule_stack][antlrope.FacadeListener.rule_stack] track the full parse
        tree. `rule_index` is the rule's index;
        [rule_name][antlrope.FacadeListener.rule_name] or
        [current_rule][antlrope.FacadeListener.current_rule] gives its name.

        Warning:
            Useful for debugging and tracing, but at a performance cost:
            subscribing to all rule events disables the native rule filtering, so
            every rule entry and exit crosses into Python and dispatches a callback.
            On a large input that can mean iterating millions of events instead of
            a few. For production listeners, override the named `enter<Rule>` and
            `exit<Rule>` callbacks you need instead.
        """

    def exitEveryRule(self, rule_index: int) -> None:
        """No-op hook called on exit from every rule; override it to use it.

        See [enterEveryRule][antlrope.FacadeListener.enterEveryRule], including its
        warning. This hook is useful for debugging and tracing, but overriding it
        disables the native rule filtering, so every rule event crosses into Python.
        """

    @classmethod
    def _facade_base(cls) -> type[FacadeListener]:
        """Return the generated `<Grammar>EventListener` in this class's ancestry.

        That base (the class that directly subclasses `FacadeListener`) holds the
        no-op callback stubs `walk` compares against to
        detect overrides, plus `ruleNames` and `START_RULE`. Works whether `cls` is
        the generated class itself or a user subclass of it.

        Raises:
            TypeError: If `cls` is not a generated facade listener subclass.
        """
        for klass in cls.__mro__:
            if FacadeListener in klass.__bases__:
                return cast("type[FacadeListener]", klass)
        raise TypeError(f"{cls.__name__} is not a generated facade listener subclass")

    @classmethod
    def _resolve_start_rule(
        cls, base: type[FacadeListener], start_rule: int | str | None
    ) -> int:
        """Turn a rule name, rule index, or `None` into a rule index for `base`."""
        if start_rule is None:
            return base.START_RULE
        if isinstance(start_rule, int):
            return start_rule
        try:
            return list(base.ruleNames).index(start_rule)
        except ValueError:
            raise ValueError(
                f"unknown start rule {start_rule!r}; "
                f"known rules: {list(base.ruleNames)}"
            ) from None

    @classmethod
    def _resolve_rule(cls, rule: str | int) -> int:
        """Resolve a rule name or index to a rule index for this grammar."""
        if isinstance(rule, int):
            return rule
        names = list(cls.ruleNames)
        try:
            return names.index(rule)
        except ValueError:
            raise ValueError(f"unknown rule {rule!r}; known rules: {names}") from None

    # NOTE: internal, but the srdl-bench benchmark (github.com/zuzukin/srdl-bench)
    # calls _parser_spec/_lexer_spec and _native.parse_events directly to time the
    # raw native stage. These are unlikely to ever change, but if they do, update
    # that repo at the same time (it is not published as a package).
    @classmethod
    def _parser_spec(cls, *, cached: bool = True) -> _native.ParserSpec:
        """Return the native `ParserSpec` built from this grammar's parser class.

        Reads the serialized ATN and the name and vocabulary metadata from the stock
        `<Grammar>Parser` class and passes them to the C++ runtime. This is what
        makes the runtime grammar-agnostic without any code generation of its own.

        Args:
            cached: When `True` (default), reuse the cached result or store a new
                one. Pass `False` to build a fresh spec that is neither read from
                nor written to the cache, e.g. to give each worker thread its own
                spec.

        Returns:
            The `ParserSpec` for the grammar. A spec owns a mutable ATN. Because the
            vendored runtime uses per-DFA locks, sharing one spec across threads is
            correct and scales, so most parallel code can share a cached spec (or use
            [walk_parallel][antlrope.FacadeListener.walk_parallel]).
        """
        if cached:
            hit = _PARSER_SPEC_CACHE.get(cls._PARSER)
            if hit is not None:
                return hit
        spec = _build_parser_spec(cls._PARSER)
        if cached:
            _PARSER_SPEC_CACHE[cls._PARSER] = spec
        return spec

    @classmethod
    def _lexer_spec(cls, *, cached: bool = True) -> _native.LexerSpec:
        """Return the native `LexerSpec` built from this grammar's lexer class.

        The lexer-only counterpart of the parser spec, for the token-based chunkers
        (and [lex][antlrope.FacadeListener.lex]) that lex without parsing.

        Args:
            cached: When `True` (default), reuse the cached result or store a new
                one in the per-lexer cache. `False` builds a fresh, uncached spec.

        Returns:
            The `LexerSpec` for the grammar.
        """
        if cached:
            hit = _LEXER_SPEC_CACHE.get(cls._LEXER)
            if hit is not None:
                return hit
        spec = _build_lexer_spec(cls._LEXER)
        if cached:
            _LEXER_SPEC_CACHE[cls._LEXER] = spec
        return spec

    @classmethod
    def clear_cache(cls) -> None:
        """Release this grammar's cached parser and lexer state.

        Repeated walks reuse deserialized parser and lexer state cached per
        generated class. You never need `clear_cache` for correctness. The caches
        are keyed by the generated lexer and parser classes, and regenerating a
        parser means re-importing its module, which produces new classes and
        therefore new cache entries. Call it to release the memory of a grammar you are
        done with. The per-thread copies built by
        [walk_parallel][antlrope.FacadeListener.walk_parallel] are thread-local
        and released with their worker threads, not from here.
        """
        parser = getattr(cls, "_PARSER", None)
        if parser is not None:
            _PARSER_SPEC_CACHE.pop(parser, None)
            _TOKEN_NAME_CACHE.pop(parser, None)
        lexer = getattr(cls, "_LEXER", None)
        if lexer is not None:
            _LEXER_SPEC_CACHE.pop(lexer, None)

    def walk(
        self,
        text: str,
        *,
        start_rule: int | str | None = None,
        filtered: bool = True,
    ) -> Self:
        """Parse `text` with this grammar's lexer and parser and return `self`.

        Runs the parse in C++ and dispatches the event stream to this listener's
        callbacks. The generated subclass already references the lexer and parser
        classes, so you do not pass them.

        Args:
            text: The source text to parse.
            start_rule: The rule to start parsing at: a rule name, a rule index, or
                `None` for the grammar's default start rule.
            filtered: When `True` (default), the native parser emits events only for
                the rules and tokens whose callbacks you override. `False` emits the
                full event stream.

        Returns:
            `self`, so calls chain (e.g. `result = Collector().walk(text).result`).

        Raises:
            TypeError: If called on a class that is not a generated
                `<Grammar>EventListener` subclass.
        """
        rule = self._resolve_start_rule(self._facade_base(), start_rule)
        self._drive(
            self._parser_spec(), self._lexer_spec(), text, rule, filtered=filtered
        )
        return self

    @classmethod
    def walk_parallel(
        cls,
        chunks: Iterable[str | Chunk],
        *,
        start_rule: int | str | None = None,
        max_workers: int | None = None,
        filtered: bool = True,
        factory: Callable[[], Self] | None = None,
    ) -> Iterator[Self]:
        """Parse independent `chunks` across a thread pool, one listener each.

        The native parse releases the GIL, so the parses overlap across cores. Each
        worker thread builds its own copy of the parser state once, so threads never
        contend on a shared ATN. The per-event Python dispatch still holds the GIL,
        so the speedup depends on how much of the time goes to parsing rather than
        to Python callbacks. See the "Parallel parsing" section of
        `docs/performance.md`.

        Args:
            chunks: The pieces of source to parse, each a self-contained piece
                (e.g. one record or top-level definition) that parses as
                `start_rule`. A bare `str` is treated as contiguous with the
                previous chunk and its source position is computed; a
                [Chunk][antlrope.Chunk] pins
                an explicit `offset`, `line`, and `column` so callbacks
                report [span][antlrope.FacadeListener.span] and
                [line_col][antlrope.FacadeListener.line_col] against the
                whole source. You can mix the two: a `Chunk` resets the running
                position for the contiguous `str` chunks that follow it.
            start_rule: The rule each chunk parses as: a rule name, a rule index,
                or `None` for the grammar's start rule.
            max_workers: The maximum number of parses running at once. This is also
                the size of the window used to buffer results and keep them in input
                order. Defaults to `os.cpu_count()`. With `1` it runs inline,
                without a pool.
            filtered: When `True` (default), the native parser emits events only for
                the rules and tokens whose callbacks you override. `False` emits the
                full event stream.
            factory: A zero-argument callable returning a fresh listener, for
                subclasses whose constructor needs arguments. Defaults to `cls`.

        Returns:
            A lazy iterator of listeners, one per chunk, in input order. Chunks
            are pulled and parsed on demand with at most `max_workers` parses in
            flight, so neither the whole input nor all the results are held at once.
            Consume the iterator incrementally, or call `list(...)` on it to collect
            every result. Each
            listener carries its accumulated state plus its
            [syntax_errors][antlrope.FacadeListener.syntax_errors].

        Raises:
            TypeError: If called on a class that is not a generated
                `<Grammar>EventListener` subclass.
        """
        base = cls._facade_base()  # raises if not a generated facade subclass
        rule = cls._resolve_start_rule(base, start_rule)  # validate eagerly
        make = factory if factory is not None else cls
        workers = max_workers if max_workers is not None else (os.cpu_count() or 1)

        def run(chunk: Chunk) -> Self:
            parser_spec, lexer_spec = _specs_for_thread(cls)
            listener = make()
            listener._drive(
                parser_spec,
                lexer_spec,
                chunk.text,
                rule,
                filtered=filtered,
                origin=(chunk.offset, chunk.line, chunk.column),
                sourcename=chunk.sourcename,
            )
            return listener

        def resolved() -> Iterator[Chunk]:
            # A bare str continues contiguously from the previous chunk; a Chunk
            # pins its own position and re-anchors the str chunks that follow it.
            # The empty seed chunk starts the first str at the origin (0, 1, 0).
            chunk = Chunk("")
            for item in chunks:
                if isinstance(item, Chunk):
                    chunk = item
                elif isinstance(item, str):
                    chunk = chunk.after(item)
                else:
                    raise TypeError(
                        f"chunks must be str or Chunk, got {type(item).__name__}"
                    )
                yield chunk

        def stream() -> Iterator[Self]:
            if workers <= 1:
                for chunk in resolved():
                    yield run(chunk)
                return
            # Bounded-window parallelism: keep ~`workers` parses in flight and yield
            # in input order, so peak memory tracks the window, not the chunk count.
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending: deque[Future[Self]] = deque()
                for chunk in resolved():
                    pending.append(pool.submit(run, chunk))
                    if len(pending) > workers:
                        yield pending.popleft().result()
                while pending:
                    yield pending.popleft().result()

        return stream()

    def _drive(
        self,
        parser_spec: _native.ParserSpec,
        lexer_spec: _native.LexerSpec,
        text: str,
        start_rule: int,
        *,
        filtered: bool = True,
        origin: tuple[int, int, int] = (0, 1, 0),
        sourcename: str = "",
    ) -> None:
        """Run the native parse and dispatch this listener's overridden callbacks.

        Usually called for you by [walk][antlrope.FacadeListener.walk] or
        [walk_parallel][antlrope.FacadeListener.walk_parallel]; call it
        directly to drive a listener from already-loaded specs.

        Args:
            parser_spec: The native parser spec.
            lexer_spec: The native lexer spec.
            text: The source to parse.
            start_rule: The index of the rule to start parsing at.
            filtered: When `True` (default), the native parser emits events only for
                the rules and tokens whose callbacks you override. `False` emits the
                full event stream regardless of overrides.
            origin: The `(offset, line, column)` of `text`'s first character, so
                callbacks report positions against the whole source. Defaults to
                the start of the source, `(0, 1, 0)`.
            sourcename: Optional name of the source (e.g. a filename), returned by
                [sourcename][antlrope.FacadeListener.sourcename] during the
                walk.
        """
        cls = type(self)
        # The generated <Grammar>EventListener base holds the no-op callback stubs
        # to compare against for override detection, plus ruleNames.
        base_cls = self._facade_base()
        rule_names = base_cls.ruleNames

        enter: list[Callable | None] = [None] * len(rule_names)
        leave: list[Callable | None] = [None] * len(rule_names)
        rule_mask: list[int] | None = []
        for idx, name in enumerate(rule_names):
            cap = name[0].upper() + name[1:]
            e_over = getattr(cls, "enter" + cap) is not getattr(base_cls, "enter" + cap)
            x_over = getattr(cls, "exit" + cap) is not getattr(base_cls, "exit" + cap)
            if e_over:
                enter[idx] = getattr(self, "enter" + cap)
            if x_over:
                leave[idx] = getattr(self, "exit" + cap)
            if e_over or x_over:
                rule_mask.append(idx)

        visit = None
        token_mask: list[int] | None = []
        if cls.visitTerminal is not base_cls.visitTerminal:
            visit = self.visitTerminal
            toks = getattr(cls, "TERMINAL_TOKENS", None)
            token_mask = list(toks) if toks is not None else None

        on_error = None
        if cls.visitError is not base_cls.visitError:
            on_error = self.visitError

        # enterEveryRule and exitEveryRule fire on every rule; overriding either
        # forces all rule events (mask -> None) so the hooks see them all and the
        # rule_stack tracks the full parse tree.
        every_enter = None
        if cls.enterEveryRule is not base_cls.enterEveryRule:
            every_enter = self.enterEveryRule
        every_exit = None
        if cls.exitEveryRule is not base_cls.exitEveryRule:
            every_exit = self.exitEveryRule
        if every_enter is not None or every_exit is not None:
            rule_mask = None

        # filtered=False forces a faithful full stream regardless of overrides.
        r_mask = rule_mask if filtered else None
        t_mask = token_mask if filtered else None
        raw, errors = _native.parse_events(
            parser_spec, lexer_spec, text, start_rule, r_mask, t_mask
        )
        # Wrap the raw native diagnostics into ParseError exceptions (rare error
        # path, so the per-error allocation is negligible).
        self.syntax_errors = [
            ParseError(e.line, e.column, e.start, e.stop, e.message) for e in errors
        ]

        # Source-location state read by span and line_col. Reset the cached map so a
        # reused listener re-derives it for this text.
        self._pyfacade_text = text
        self._pyfacade_sourcemap = None
        self._pyfacade_sourcename = sourcename
        self._pyfacade_base_offset = origin[0]
        self._pyfacade_base_linecol = LineCol(origin[1], origin[2])
        scope = self._pyfacade_scope = []

        for kind, payload, start, stop in struct.iter_unpack(_REC, raw):
            if kind == EV_TERMINAL:
                if visit is not None:
                    self._pyfacade_start = start
                    self._pyfacade_stop = stop
                    visit(payload, text[start : stop + 1])
            elif kind == EV_ENTER:
                scope.append(payload)  # push before callbacks: depth() includes self
                cb = enter[payload]
                if every_enter is not None or cb is not None:
                    self._pyfacade_start = start
                    self._pyfacade_stop = stop
                    if every_enter is not None:
                        every_enter(payload)
                    if cb is not None:
                        cb()
            elif kind == EV_EXIT:
                cb = leave[payload]
                if every_exit is not None or cb is not None:
                    self._pyfacade_start = start
                    self._pyfacade_stop = stop
                    if every_exit is not None:
                        every_exit(payload)
                    if cb is not None:
                        cb()
                if scope:  # pop after callbacks: exit sees the same depth as enter
                    scope.pop()
            elif kind == EV_ERROR and on_error is not None:
                self._pyfacade_start = start
                self._pyfacade_stop = stop
                err_text = text[start : stop + 1] if 0 <= start <= stop else ""
                on_error(payload, err_text)

    # ------------------------------------------------------------------ chunking
    # Split a whole source into positioned `Chunk`s for `walk_parallel`. The token-
    # and rule-based chunkers use this class's lexer and parser; the regex ones need
    # neither. See `docs/chunking.md`.

    @classmethod
    def lex(
        cls,
        text: str,
        *,
        keep: Iterable[int] | None = None,
    ) -> Iterator[LexToken]:
        """Tokenize `text` with the grammar's lexer (no parsing).

        This efficiently generates a stream of tokens on all channels.

        Use it to inspect the token stream directly: to recover off-channel tokens
        such as comments that the parser drops and so never reach `visitTerminal`,
        to gather token statistics or run a cheap pre-scan, or to build a custom
        token-aware splitter. It is also the pass the built-in `split_on_token` and
        `split_between_tokens` chunkers run on.

        `lex` works in memory and has no streaming variant: for a large file, chunk
        it with the `stream_*` methods and `lex` each chunk's `text` (see the
        "streaming records" recipe in the docs).

        Args:
            text: The source to tokenize.
            keep: Optional set of token types to return. The lexer drops every other
                token in C++, so only these cross into Python. `None` returns all
                tokens.

        Yields:
            Every token the lexer produces, in source order, on all channels: the
            default channel plus any hidden or custom channel a rule routes to with
            `-> channel(...)` (each token carries its own `channel`). The stream
            excludes tokens the lexer discards with `-> skip`, the partial tokens
            that `-> more` merges into the next token, the EOF sentinel, and, if
            `keep` is given, any token type not in it.

            On illegal input the lexer applies ANTLR's default recovery (it discards
            the offending character and continues) instead of raising or stopping,
            so malformed input still yields a token stream for the rest of the text.
            Those lexer error diagnostics are not surfaced by `lex`; parse with
            `walk` if you need `syntax_errors`.
        """
        spec = cls._lexer_spec()
        mask = None if keep is None else list(keep)
        raw, _errors = _native.lex(spec, text, mask)
        for rec in _TOK.iter_unpack(raw):
            yield LexToken(*rec)

    @classmethod
    def split_on_token(
        cls,
        text: str,
        token_types: TokenTypes,
        *,
        where: str = "before",
        channel: int | None = DEFAULT_CHANNEL,
        sourcename: str = "",
        trim: bool = True,
    ) -> Iterator[Chunk]:
        """Split `text` into chunks at each delimiter token.

        Token-aware: because it runs the grammar's lexer, a delimiter that appears
        inside a string or comment token never causes a split. The cost is lexing the
        whole input. Finding the delimiters is roughly an order of magnitude slower
        than with the regex-based
        [split_on_pattern][antlrope.FacadeListener.split_on_pattern] (about 4x end to
        end), but still negligible compared with the parse it feeds. See the
        "Chunking: lexer vs regex" notes in `docs/performance.md`.

        Args:
            text: The source to split.
            token_types: The delimiter token type, or several types that all act as
                delimiters (e.g. `MyLexer.RECORD`, or `{MyLexer.RECORD, MyLexer.NOTE}`).
            where: `"before"` starts a new chunk at each delimiter (each chunk begins
                with one), so content before the first delimiter is its own leading
                chunk; `"after"` ends a chunk at each delimiter (each chunk ends with
                one), so content after the last delimiter is a trailing chunk.
            channel: Only tokens on this channel are split on (default: the default
                channel). Pass `None` to consider all channels.
            sourcename: Optional source name (e.g. a filename) recorded on each
                [Chunk][antlrope.Chunk], surfaced during a walk as
                [sourcename][antlrope.FacadeListener.sourcename].
            trim: When `True` (the default) each chunk is stripped of surrounding
                whitespace and whitespace-only regions are dropped. Pass `False`
                to keep every region verbatim, including a leading or trailing
                whitespace terminator such as a record's mandatory newline. Only
                empty (zero-length) regions are then dropped.

        Yields:
            One [Chunk][antlrope.Chunk] per region between delimiters, carrying its
            source position; by default trimmed of surrounding whitespace with
            whitespace-only regions skipped (see `trim`).
        """
        if where not in ("before", "after"):
            raise ValueError(f"where must be 'before' or 'after', got {where!r}")
        delims = _as_set(token_types)
        bounds = [
            t
            for t in cls.lex(text, keep=delims)
            if channel is None or t.channel == channel
        ]
        sm = SourceMap(text)
        n = len(text)
        if where == "before":
            first = bounds[0].start if bounds else n
            if first > 0:  # leading region, before the first delimiter
                chunk = Chunk._from_span(text, sm, 0, first, sourcename, trim=trim)
                if chunk is not None:
                    yield chunk
            for i, b in enumerate(bounds):
                end = bounds[i + 1].start if i + 1 < len(bounds) else n
                chunk = Chunk._from_span(text, sm, b.start, end, sourcename, trim=trim)
                if chunk is not None:
                    yield chunk
        else:  # after
            prev = 0
            for b in bounds:
                chunk = Chunk._from_span(
                    text, sm, prev, b.stop + 1, sourcename, trim=trim
                )
                if chunk is not None:
                    yield chunk
                prev = b.stop + 1
            if prev < n:  # trailing region, after the last delimiter
                chunk = Chunk._from_span(text, sm, prev, n, sourcename, trim=trim)
                if chunk is not None:
                    yield chunk

    @classmethod
    def stream_on_token(
        cls,
        path: str | os.PathLike[str],
        token_types: TokenTypes,
        *,
        where: str = "before",
        encoding: str = "utf-8",
        channel: int | None = DEFAULT_CHANNEL,
        sourcename: str = "",
        trim: bool = True,
        batch: int = 256,
        _block_bytes: int = 0,
    ) -> Iterator[Chunk]:
        """Split a file into chunks at each delimiter token without loading it whole.

        The streaming counterpart of
        [split_on_token][antlrope.FacadeListener.split_on_token]: instead of taking
        the whole source as a `str`, it opens `path` in C++ and lexes it incrementally
        over a sliding window, slicing out and freeing each chunk as it goes, so peak
        memory is roughly one chunk rather than the whole file. The yielded
        [Chunk][antlrope.Chunk]s can be passed directly to
        [walk_parallel][antlrope.FacadeListener.walk_parallel], which pulls them
        lazily, keeping the whole pipeline bounded.

        Args:
            path: Filesystem path to the source (opened by the native layer as UTF-8).
            token_types: The delimiter token type, or several types that all act as
                delimiters (see
                [split_on_token][antlrope.FacadeListener.split_on_token]).
            where: `"before"` starts a new chunk at each delimiter; `"after"` ends a
                chunk at each delimiter (see `split_on_token`).
            encoding: The source encoding. Only UTF-8 is supported today (Python codec
                aliases such as `"utf8"` are accepted); the keyword is reserved so other
                encodings can be added later. For a non-UTF-8 source now, decode it in
                Python (`Path(p).read_text(encoding=...)`) and use the in-memory
                [split_on_token][antlrope.FacadeListener.split_on_token].
            channel: Only tokens on this channel are split on (default: the default
                channel). Pass `None` to consider all channels.
            sourcename: Source name recorded on each [Chunk][antlrope.Chunk] (and
                surfaced as [sourcename][antlrope.FacadeListener.sourcename] during
                a walk). Defaults to `str(path)`.
            trim: When `True` (the default) each chunk is stripped of surrounding
                whitespace and whitespace-only regions are dropped. Pass `False`
                to keep every region verbatim, including a leading or trailing
                whitespace terminator such as a record's mandatory newline. Only
                empty (zero-length) regions are then dropped.
            batch: How many chunk records to fetch from C++ per call. This affects
                throughput only, not the output.
            _block_bytes: Internal test hook: the file read-block size in bytes
                (0 = the default). Not part of the public API.

        Yields:
            One [Chunk][antlrope.Chunk] per region between delimiters, carrying its
            source position; by default trimmed of surrounding whitespace with
            whitespace-only regions skipped (see `trim`). Equivalent to
            `split_on_token` over the file's text.

        Raises:
            ValueError: If `where` is not `"before"` or `"after"`, or `encoding` is not
                UTF-8.
        """
        if where not in ("before", "after"):
            raise ValueError(f"where must be 'before' or 'after', got {where!r}")
        # Reserve the keyword for future encodings while only UTF-8 is implemented.
        # codecs.lookup normalizes aliases ("utf8", "UTF-8", "U8") to "utf-8".
        if codecs.lookup(encoding).name != "utf-8":
            raise ValueError(
                f"stream_on_token currently supports only UTF-8, got {encoding!r}; "
                f"decode in Python and use split_on_token for other encodings"
            )
        src_path = os.fspath(path)
        if not sourcename:
            sourcename = src_path
        spec = cls._lexer_spec()
        chunker = _native.StreamChunker(
            spec,
            src_path,
            list(_as_set(token_types)),
            0 if where == "before" else 1,
            channel,
            True,  # lenient: substitute U+FFFD for malformed bytes
            trim,
            _block_bytes,
        )
        more = True
        while more:
            rows, more = chunker.next_batch(batch)
            for offset, line, column, body in rows:
                yield Chunk(body, offset, line, column, sourcename)

    @classmethod
    def split_between_tokens(
        cls,
        text: str,
        pairs: Pair | list[Pair],
        *,
        nested: bool = False,
        channel: int | None = DEFAULT_CHANNEL,
        sourcename: str = "",
        trim: bool = True,
    ) -> Iterator[Chunk]:
        """Yield a chunk for each region bounded by an opener and closer pair.

        Token-aware, like [split_on_token][antlrope.FacadeListener.split_on_token]:
        it lexes the whole input, so a bracket inside a string or comment is ignored,
        at the cost of being slower than a plain regex over the text (see the
        "Chunking: lexer vs regex" notes in `docs/performance.md`).

        Args:
            text: The source to split.
            pairs: One `(open, close)` pair, or a list of them. Each side is a token
                type or several equivalent types (e.g. `(MyLexer.LBRACE,
                MyLexer.RBRACE)`, or `({MyLexer.BEGIN, MyLexer.DO}, {MyLexer.END})`).
                With multiple pairs (e.g. `[(LPAREN, RPAREN), (LBRACK, RBRACK)]`) each
                opener is matched only by a closer from its own pair, so distinct
                bracket kinds nest correctly. Open and close types should be disjoint.
            nested: When `True`, treat regions as balanced: match openers to closers
                by depth (respecting pair identity) and emit the outermost regions.
                When `False` (default), each opener pairs with the next closer of its
                pair and scanning resumes after it.
            channel: Only tokens on this channel are considered (default: the default
                channel). Pass `None` to consider all channels.
            sourcename: Optional source name (e.g. a filename) recorded on each
                [Chunk][antlrope.Chunk], surfaced during a walk as
                [sourcename][antlrope.FacadeListener.sourcename].
            trim: When `True` (the default) the region is stripped of surrounding
                whitespace; pass `False` to keep it verbatim. The span runs from
                the opener to the closer, so there is rarely any to strip.

        Yields:
            One [Chunk][antlrope.Chunk] per region (the text from the opener's
            start to the closer's stop, inclusive), in source order. An unmatched
            opener yields nothing; a closer with no matching opener is ignored.
        """
        norm = _normalize_pairs(pairs)
        open_to_pair: dict[int, int] = {}
        close_to_pair: dict[int, int] = {}
        keep: set[int] = set()
        for i, (opens, closes) in enumerate(norm):
            for o in opens:
                open_to_pair[o] = i
                keep.add(o)
            for c in closes:
                close_to_pair[c] = i
                keep.add(c)
        toks = [
            t
            for t in cls.lex(text, keep=keep)
            if channel is None or t.channel == channel
        ]
        sm = SourceMap(text)
        n = len(toks)
        if nested:
            stack: list[int] = []  # pair ids of the currently-open brackets
            start: int | None = None
            for idx in range(n):
                ttype = toks[idx].type
                if ttype in open_to_pair:
                    if not stack:
                        start = idx
                    stack.append(open_to_pair[ttype])
                elif ttype in close_to_pair:
                    # Only a closer matching the innermost opener pops the stack;
                    # a mismatched or stray closer is ignored.
                    if stack and stack[-1] == close_to_pair[ttype]:
                        stack.pop()
                        if not stack and start is not None:
                            chunk = Chunk._from_span(
                                text,
                                sm,
                                toks[start].start,
                                toks[idx].stop + 1,
                                sourcename,
                                trim=trim,
                            )
                            if chunk is not None:
                                yield chunk
                            start = None
        else:
            idx = 0
            while idx < n:
                pair_id = open_to_pair.get(toks[idx].type)
                if pair_id is not None:
                    j = idx + 1
                    while j < n and close_to_pair.get(toks[j].type) != pair_id:
                        j += 1
                    if j >= n:
                        break  # opener with no matching closer
                    chunk = Chunk._from_span(
                        text,
                        sm,
                        toks[idx].start,
                        toks[j].stop + 1,
                        sourcename,
                        trim=trim,
                    )
                    if chunk is not None:
                        yield chunk
                    idx = j + 1
                else:
                    idx += 1

    @classmethod
    def split_on_pattern(
        cls,
        text: str,
        pattern: str | re.Pattern[str],
        *,
        where: str = "before",
        flags: int | re.RegexFlag = 0,
        sourcename: str = "",
        trim: bool = True,
    ) -> Iterator[Chunk]:
        """Split `text` into chunks at each match of a delimiter regex.

        The regex analogue of
        [split_on_token][antlrope.FacadeListener.split_on_token]. No lexer is
        involved, so it is much faster: roughly an order of magnitude faster at
        finding delimiters and a few times faster end to end (the per-chunk Python
        work is the same). However, it is not token-aware, so a match inside a string
        or comment still counts as a delimiter. Use it when the delimiter text cannot
        appear inside other tokens; otherwise use the token-based splitter. See the
        "Chunking: lexer vs regex" notes in `docs/performance.md`.

        Args:
            text: The source to split.
            pattern: The delimiter regular expression (a `str` or compiled pattern).
            where: `"before"` starts each chunk at a match; `"after"` ends each chunk
                at a match (see `split_on_token`).
            flags: `re` flags, used only when `pattern` is a `str`.
            sourcename: Optional source name (e.g. a filename) recorded on each
                [Chunk][antlrope.Chunk], surfaced during a walk as
                [sourcename][antlrope.FacadeListener.sourcename].
            trim: When `True` (the default) each chunk is stripped of surrounding
                whitespace and whitespace-only regions are dropped. Pass `False`
                to keep every region verbatim, including a leading or trailing
                whitespace terminator such as a record's mandatory newline. Only
                empty (zero-length) regions are then dropped.

        Yields:
            One [Chunk][antlrope.Chunk] per region between matches, carrying its
            source position; by default trimmed of surrounding whitespace with
            whitespace-only regions skipped (see `trim`).
        """
        if where not in ("before", "after"):
            raise ValueError(f"where must be 'before' or 'after', got {where!r}")
        rx = re.compile(pattern, flags) if isinstance(pattern, str) else pattern
        sm = SourceMap(text)
        matches = list(rx.finditer(text))
        n = len(text)
        if where == "before":
            first = matches[0].start() if matches else n
            if first > 0:  # leading region, before the first match
                chunk = Chunk._from_span(text, sm, 0, first, sourcename, trim=trim)
                if chunk is not None:
                    yield chunk
            for i, m in enumerate(matches):
                end = matches[i + 1].start() if i + 1 < len(matches) else n
                chunk = Chunk._from_span(
                    text, sm, m.start(), end, sourcename, trim=trim
                )
                if chunk is not None:
                    yield chunk
        else:  # after
            prev = 0
            for m in matches:
                chunk = Chunk._from_span(text, sm, prev, m.end(), sourcename, trim=trim)
                if chunk is not None:
                    yield chunk
                prev = m.end()
            if prev < n:  # trailing region, after the last match
                chunk = Chunk._from_span(text, sm, prev, n, sourcename, trim=trim)
                if chunk is not None:
                    yield chunk

    @classmethod
    def stream_on_pattern(
        cls,
        source: TextSource,
        pattern: str | re.Pattern[str],
        *,
        where: str = "before",
        flags: int | re.RegexFlag = 0,
        window_chars: int | None = 65536,
        window_lines: int | None = None,
        encoding: str = "utf-8",
        sourcename: str = "",
        trim: bool = True,
    ) -> Iterator[Chunk]:
        """Split a text source into chunks at each regex match, reading incrementally.

        The streaming counterpart of
        [split_on_pattern][antlrope.FacadeListener.split_on_pattern]: it reads
        `source` incrementally and yields positioned [Chunk][antlrope.Chunk]s
        without holding the whole input, so paired with
        [walk_parallel][antlrope.FacadeListener.walk_parallel] the pipeline stays
        bounded. Because matching uses Python's `re`, decoding also happens in Python,
        so any encoding that Python text files support works. (The lexer-based
        [stream_on_token][antlrope.FacadeListener.stream_on_token] reads UTF-8 in
        C++.)

        A delimiter match is only committed once a character past it has been read (or
        the source ends), so a match is never split across a read boundary, provided
        the delimiter fits within the read window. A region with no delimiter is
        buffered in full (like the other streamers).

        Args:
            source: A filesystem `path` (opened with `encoding`), an already-open text
                file object, or any iterable of `str` pieces (e.g. lines, or a
                generator). For an in-memory string, use `split_on_pattern` instead.
            pattern: The delimiter regular expression (a `str` or compiled pattern).
            where: `"before"` starts each chunk at a match; `"after"` ends each chunk at
                a match (see `split_on_pattern`).
            flags: `re` flags, used only when `pattern` is a `str`.
            window_chars: How many characters to read and search per step (default
                64K). Controls how often the regex re-runs and how much read-ahead is
                buffered, not the output. Used when reading a path or file object.
            window_lines: If set, read this many lines per step instead (still capped by
                `window_chars` if both are given). Useful for line-oriented
                delimiters. Ignored for a plain `str` iterable, which is consumed as-is.
            encoding: Text encoding, used only when `source` is a path. Any codec
                Python supports.
            sourcename: Source name recorded on each [Chunk][antlrope.Chunk] (and
                surfaced as [sourcename][antlrope.FacadeListener.sourcename] during
                a walk). Defaults to the path when `source` is a path and to an empty
                string otherwise, so pass it for a stream or iterable.
            trim: When `True` (the default) each chunk is stripped of surrounding
                whitespace and whitespace-only regions are dropped. Pass `False`
                to keep every region verbatim, including a leading or trailing
                whitespace terminator such as a record's mandatory newline. Only
                empty (zero-length) regions are then dropped.

        Yields:
            One [Chunk][antlrope.Chunk] per region between matches, carrying its
            source position; by default trimmed of surrounding whitespace with
            whitespace-only regions skipped (see `trim`). Equivalent to
            `split_on_pattern` over the source's decoded text.

        Raises:
            ValueError: If `where` is not `"before"` or `"after"`.
        """
        if where not in ("before", "after"):
            raise ValueError(f"where must be 'before' or 'after', got {where!r}")
        rx = re.compile(pattern, flags) if isinstance(pattern, str) else pattern

        opened = None
        if isinstance(source, (str, os.PathLike)):
            fspath = os.fspath(source)
            if not sourcename:
                sourcename = fspath
            opened = open(fspath, encoding=encoding)  # noqa: SIM115
            increments = _read_increments(opened, window_chars, window_lines)
        elif hasattr(source, "read"):
            increments = _read_increments(source, window_chars, window_lines)
        else:
            increments = iter(source)
        try:
            yield from Chunk._split_stream(increments, rx, where, sourcename, trim)
        finally:
            if opened is not None:
                opened.close()

    @classmethod
    def chunk_by_pattern(
        cls,
        text: str,
        pattern: str | re.Pattern[str],
        *,
        flags: int | re.RegexFlag = 0,
        sourcename: str = "",
        trim: bool = True,
    ) -> Iterator[Chunk]:
        """Yield one chunk per non-overlapping match of `pattern`.

        Here the pattern matches a whole record (rather than a delimiter), so each
        match is a chunk and the text between matches is dropped. No lexer is
        involved, so it is fast but not token-aware. See
        [split_on_pattern][antlrope.FacadeListener.split_on_pattern] and the
        "Chunking: lexer vs regex" notes in `docs/performance.md` for the
        trade-off between speed and correctness.

        Args:
            text: The source to split.
            pattern: A regular expression matching one record (a `str` or compiled
                pattern).
            flags: `re` flags, used only when `pattern` is a `str`.
            sourcename: Optional source name (e.g. a filename) recorded on each
                [Chunk][antlrope.Chunk], surfaced during a walk as
                [sourcename][antlrope.FacadeListener.sourcename].
            trim: When `True` (the default) each match is stripped of surrounding
                whitespace and a whitespace-only match is dropped. Pass `False` to
                keep each match verbatim. Only empty (zero-length) matches are then
                dropped.

        Yields:
            One [Chunk][antlrope.Chunk] per match, carrying its source position; by
            default trimmed of surrounding whitespace with whitespace-only matches
            skipped (see `trim`).
        """
        rx = re.compile(pattern, flags) if isinstance(pattern, str) else pattern
        sm = SourceMap(text)
        for m in rx.finditer(text):
            chunk = Chunk._from_span(
                text, sm, m.start(), m.end(), sourcename, trim=trim
            )
            if chunk is not None:
                yield chunk

    @classmethod
    def chunk_by_rule(
        cls,
        text: str,
        rule: RuleTypes,
        *,
        start_rule: str | int | None = None,
        outermost: bool = True,
        sourcename: str = "",
    ) -> Iterator[Chunk]:
        """Yield each occurrence of a grammar `rule` as a chunk.

        Unlike the token and regex chunkers, this parses the whole input with the
        grammar's `ParserInterpreter` to find where the rule occurs, which makes it the
        structural counterpart to those heuristic splitters. The parse runs entirely
        in C++ and only the `(start, stop)` spans cross into Python, so it stays cheap
        compared with the per-chunk `walk_parallel` parse it feeds. Use it when the
        per-chunk callback work dominates, or when no token or regex delimiter cleanly
        marks a record. See `docs/performance.md`.

        Args:
            text: The source to split.
            rule: The rule to chunk by: a rule name or index, or several of them
                (e.g. `"function"`, or `{"function", "class"}`).
            start_rule: The rule to parse the whole input as: a name, an index, or
                `None` for the grammar's start rule (index 0).
            outermost: When `True` (default), only top-level occurrences are emitted;
                a matched rule nested inside another match is skipped. `False` emits
                every occurrence, so nested occurrences overlap.
            sourcename: Optional source name (e.g. a filename) recorded on each
                [Chunk][antlrope.Chunk], surfaced during a walk as
                [sourcename][antlrope.FacadeListener.sourcename].

        Yields:
            One [Chunk][antlrope.Chunk] per matched rule occurrence, in source
            order, trimmed of surrounding whitespace and carrying its position. An
            empty occurrence (a rule that consumed no token) is skipped.
        """
        parser_spec = cls._parser_spec()
        lexer_spec = cls._lexer_spec()
        if isinstance(rule, (int, str)):
            rule_mask = [cls._resolve_rule(rule)]
        else:
            rule_mask = [cls._resolve_rule(r) for r in rule]
        start_idx = 0 if start_rule is None else cls._resolve_rule(start_rule)

        raw, _errors = _native.rule_spans(
            parser_spec, lexer_spec, text, start_idx, rule_mask, outermost
        )
        sm = SourceMap(text)
        for _ridx, start, stop in _RULE.iter_unpack(raw):
            if start < 0:  # empty rule occurrence, with no source span
                continue
            chunk = Chunk._from_span(text, sm, start, stop + 1, sourcename)
            if chunk is not None:
                yield chunk

    @classmethod
    def stream_by_rule(
        cls,
        path: str | os.PathLike[str],
        rule: RuleTypes,
        *,
        sourcename: str = "",
        encoding: str = "utf-8",
        batch: int = 256,
        _block_bytes: int = 0,
    ) -> Iterator[Chunk]:
        """Stream chunks from a file that is a sequence of a grammar `rule`.

        The streaming counterpart of
        [chunk_by_rule][antlrope.FacadeListener.chunk_by_rule], for input that is a
        top-level sequence of records, each an occurrence of `rule` (or of one of
        several rules). It parses one record at a time over a bounded-memory
        pipeline (the native layer opens the file and runs lexer → parser over a
        sliding window), yielding each as a positioned [Chunk][antlrope.Chunk]
        without holding the whole token stream or parse tree. The chunks feed
        [walk_parallel][antlrope.FacadeListener.walk_parallel] like any other.

        Unlike `chunk_by_rule`, which parses the whole input and finds the rule
        anywhere in the tree, this form is for a bounded-memory "file of records".
        Records must be directly adjacent: only lexer-skipped tokens (whitespace,
        comments) may appear between them. With several candidate rules, the next
        token decides which one to parse (using each rule's start-token set), so the
        candidates should begin with different tokens (e.g. `class` vs `def`). If
        they overlap, the first listed rule wins. An on-channel separator between
        records (e.g. a comma) is not supported; use `chunk_by_rule` or
        [stream_on_token][antlrope.FacadeListener.stream_on_token] instead.

        Args:
            path: Filesystem path to the source (opened by the native layer as UTF-8).
            rule: The record rule: a rule name or index, or several of them (a set of
                top-level record types, e.g. `{"classdef", "funcdef"}`).
            sourcename: Source name recorded on each [Chunk][antlrope.Chunk] (and
                surfaced as [sourcename][antlrope.FacadeListener.sourcename] during a
                walk). Defaults to `str(path)`.
            encoding: The source encoding. Only UTF-8 is supported today (Python codec
                aliases are accepted); the keyword is reserved for future encodings.
            batch: How many records to fetch from C++ per call. This affects throughput
                only.
            _block_bytes: Internal test hook: the file read-block size in bytes
                (0 = the default). Not part of the public API.

        Yields:
            One [Chunk][antlrope.Chunk] per record, in source order, carrying its
            position. The whole input must be a sequence of records: the stream stops
            cleanly only at end of input (trailing whitespace and comments that the
            lexer skips are fine).

        Raises:
            ValueError: If `encoding` is not UTF-8, or a rule name is unknown.
            RuntimeError: If an on-channel token begins no candidate rule (e.g. an
                unsupported header or record separator) or a chosen record fails to
                make progress. The message names the offending token and the
                candidate rules. Records parsed before the offending token are
                yielded first, so the error is raised only after them.
        """
        if codecs.lookup(encoding).name != "utf-8":
            raise ValueError(
                f"stream_by_rule currently supports only UTF-8, got {encoding!r}; "
                f"decode in Python and use chunk_by_rule for other encodings"
            )
        if isinstance(rule, (int, str)):
            rule_indices = [cls._resolve_rule(rule)]
        else:
            rule_indices = [cls._resolve_rule(r) for r in rule]
        src_path = os.fspath(path)
        if not sourcename:
            sourcename = src_path
        parser_spec = cls._parser_spec()
        lexer_spec = cls._lexer_spec()
        chunker = _native.StreamRuleChunker(
            parser_spec,
            lexer_spec,
            src_path,
            rule_indices,
            True,  # lenient: substitute U+FFFD for malformed bytes
            _block_bytes,
        )
        more = True
        while more:
            rows, more = chunker.next_batch(batch)
            for offset, line, column, body in rows:
                yield Chunk(body, offset, line, column, sourcename)
