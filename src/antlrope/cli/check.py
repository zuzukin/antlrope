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

"""The `check` subcommand: flag grammars the ATN interpreter cannot run faithfully.

Antlrope drives the interpreted ATN, which does not execute target-language code
embedded in a grammar: semantic predicates (`{...}?`) and embedded actions
(`{...}`). A grammar whose parse depends on them mis-parses silently (see the
"Performance & limitations" docs). This command scans the serialized ATNs of a
generated parser and lexer for those constructs and reports the rules that carry
them, exiting non-zero when any are found.

What is (and is not) flagged:

- parser ATN: semantic-predicate and embedded-action transitions. Precedence
  predicates (from left-recursive rules) are not flagged, because the interpreter
  evaluates them itself.
- lexer ATN: semantic predicates and custom `{...}` lexer actions. The built-in
  lexer commands (`-> skip`, `-> channel(...)`, `-> mode(...)`, `-> more`, ...)
  compile to actions the interpreter executes, so they are not flagged.
"""

from __future__ import annotations

import argparse
import sys
from textwrap import dedent

from antlr4.atn.ATNDeserializer import ATNDeserializer
from antlr4.atn.LexerAction import LexerCustomAction
from antlr4.atn.Transition import ActionTransition, PredicateTransition

from antlrope.cli.generate import _derive_lexer, _import_class

_DOCS_URL = "https://zuzukin.github.io/antlrope/performance/"


def _rule_name(rule_names: list[str], index: int) -> str:
    return rule_names[index] if 0 <= index < len(rule_names) else f"<rule {index}>"


def scan_class(cls: type, *, is_lexer: bool) -> list[str]:
    """Return a finding per grammar rule using an interpreter-incompatible construct.

    Deserializes the class's module-level serialized ATN with the official Python
    runtime and walks its transitions (`sys.modules[cls.__module__]` is where the
    generated `serializedATN()` lives).
    """
    mod = sys.modules[cls.__module__]
    atn = ATNDeserializer().deserialize(mod.serializedATN())
    rule_names = list(cls.ruleNames)
    kind = "lexer" if is_lexer else "parser"

    findings: dict[tuple[str, int], str] = {}  # (construct, rule) -> message, deduped
    for state in atn.states:
        for transition in state.transitions:
            if isinstance(transition, PredicateTransition):
                findings[("pred", transition.ruleIndex)] = (
                    f"semantic predicate in {kind} rule "
                    f"'{_rule_name(rule_names, transition.ruleIndex)}'"
                )
            # In a lexer ATN an ActionTransition also carries the built-in
            # commands (skip/channel/mode/...), which the interpreter executes;
            # only the parser's embedded {...} actions are flagged here. Custom
            # lexer {...} actions are found via atn.lexerActions below.
            elif isinstance(transition, ActionTransition) and not is_lexer:
                findings[("action", transition.ruleIndex)] = (
                    f"embedded action in {kind} rule "
                    f"'{_rule_name(rule_names, transition.ruleIndex)}'"
                )
    for action in getattr(atn, "lexerActions", None) or []:
        if isinstance(action, LexerCustomAction):
            findings[("action", action.ruleIndex)] = (
                f"custom action in {kind} rule "
                f"'{_rule_name(rule_names, action.ruleIndex)}'"
            )
    return [findings[key] for key in sorted(findings)]


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the `check` subcommand to the top-level `antlrope` parser."""
    parser = subparsers.add_parser(
        "check",
        help="Check a grammar for semantic predicates and embedded actions.",
        description=dedent(
            """
            Scan a generated parser and lexer for semantic predicates and
            embedded actions. The interpreted ATN cannot execute them, so a
            grammar whose parse depends on them silently mis-parses under
            antlrope. Exits with status 0 if none are found, 1 if any are
            found, and 2 on a usage error or if a module cannot be imported.
            """
        ),
    )
    parser.add_argument(
        "parser_module",
        metavar="<parser-module>",
        help="Importable dotted path to the generated parser module "
        "(e.g. mypkg.generated.MyParser).",
    )
    parser.add_argument(
        "--lexer",
        metavar="<lexer-module>",
        help="Importable dotted path to the generated lexer module. Defaults to the "
        "parser path with a trailing 'Parser' replaced by 'Lexer'.",
    )
    parser.set_defaults(main=main)


def main(args: argparse.Namespace) -> int:
    try:
        lexer_qualname = args.lexer or _derive_lexer(args.parser_module)
    except ValueError as e:
        print(f"antlrope check: {e}", file=sys.stderr)
        return 2
    parser_cls = _import_class(args.parser_module)
    lexer_cls = _import_class(lexer_qualname)

    findings = scan_class(parser_cls, is_lexer=False)
    findings += scan_class(lexer_cls, is_lexer=True)
    if findings:
        print(f"{args.parser_module}: INCOMPATIBLE with the ATN interpreter")
        for finding in findings:
            print(f"  {finding}")
        print(
            "  The interpreted ATN cannot execute these, so parses that depend on\n"
            f"  them silently mis-parse. See {_DOCS_URL}. Use the official\n"
            "  antlr4-python3-runtime for this grammar, or restructure it to avoid\n"
            "  predicates and actions."
        )
        return 1
    print(f"{args.parser_module}: OK: no semantic predicates or embedded actions")
    return 0
