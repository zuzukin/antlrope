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

"""The `tokens` subcommand: list a generated parser's token-type constants.

Exactly the constants the generated facade carries: each token type's symbolic
name, or ANTLR's positional `T__n` name for an anonymous string literal. Plain
text (one `type<TAB>name` line per token) by default, or a JSON object mapping
name to token type with `--json`.
"""

from __future__ import annotations

import argparse
import json

from antlrope.cli.generate import _import_class, token_constants


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the `tokens` subcommand to the top-level `antlrope` parser."""
    parser = subparsers.add_parser(
        "tokens",
        help="List a parser's token types and names (the facade's constants).",
        description="List the token types and names of a generated parser module: "
        "the symbolic names and the positional T__n names of anonymous literals, "
        "matching the generated facade's token-type constants.",
    )
    parser.add_argument(
        "parser_module",
        metavar="<parser-module>",
        help="Importable dotted path to the generated parser module "
        "(e.g. mypkg.generated.MyParser).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON object mapping token name to token type.",
    )
    parser.set_defaults(main=main)


def main(args: argparse.Namespace) -> int:
    consts = token_constants(_import_class(args.parser_module))
    if args.json:
        print(json.dumps(dict(consts), indent=2))
    else:
        for name, ttype in consts:
            print(f"{ttype}\t{name}")
    return 0
