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

"""The `up-to-date` subcommand: check a generated facade against its inputs.

Re-hashes the input modules recorded in a facade's origin header (see
antlrope.cli.metadata) and compares them, along with the antlrope version, to the
recorded values. It only hashes files and never imports or parses the grammar. The
exit status is 0 when the facade is current, 1 when it is stale, and 2 on a usage
error, which suits CI and Make.
"""

from __future__ import annotations

import argparse
import os
import sys
from textwrap import dedent

from antlrope import __version__
from antlrope.cli import metadata


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the `up-to-date` subcommand to the top-level `antlrope` parser."""
    parser = subparsers.add_parser(
        "up-to-date",
        help="Check whether a generated facade is current with its inputs.",
        description=dedent(
            """
            Compare the input SHA256 hashes and antlrope version recorded in
            a generated facade against the current files. Exits with status 0
            if it is up to date, 1 if it is stale, and 2 on a usage error or if
            the file cannot be read or has no antlrope metadata.
            """
        ),
    )
    parser.add_argument(
        "file", metavar="<file>", help="A facade previously written by `antlrope gen`."
    )
    parser.set_defaults(main=main)


def main(args: argparse.Namespace) -> int:
    with open(args.file, encoding="utf-8") as fh:
        meta = metadata.parse(fh.read())
    if meta is None:
        print(
            f"antlrope up-to-date: no antlrope metadata found in {args.file}",
            file=sys.stderr,
        )
        return 2

    base = os.path.dirname(os.path.abspath(args.file))
    rundir = meta.rundir or "."
    stale: list[str] = []
    for path, recorded in meta.inputs:
        actual = os.path.join(base, rundir, path)
        if not os.path.exists(actual):
            stale.append(f"missing input: {path}")
        elif metadata.sha256_file(actual) != recorded:
            stale.append(f"changed input: {path}")
    if meta.version != __version__:
        stale.append(
            f"antlrope version: generated with {meta.version}, now {__version__}"
        )

    if stale:
        print(f"{args.file}: STALE")
        for reason in stale:
            print(f"  {reason}")
        return 1
    print(f"{args.file}: up to date")
    return 0
