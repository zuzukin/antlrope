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

"""The `rules` subcommand: list a generated parser's rule names.

These are the names accepted by `walk(start_rule=...)` and the rule-based
chunkers (`chunk_by_rule`, `stream_by_rule`). Plain text (one `index<TAB>name`
line per rule) by default, or a JSON array of names with `--json` (the index is
the position).
"""

from __future__ import annotations

import argparse
import json

from antlrope.cli.generate import _import_class


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the `rules` subcommand to the top-level `antlrope` parser."""
    parser = subparsers.add_parser(
        "rules",
        help="List a parser's rule names (for start_rule and the rule-based chunkers).",
        description="List the parser rule names of a generated parser module, "
        "as accepted by walk(start_rule=...), chunk_by_rule, and stream_by_rule.",
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
        help="Emit a JSON array of rule names (the index is the position).",
    )
    parser.set_defaults(main=main)


def main(args: argparse.Namespace) -> int:
    rule_names = list(_import_class(args.parser_module).ruleNames)
    if args.json:
        print(json.dumps(rule_names, indent=2))
    else:
        for index, name in enumerate(rule_names):
            print(f"{index}\t{name}")
    return 0
