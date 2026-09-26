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

"""The `regen` subcommand: regenerate a facade in place from its embedded metadata.

Reads the origin header an earlier `antlrope gen` wrote into the file (see
antlrope.cli.metadata), re-runs that exact command from the recorded run directory
(relative to the file), and overwrites the file. Assumes the input modules are
importable from the run directory, as in the original invocation.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import sys
from textwrap import dedent

from antlrope.cli import generate, metadata


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the `regen` subcommand to the top-level `antlrope` parser."""
    parser = subparsers.add_parser(
        "regen",
        aliases=["regenerate"],
        help="Regenerate a facade in place from its embedded metadata.",
        description=dedent(
            """
            Re-run the `gen` command recorded in a generated facade's
            metadata header. The command runs from the recorded run directory
            and overwrites the file.
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
            f"antlrope regen: no antlrope metadata found in {args.file}",
            file=sys.stderr,
        )
        return 2
    if meta.rundir is None:
        print(
            f"antlrope regen: {args.file} records no run directory "
            "(was it generated to stdout?)",
            file=sys.stderr,
        )
        return 2

    tokens = shlex.split(meta.command)
    if (
        len(tokens) < 2
        or tokens[0] != "antlrope"
        or tokens[1] not in ("gen", "generate")
    ):
        print(
            f"antlrope regen: unrecognized command in metadata: {meta.command!r}",
            file=sys.stderr,
        )
        return 2
    arg_parser = argparse.ArgumentParser(prog="antlrope gen")
    generate.add_gen_arguments(arg_parser)
    gen_args = arg_parser.parse_args(tokens[2:])

    # Run from the recorded run directory (relative to the file), with that directory
    # importable, mirroring the original `PYTHONPATH=<rundir>` invocation. The output
    # path in the command is relative to that directory, so it lands back on the file.
    base = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(args.file)), meta.rundir)
    )
    old_cwd = os.getcwd()
    sys.path.insert(0, base)
    os.chdir(base)
    try:
        return generate.run_gen(
            gen_args.parser_module, gen_args.grammar, gen_args.lexer, gen_args.output
        )
    finally:
        os.chdir(old_cwd)
        with contextlib.suppress(ValueError):
            sys.path.remove(base)
