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

"""Generate the command reference in `docs/reference/cli.md` from live `--help`.

Builds the real argparse parser, captures `format_help()` for `antlrope` and each
subcommand, and reformats it as Markdown (section labels such as `options:` and
`positional arguments:` become `###` headers and the rest goes into `text` code
blocks). It then rewrites the region between the `gen-cli-help` markers in cli.md.
Because it reads the actual CLI, the reference can never drift from it.

Run `pixi run gen-cli-docs`; pass `--check` to fail (exit 1) when the committed doc
is out of date instead of rewriting it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from antlrope.cli.main import build_parser

_DOC = Path(__file__).resolve().parents[1] / "docs" / "reference" / "cli.md"
_START = (
    "<!-- gen-cli-help: start (managed by scripts/gen_cli_docs.py — do not edit) -->"
)
_END = "<!-- gen-cli-help: end -->"
# A help "section" is an unindented label line ending in a colon, e.g. `options:`.
_SECTION = re.compile(r"^([A-Za-z][\w ]*):$")


def _command_markdown(heading: str, help_text: str) -> str:
    """Reformat one command's `--help` output as a Markdown section."""
    md = [f"## {heading}", ""]
    body: list[str] = []

    def flush() -> None:
        text = "\n".join(body).strip("\n")
        body.clear()
        if text:
            md.extend(["```text", text, "```", ""])

    for line in help_text.rstrip("\n").splitlines():
        section = _SECTION.match(line)
        if section:
            flush()
            md.extend([f"### {section.group(1)}", ""])
        else:
            body.append(line)
    flush()
    return "\n".join(md).rstrip("\n")


def _subcommand_parsers(
    parser: argparse.ArgumentParser,
) -> list[argparse.ArgumentParser]:
    """The subcommand parsers, deduplicated (each appears once, under its primary name)."""
    sub = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None
    )
    seen: set[int] = set()
    result: list[argparse.ArgumentParser] = []
    if sub is not None:
        for subparser in sub.choices.values():
            if id(subparser) not in seen:
                seen.add(id(subparser))
                result.append(subparser)
    return result


def _render() -> str:
    # Force a stable width so the output is deterministic; the help formatter caps at
    # min(COLUMNS - 2, 90), so 120 yields the CLI's ~90-column cap.
    os.environ["COLUMNS"] = "120"
    parser = build_parser()
    sections = [_command_markdown(parser.prog, parser.format_help())]
    sections += [
        _command_markdown(sub.prog, sub.format_help())
        for sub in _subcommand_parsers(parser)
    ]
    return "\n\n".join(sections)


def _rewrite(text: str, body: str) -> str:
    pre, start, rest = text.partition(_START)
    _, end, post = rest.partition(_END)
    if not start or not end:
        sys.exit(f"gen_cli_docs: markers not found in {_DOC}")
    return f"{pre}{_START}\n\n{body}\n\n{_END}{post}"


def main(argv: list[str] | None = None) -> int:
    check = "--check" in (sys.argv[1:] if argv is None else argv)
    current = _DOC.read_text(encoding="utf-8")
    updated = _rewrite(current, _render())
    if check:
        if current != updated:
            sys.stderr.write(
                "docs/reference/cli.md is out of date; run `pixi run gen-cli-docs`\n"
            )
            return 1
        return 0
    _DOC.write_text(updated, encoding="utf-8")
    print(f"wrote {_DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
