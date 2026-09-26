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

"""Run the pytest suite under AddressSanitizer (`pixi run asan-pytest`, Linux).

Builds the `_native` extension with ASan, swaps it into the environment, and runs
pytest with the ASan runtime preloaded. Unlike the portable C++ harness
(asan_harness.py), this covers the nanobind binding too, exercised through the
real test suite.

Linux only: macOS's hardened conda Python strips `DYLD_INSERT_LIBRARIES`, so ASan
can't install its interceptors there. On macOS this skips with a note; use
`pixi run asan-test` instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build-asan"


def _build_asan_extension() -> Path:
    nb = subprocess.run(
        [sys.executable, "-m", "nanobind", "--cmake_dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not (BUILD / "CMakeCache.txt").exists():
        subprocess.run(
            [
                "cmake",
                "-S",
                str(ROOT),
                "-B",
                str(BUILD),
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                "-DCMAKE_CXX_FLAGS=-fsanitize=address -fno-omit-frame-pointer -g",
                "-DCMAKE_SHARED_LINKER_FLAGS=-fsanitize=address",
                f"-Dnanobind_DIR={nb}",
                f"-DPython_EXECUTABLE={sys.executable}",
            ],
            check=True,
        )
    subprocess.run(["cmake", "--build", str(BUILD), "--target", "_native"], check=True)
    return next(BUILD.glob("_native*.so"))


def _find_libasan(asan_so: Path) -> str:
    out = subprocess.run(["ldd", str(asan_so)], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "libasan" in line or "libclang_rt.asan" in line:
            parts = line.split("=>")
            if len(parts) == 2:
                return parts[1].strip().split()[0]
    raise SystemExit("could not locate the ASan runtime via ldd")


def main() -> int:
    if sys.platform == "darwin":
        print(
            "asan-pytest: skipped on macOS (hardened Python strips "
            "DYLD_INSERT_LIBRARIES, so ASan can't load early enough). "
            "Use `pixi run asan-test`."
        )
        return 0

    asan_so = _build_asan_extension()
    # The installed editable extension may be named differently from the ASan build:
    # the abi3 editable install lands `_native.abi3.so`, while this throwaway ASan
    # build (no Stable-ABI component) produces a version-tagged `_native.cpython-*.so`.
    # Swap the ASan build over whatever `_native*.so` is actually installed, by name.
    site_pkg = Path(sysconfig.get_paths()["purelib"]) / "antlrope"
    installed = next(site_pkg.glob("_native*.so"), None)
    if installed is None:
        raise SystemExit(f"no installed _native*.so found in {site_pkg}")
    backup = installed.parent / (installed.name + ".orig")
    shutil.copy2(installed, backup)
    try:
        shutil.copy2(asan_so, installed)
        env = dict(os.environ)
        env["LD_PRELOAD"] = _find_libasan(asan_so)
        # Disable leak and container-overflow checks: Python and its allocations
        # are not instrumented, so both report false positives here. We only want
        # use-after-free and out-of-bounds errors, which are the ones this run has
        # caught.
        env["ASAN_OPTIONS"] = (
            "detect_leaks=0:detect_container_overflow=0:abort_on_error=1"
        )
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *sys.argv[1:]], cwd=ROOT, env=env
        ).returncode
    finally:
        shutil.move(str(backup), str(installed))


if __name__ == "__main__":
    raise SystemExit(main())
