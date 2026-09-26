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

"""Generate `systemrdl.svg`, the summary chart used in the README.

Dependency-free (hand-built SVG) so the asset is reproducible without a plotting
stack. Numbers are the end-to-end (parse + collect identifiers) results on the
large 2.6 MB input from `systemrdl.md`; re-run after updating them:

    python docs/benchmarks/make_chart.py
"""

from __future__ import annotations

from pathlib import Path

# End-to-end (parse + read identifiers), large input (2.6 MB) — see systemrdl.md.
TOOLS = ["pure-Python", "speedy-antlr", "antlrope"]
COLOR = {"pure-Python": "#64748b", "speedy-antlr": "#fbbf24", "antlrope": "#34d399"}
TIME_MS = {"pure-Python": 3963, "speedy-antlr": 1618, "antlrope": 174}
MEM_MB = {"pure-Python": 418, "speedy-antlr": 1398, "antlrope": 218}

W, H = 820, 412
BASE_Y, BAR_MAX, BAR_W = 318, 188, 64
PANELS = [
    ("Time (ms)", TIME_MS, 60, "{:,} ms", "22.8&#215; faster"),
    ("Peak memory (MB)", MEM_MB, 430, "{:,} MB", "6.4&#215; less"),
]


def bars(title: str, data: dict[str, int], px: int, fmt: str, hero_note: str) -> str:
    out = [
        f'<text x="{px}" y="98" '
        f'fill="#cbd5e1" font-size="14" font-weight="600">{title}</text>',
        f'<line x1="{px}" y1="{BASE_Y}" x2="{px + 330}" y2="{BASE_Y}" '
        f'stroke="#1e293b" stroke-width="1"/>',
    ]
    top = max(data.values())
    for i, name in enumerate(TOOLS):
        v = data[name]
        h = max(3, round(v / top * BAR_MAX))
        x = px + 20 + i * 100
        y = BASE_Y - h
        hero = name == "antlrope"
        glow = ' filter="url(#glow)"' if hero else ""
        out.append(
            f'<rect x="{x}" y="{y}" width="{BAR_W}" height="{h}" rx="5" '
            f'fill="{COLOR[name]}"{glow}/>'
        )
        out.append(
            f'<text x="{x + BAR_W / 2:.0f}" y="{y - 9}" text-anchor="middle" '
            f'fill="#f1f5f9" font-size="13" font-weight="700">{fmt.format(v)}</text>'
        )
        if hero:
            out.append(
                f'<text x="{x + BAR_W / 2:.0f}" y="{y - 27}" text-anchor="middle" '
                f'fill="#34d399" font-size="11" font-weight="700">{hero_note}</text>'
            )
    return "\n".join(out)


def legend() -> str:
    out = []
    x = 168
    for name in TOOLS:
        out.append(
            f'<rect x="{x}" y="372" width="13" height="13" rx="3" fill="{COLOR[name]}"/>'
        )
        out.append(
            f'<text x="{x + 19}" y="383" fill="#cbd5e1" font-size="12.5">{name}</text>'
        )
        x += 22 + len(name) * 7.6 + 34
    return "\n".join(out)


svg = f"""<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" \
font-family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif">
  <defs>
    <filter id="glow" x="-40%" y="-40%" width="180%" height="180%">
      <feDropShadow dx="0" dy="0" stdDeviation="4" flood-color="#34d399" flood-opacity="0.55"/>
    </filter>
  </defs>
  <rect x="0" y="0" width="{W}" height="{H}" rx="18" fill="#0b1220"/>
  <rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="17" fill="none" stroke="#1e293b"/>
  <text x="40" y="48" fill="#f1f5f9" font-size="23" font-weight="800">\
antlrope vs. the alternatives</text>
  <text x="40" y="74" fill="#94a3b8" font-size="13.5">\
Parse + read a 2.6&#8202;MB SystemRDL file &#183; lower is better &#183; \
~21&#215; faster than pure-Python, ~8&#215; faster than speedy-antlr</text>
{bars(*PANELS[0])}
{bars(*PANELS[1])}
{legend()}
</svg>
"""

out = Path(__file__).with_name("systemrdl.svg")
out.write_text(svg, encoding="utf-8")
print(f"wrote {out} ({len(svg)} bytes)")
