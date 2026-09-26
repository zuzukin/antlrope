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

"""The `antlrope` command-line entry point: the command group and dispatch.

`antlrope` is a command group. Each subcommand lives in its own module under
`antlrope.cli` and exposes a `register(subparsers)` function; add new commands to
`_SUBCOMMANDS` below. Today these are `gen`, `regen`, `up-to-date`, `check`,
`rules`, and `tokens`.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from typing import Any

from antlrope import __version__
from antlrope.cli import check, generate, regen, rules, tokens, uptodate

# Each entry is a subcommand module exposing `register(subparsers)`.
_SUBCOMMANDS = (generate, regen, uptodate, check, rules, tokens)

# Cap help text at ~90 columns (never wider than the terminal) instead of letting it
# stretch across a wide window.
_HELP_WIDTH = 90


def _help_formatter(prog: str) -> argparse.HelpFormatter:
    width = min(shutil.get_terminal_size().columns - 2, _HELP_WIDTH)
    return argparse.HelpFormatter(prog, width=width)


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that caps help width. `add_subparsers` copies this class to the
    subparsers (via `type(self)`), so every subcommand's help is capped too."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", _help_formatter)
        super().__init__(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level `antlrope` parser with every subcommand registered."""
    parser = _Parser(
        prog="antlrope",
        description="Command-line tools for the antlrope ANTLR runtime.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for module in _SUBCOMMANDS:
        module.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:  # bare `antlrope`: show help and exit cleanly
        parser.print_help()
        return 0
    try:
        return args.main(args)
    except (ImportError, AttributeError, OSError, ValueError) as e:
        # Backstop for expected user-input failures (an unimportable module, a
        # module that isn't a generated parser, a file that can't be read or
        # written), so the CLI prints one clean line instead of a traceback.
        # Genuine bugs (any other exception type) still traceback.
        print(f"antlrope {args.command}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
