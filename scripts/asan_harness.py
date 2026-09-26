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

"""Build and run the AddressSanitizer C++ harness (`pixi run asan-test`).

Extracts the example grammars' serialized ATNs and metadata into a generated
`grammar_data.h`, then builds and runs `tests/asan/harness.cpp` with ASan over a
range of inputs. It drives the vendored runtime and cpp/events.h the way the
binding does but without Python or dyld, so it works on both macOS and Linux. CI
also runs the real suite under ASan on Linux (asan_pytest.py).
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASAN_DIR = ROOT / "tests" / "asan"
BUILD_DIR = ASAN_DIR / "build"

# (display name, sys.path dir, lexer module, parser module, start rule, inputs).
# Inputs deliberately include single-token and empty cases that force a decision
# to look ahead at EOF, the path that exposed the DFA-edge overflow.
_GRAMMARS = [
    (
        "JSON",
        ROOT / "examples" / "json",
        "generated.JSONLexer",
        "generated.JSONParser",
        0,
        [
            '{"a": 1, "b": [2, 3, {"c": null}], "d": true}',
            '[1, 2.5, -3, 4e10, true, false, null, "x"]',
            "{}",
            "[]",
            '{"café": "naïve", "emoji": "\U0001f600\U0001f680"}',
            '{"nested": {"x": [[], {}], "y": [{"z": 1}]}}',
            "42",
            '{"a": 1 2}',  # syntax error / recovery
            "[",  # truncated
        ],
    ),
    (
        "Pred",
        ROOT / "examples" / "predicate" / "generated",
        "PredLexer",
        "PredParser",
        0,
        ["a", "a a", "", "a a a"],  # "a" and "" hit the EOF-lookahead decision
    ),
]


def _cpp_ints(values) -> str:
    return "{" + ", ".join(str(int(v)) for v in values) + "}"


def _cpp_strs(values) -> str:
    def esc(s: str | None) -> str:
        if s is None or s == "<INVALID>":
            return '""'
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        s = s.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
        return '"' + s + '"'

    return "{" + ", ".join(esc(s) for s in values) + "}"


def _extract() -> str:
    out = [
        "#pragma once",
        "#include <cstddef>",
        "#include <cstdint>",
        "#include <string>",
        "#include <vector>",
        "",
        "struct GrammarData {",
        "  std::string name;",
        "  std::vector<int32_t> lexerAtn;",
        "  std::vector<int32_t> parserAtn;",
        "  std::vector<std::string> lexerLiteral;",
        "  std::vector<std::string> lexerSymbolic;",
        "  std::vector<std::string> lexerRules;",
        "  std::vector<std::string> lexerChannels;",
        "  std::vector<std::string> lexerModes;",
        "  std::vector<std::string> parserLiteral;",
        "  std::vector<std::string> parserSymbolic;",
        "  std::vector<std::string> parserRules;",
        "  size_t startRule;",
        "  std::vector<std::string> inputs;",
        "};",
        "",
        "static const std::vector<GrammarData> GRAMMARS = {",
    ]
    for name, path, lexer_mod_name, parser_mod_name, start_rule, inputs in _GRAMMARS:
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        lexer_mod = importlib.import_module(lexer_mod_name)
        parser_mod = importlib.import_module(parser_mod_name)
        lexer = getattr(lexer_mod, lexer_mod_name.rsplit(".", 1)[-1])
        parser = getattr(parser_mod, parser_mod_name.rsplit(".", 1)[-1])
        out += [
            "  {",
            f'    "{name}",',
            f"    {_cpp_ints(lexer_mod.serializedATN())},",
            f"    {_cpp_ints(parser_mod.serializedATN())},",
            f"    {_cpp_strs(lexer.literalNames)},",
            f"    {_cpp_strs(lexer.symbolicNames)},",
            f"    {_cpp_strs(lexer.ruleNames)},",
            f"    {_cpp_strs(lexer.channelNames)},",
            f"    {_cpp_strs(lexer.modeNames)},",
            f"    {_cpp_strs(parser.literalNames)},",
            f"    {_cpp_strs(parser.symbolicNames)},",
            f"    {_cpp_strs(parser.ruleNames)},",
            f"    {start_rule},",
            f"    {_cpp_strs(inputs)},",
            "  },",
        ]
    out += ["};", ""]
    return "\n".join(out)


def main() -> int:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    (BUILD_DIR / "grammar_data.h").write_text(_extract(), encoding="utf-8")

    # Compile in a single invocation rather than via CMake: a CMake-linked binary
    # mis-orders the ASan runtime initializer on macOS and deadlocks at startup,
    # whereas a one-shot build links it correctly. Uses the environment's compiler
    # ($CXX from the pixi cxx-compiler: clang on macOS, gcc on Linux).
    src = ROOT / "vendor" / "antlr4-cpp" / "src"
    sources = sorted(str(p) for p in src.rglob("*.cpp"))
    # On macOS use the system clang: its ASan runtime tracks the OS, whereas the
    # conda clang's ASan deadlocks at init on recent macOS (re-entrant malloc
    # during shadow-memory setup). On Linux the env compiler ($CXX, conda gcc) is
    # fine.
    if sys.platform == "darwin":
        cxx = "/usr/bin/clang++"
    else:
        cxx = os.environ.get("CXX") or "c++"
    exe = BUILD_DIR / "asan_harness"
    subprocess.run(
        [
            cxx,
            "-std=c++17",
            "-fsanitize=address",
            "-fno-omit-frame-pointer",
            "-g",
            "-O1",
            "-pthread",
            "-DANTLR4CPP_STATIC",
            "-I",
            str(src),
            "-I",
            str(src / "tree" / "pattern"),
            "-I",
            str(src / "tree" / "xpath"),
            "-I",
            str(ROOT / "cpp"),  # events.h
            "-I",
            str(BUILD_DIR),  # generated grammar_data.h
            str(ASAN_DIR / "harness.cpp"),
            *sources,
            "-o",
            str(exe),
        ],
        check=True,
    )

    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:abort_on_error=1")
    return subprocess.run([str(exe)], env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
