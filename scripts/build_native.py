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

"""Rebuild the `_native` extension and install it into the active environment.

With `editable.rebuild = false`, the extension is not rebuilt on import; run this
(`pixi run build`) after editing `cpp/` or the vendored runtime. It does exactly
what scikit-build-core's rebuild hook would: an incremental `cmake --build`
followed by `cmake --install` into the environment's site-packages.

This deliberately drives cmake directly rather than reinstalling via uv or pip,
because a uv editable reinstall serves a cached archive and does not reliably pick
up C++ changes, whereas `cmake --build` tracks source mtimes and rebuilds only
what changed.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import sysconfig


def main() -> int:
    build_dirs = sorted(glob.glob("build/*/"))
    if not build_dirs:
        sys.exit("No build/ directory found; run `pixi install` first.")

    purelib = sysconfig.get_paths()["purelib"]

    # With more than one build dir present (e.g. a stale pre-abi3 leftover sitting
    # beside the current one), pick the dir that builds the extension actually
    # installed in this environment. The editable install is built abi3
    # (`wheel.py-api = "cp312"` in pyproject), so it lands `_native.abi3.so` whatever
    # the running interpreter's version tag. Matching on the interpreter's
    # `EXT_SUFFIX` would pick the wrong (or a stale `cp3XX`) dir, rebuild it, and
    # leave the loaded `_native.abi3.so` untouched. Resolve by the installed file's
    # name instead, the same way scripts/asan_pytest.py does.
    if len(build_dirs) > 1:
        installed = next(
            glob.iglob(os.path.join(purelib, "antlrope", "_native*.so")), None
        )
        name = os.path.basename(installed) if installed else None
        matched = [
            d for d in build_dirs if name and os.path.exists(os.path.join(d, name))
        ]
        if len(matched) != 1:
            sys.exit(
                f"Could not match a unique build dir to the installed extension "
                f"{name!r} among {build_dirs}. Remove stale build/* dirs and re-run "
                f"`pixi install`."
            )
        build_dirs = matched

    build_dir = build_dirs[0]
    subprocess.run(["cmake", "--build", build_dir], check=True)
    subprocess.run(["cmake", "--install", build_dir, "--prefix", purelib], check=True)
    print(f"rebuilt _native -> {purelib}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
