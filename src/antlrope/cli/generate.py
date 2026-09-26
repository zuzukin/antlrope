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

"""The `gen` subcommand: generate a grammar-specific event-listener facade.

Reads `ruleNames` and the token name lists from an already-generated ANTLR Python
parser module (no annotated grammar, no extra inputs) and emits a
`<Grammar>EventListener` base class with no-op `enter<Rule>`, `exit<Rule>`,
`visitTerminal`, and `visitError` stubs and the token-type constants. The class
subclasses [FacadeListener][antlrope.FacadeListener], from which it inherits
`walk` (which runs the bulk native event stream) and `self.line_col()` (the
current event's source position).

The generated interface mirrors the stock ANTLR listener, so consumers write the
same code. The difference is that a flat event buffer drives the callbacks instead
of a Python parse-tree walk. Usage (console script or module):

    antlrope gen mypkg.generated.MyParser My -o my_listener.py
"""

from __future__ import annotations

import argparse
import importlib
import os
import shlex
import sys
from textwrap import dedent

from antlrope import __version__
from antlrope.cli.metadata import (
    BANNER,
    Metadata,
    input_digests,
    relpath_or_abs,
    render,
)


def _cap_first(name: str) -> str:
    """Return `name` with its first character uppercased and the rest unchanged."""
    return name[0].upper() + name[1:]


# black/ruff wrap a collection literal across multiple lines once the statement
# exceeds this width (their shared default). Matching it lets the generator emit
# ruff-format-clean source directly, with no formatting pass.
_LINE_LENGTH = 88


def _rule_names_block(rule_names: list[str]) -> str:
    """Render the `ruleNames` class attribute as ruff/black-formatted source.

    A single line when the whole statement fits the line-length budget, otherwise
    the one-item-per-line form with the trailing comma that ruff and black would
    add (the magic trailing comma then keeps it stable). Rule names are ANTLR
    identifiers, so the double-quoted literals need no escaping. `repr()` would
    emit single quotes, which the formatter would then rewrite.
    """
    indent = "    "
    prefix = f"{indent}ruleNames: ClassVar[list[str]] = "
    items = [f'"{name}"' for name in rule_names]
    single = f"{prefix}[{', '.join(items)}]"
    if len(single) <= _LINE_LENGTH:
        return single
    body = "".join(f"{indent}{indent}{item},\n" for item in items)
    return f"{prefix}[\n{body}{indent}]"


def _derive_lexer(parser_qualname: str) -> str:
    """Derive the lexer module path from the parser's, by ANTLR convention.

    ANTLR names a combined grammar's classes `<Grammar>Lexer` and `<Grammar>Parser`
    in same-named modules, so the lexer path is the parser path with the trailing
    `Parser` replaced by `Lexer` (e.g. `pkg.JSONParser` -> `pkg.JSONLexer`).

    Raises:
        ValueError: If the parser module path's final component does not end in
            `Parser`, so the lexer cannot be derived. Pass `--lexer` explicitly.
    """
    if not parser_qualname.rsplit(".", 1)[-1].endswith("Parser"):
        raise ValueError(
            f"cannot derive the lexer from parser module {parser_qualname!r} "
            "(its name does not end in 'Parser'); pass --lexer with the lexer's "
            "dotted module path."
        )
    return parser_qualname[: -len("Parser")] + "Lexer"


def _import_class(qualname: str) -> type:
    """Import the dotted module path and return its same-named class."""
    mod = importlib.import_module(qualname)
    return getattr(mod, qualname.rsplit(".", 1)[-1])


def token_constants(parser_cls: type) -> list[tuple[str, int]]:
    """Return the facade's `(name, token_type)` constants for a parser class.

    ANTLR names anonymous string-literal tokens positionally: `T__0` is the
    first such token (token type 1), `T__1` the second (type 2), and so on. The
    name therefore does not equal the token-type value. These names are read from
    the generated parser, which already declares them, so the facade's constants
    match the user's lexer and parser instead of using synthesized names. Each
    token type maps to its symbolic name when ANTLR gave one, and otherwise to the
    parser's own positional `T__n` name for the anonymous literal.
    """
    literal_consts = {
        value: name
        for name, value in vars(parser_cls).items()
        if name.startswith("T__") and isinstance(value, int)
    }
    tok_consts: list[tuple[str, int]] = []
    for ttype, sym in enumerate(parser_cls.symbolicNames):
        if ttype == 0:
            continue  # type 0 is ANTLR's invalid token type (EOF is -1)
        if sym and sym != "<INVALID>":
            tok_consts.append((sym, ttype))
        elif ttype in literal_consts:
            tok_consts.append((literal_consts[ttype], ttype))
    return tok_consts


def generate(
    parser_qualname: str,
    grammar: str,
    lexer_qualname: str | None = None,
    *,
    metadata: str | None = None,
) -> str:
    parser_cls = _import_class(parser_qualname)
    rule_names: list[str] = list(parser_cls.ruleNames)

    if lexer_qualname is None:
        lexer_qualname = _derive_lexer(parser_qualname)
    # Import the lexer too so a wrong or missing path fails here, at generation time,
    # with a clear error rather than as an ImportError in the user's generated file.
    _import_class(lexer_qualname)
    parser_clsname = parser_qualname.rsplit(".", 1)[-1]
    lexer_clsname = lexer_qualname.rsplit(".", 1)[-1]

    listener_cls = f"{grammar.capitalize()}EventListener"

    tok_consts = token_constants(parser_cls)

    # token_lines and rule_methods carry their own class-body indentation; the
    # template's placeholders sit at the base column so the content lands right.
    token_lines = "\n".join(f"    {name} = {ttype}" for name, ttype in tok_consts)
    rule_methods = "".join(
        f"""    def enter{cap}(self) -> None:
        pass

    def exit{cap}(self) -> None:
        pass

"""
        for cap in map(_cap_first, rule_names)
    )

    rule_names_block = _rule_names_block(rule_names)

    # The origin header (antlrope.cli.metadata) when the CLI provides one, else
    # the bare banner, which keeps a direct generate() call deterministic and free
    # of version details.
    header = metadata if metadata is not None else BANNER

    # dedent() must run before .format(): an f-string would interpolate the
    # multi-line token_lines and rule_methods first, and their lower indentation would
    # then throw off dedent's common-prefix calculation.
    return dedent(
        '''\
        {metadata}
        """
        Event-listener facade for the {grammar} grammar. Subclass {cls},
        override the callbacks you care about, then call .walk(text).
        """

        from __future__ import annotations

        from typing import ClassVar

        from {lexer_qualname} import {lexer_clsname}
        from {parser_qualname} import {parser_clsname}

        from antlrope import FacadeListener


        class {cls}(FacadeListener):
        {rule_names_block}
            START_RULE = 0  # {rule0}
            _LEXER: ClassVar[type] = {lexer_clsname}
            _PARSER: ClassVar[type] = {parser_clsname}

            # token-type constants
        {token_lines}

        {rule_methods}    def visitTerminal(self, token_type: int, text: str) -> None:
                pass

            def visitError(self, token_type: int, text: str) -> None:
                pass
        '''
    ).format(
        metadata=header,
        grammar=grammar,
        cls=listener_cls,
        lexer_qualname=lexer_qualname,
        lexer_clsname=lexer_clsname,
        parser_qualname=parser_qualname,
        parser_clsname=parser_clsname,
        rule_names_block=rule_names_block,
        rule0=rule_names[0],
        token_lines=token_lines,
        rule_methods=rule_methods,
    )


def add_gen_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the `gen` positional and optional arguments (shared with `regen`)."""
    parser.add_argument(
        "parser_module",
        metavar="<parser-module>",
        help="Importable dotted path to the generated parser module "
        "(e.g. mypkg.generated.MyParser).",
    )
    parser.add_argument(
        "grammar",
        metavar="<name>",
        help="Grammar name used to prefix the facade class. It is passed through "
        "str.capitalize(), so JSON gives JsonEventListener.",
    )
    parser.add_argument(
        "--lexer",
        metavar="<lexer-module>",
        help="Importable dotted path to the generated lexer module. Defaults to the "
        "parser path with a trailing 'Parser' replaced by 'Lexer' "
        "(e.g. mypkg.generated.MyLexer); pass this when the lexer is named "
        "differently.",
    )
    parser.add_argument(
        "-o", "--output", metavar="<file>", help="Write to this file instead of stdout."
    )


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the `gen` subcommand to the top-level `antlrope` parser."""
    parser = subparsers.add_parser(
        "gen",
        aliases=["generate"],
        help="Generate a <Grammar>EventListener facade from a parser module.",
        description="Generate a <Grammar>EventListener facade from a "
        "stock-generated ANTLR Python parser module.",
    )
    add_gen_arguments(parser)
    parser.set_defaults(main=main)


def _build_metadata(
    parser_module: str,
    grammar: str,
    lexer: str | None,
    lexer_qualname: str,
    output: str | None,
) -> str:
    """Render the origin header for a `gen` invocation (antlrope.cli.metadata)."""
    command_parts = ["antlrope", "gen", parser_module, grammar]
    if lexer:
        command_parts += ["--lexer", lexer]
    if output:
        command_parts += ["-o", output]
    command = " ".join(shlex.quote(part) for part in command_parts)
    rundir = None
    if output:
        # No relative form across Windows drives: record the cwd absolute then.
        rundir = relpath_or_abs(os.getcwd(), os.path.dirname(os.path.abspath(output)))
    inputs = input_digests(parser_module, lexer_qualname)
    return render(
        Metadata(version=__version__, command=command, rundir=rundir, inputs=inputs)
    )


def run_gen(
    parser_module: str, grammar: str, lexer: str | None, output: str | None
) -> int:
    """Generate the facade (with an origin header) and write it to a file or stdout."""
    lexer_qualname = lexer or _derive_lexer(parser_module)
    metadata = _build_metadata(parser_module, grammar, lexer, lexer_qualname, output)
    source = generate(parser_module, grammar, lexer, metadata=metadata)
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(source)
    else:
        sys.stdout.write(source)
    return 0


def main(args: argparse.Namespace) -> int:
    return run_gen(args.parser_module, args.grammar, args.lexer, args.output)
