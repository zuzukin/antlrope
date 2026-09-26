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

"""Quantify the contribution of the two vendored-runtime patches.

Run once per runtime variant (rebuild `_native` against each, then run this):

  * pristine upstream runtime (no patches),
  * Patch 1 only (lock-free DFA-edge reads),
  * Patch 1 and Patch 2 (per-DFA write locks).

Two measurements, matching the two axes the patches target:

  * single-thread parse: best-of-N `parse_events` over one large in-memory JSON
    document. Isolates Patch 1 (the lexer DFA-edge read hot path).
  * shared-spec parallel scaling: the same cached spec parsed across a thread
    pool and serially. Isolates Patch 2 (per-DFA write locks): how well concurrent
    parses that share one ATN overlap.

Reports one labelled line per measurement so several runs can be compared.
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "json"))
_jlist: Any = importlib.import_module("json_listener")
JsonEventListener = _jlist.JsonEventListener

from antlrope._native import parse_events  # noqa: E402

LABEL = sys.argv[1] if len(sys.argv) > 1 else "variant"

# One large document: ~6 MB, lexer- and parser-exercising.
_N_OBJECTS = 60_000
_BIG = (
    "["
    + ",".join(
        f'{{"id": {i}, "name": "item-{i}", "vals": [1, 2.5, true, null], "s": "abc"}}'
        for i in range(_N_OBJECTS)
    )
    + "]"
)
_NBYTES = len(_BIG.encode())

_N_THREADS = 4
_N_TASKS = 2 * _N_THREADS


def _best_of(fn, trials: int = 7) -> float:
    best = float("inf")
    for _ in range(trials):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def _single_thread() -> float:
    """Best-of-7 single-thread native parse (cached spec, warmed)."""
    pspec = JsonEventListener._parser_spec()
    lspec = JsonEventListener._lexer_spec()

    def one() -> None:
        _raw, errors = parse_events(pspec, lspec, _BIG, 0, None, None)
        assert not errors

    one()  # warm the DFA cache
    return _best_of(one)


def _parallel_shared() -> tuple[float, float]:
    """Serial vs threaded wall time for N tasks through ONE shared cached spec."""
    pspec = JsonEventListener._parser_spec()
    lspec = JsonEventListener._lexer_spec()

    def one(_=None) -> int:
        raw, errors = parse_events(pspec, lspec, _BIG, 0, None, None)
        assert not errors
        return len(raw)

    one()  # warm

    def serial() -> None:
        for _ in range(_N_TASKS):
            one()

    def threaded() -> None:
        with ThreadPoolExecutor(max_workers=_N_THREADS) as pool:
            list(pool.map(one, range(_N_TASKS)))

    return _best_of(serial, 3), _best_of(threaded, 3)


def main() -> int:
    cores = os.cpu_count() or 1
    st = _single_thread()
    mbps = _NBYTES / 1e6 / st
    print(
        f"[{LABEL}] single-thread parse: {st * 1e3:7.1f} ms "
        f"({mbps:6.1f} MB/s) over {_NBYTES / 1e6:.1f} MB"
    )

    serial_s, threaded_s = _parallel_shared()
    speedup = serial_s / threaded_s
    print(
        f"[{LABEL}] shared-spec {_N_TASKS} tasks / {_N_THREADS} threads "
        f"({cores} cores): serial {serial_s * 1e3:7.1f} ms, "
        f"threaded {threaded_s * 1e3:7.1f} ms -> {speedup:.2f}x scaling"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
