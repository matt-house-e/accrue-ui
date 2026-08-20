#!/usr/bin/env python3
"""Generate the 5000x5 cell-state array embedded in run.json.

Deterministic (fixed seed) so re-running reproduces the committed fixture
byte-for-byte. The array is row-major: byte at ``row * 5 + step_index``.
States: 0 pending, 1 running, 2 ok, 3 cached, 4 retrying, 5 error, 6 skipped.

Shape of the run it depicts (must stay in sync with run.json's counters):

- classify   (step 0): all 5000 rows done, one APITimeoutError at row 147.
- web_search (step 1): frontier ~4426, 38 RateLimitErrors in a burst around
  rows 1211-1284, row 1283 mid-retry.
- summarize  (step 2): batch mode, frontier ~3908, 4 ValidationErrors.
- score      (step 3): frontier ~3511.
- email      (step 4): frontier ~3412, conditional step so some rows skipped.
- Rows 1281-1292 are the design-mock window and are set explicitly (WINDOW).

Usage: python tests/fixtures/api/generate_stub_cells.py [path/to/run.json]
"""

from __future__ import annotations

import base64
import json
import random
import sys
from pathlib import Path

ROWS = 5000
STEPS = 5  # classify, web_search, summarize, score, email
CLASSIFY, WEB, SUMM, SCORE, EMAIL = range(STEPS)
PENDING, RUNNING, OK, CACHED, RETRYING, ERROR, SKIPPED = range(7)

# Error rows (mirror run.json error_groups).
CLASSIFY_ERROR_ROWS = {147}
WEB_ERROR_RANGES = [
    (1211, 1214),
    (1221, 1229),
    (1233, 1241),
    (1250, 1258),
    (1262, 1265),
    (1270, 1271),
    (1284, 1284),
]
SUMM_ERROR_ROWS = {2113, 2540, 2871, 3066}

# Progress frontier per step: rows below are settled, a short band at the
# frontier is running, rows above are pending.
FRONTIER = {WEB: 4426, SUMM: 3908, SCORE: 3511, EMAIL: 3412}
RUNNING_BAND = {WEB: 16, SUMM: 32, SCORE: 8, EMAIL: 6}  # summarize is batch

# Fraction of settled cells served from cache, per step.
CACHED_FRACTION = {CLASSIFY: 0.85, WEB: 0.55, SUMM: 0.70, SCORE: 0.75, EMAIL: 0.60}

# Fraction of settled email cells skipped (conditional step).
EMAIL_SKIP_FRACTION = 0.06

# The design-mock window, rows 1281-1292, set verbatim after generation.
# Order per row: [classify, web_search, summarize, score, email].
WINDOW = {
    1281: [2, 3, 2, 2, 2],  # stripe.com
    1282: [2, 2, 2, 2, 3],  # figma.com
    1283: [2, 4, 0, 0, 0],  # linear.app     — web_search retrying
    1284: [2, 5, 0, 0, 0],  # vercel.com     — web_search error
    1285: [3, 2, 2, 2, 6],  # notion.so      — email skipped
    1286: [2, 2, 3, 2, 2],  # anthropic.com
    1287: [2, 2, 2, 2, 1],  # render.com     — email running
    1288: [2, 3, 2, 2, 2],  # fly.io
    1289: [2, 2, 2, 1, 0],  # supabase.com   — score running
    1290: [3, 2, 2, 2, 2],  # plaid.com
    1291: [2, 2, 1, 0, 0],  # ramp.com       — summarize running
    1292: [2, 2, 2, 2, 0],  # retool.com     — email pending
}


def _web_error_rows() -> set[int]:
    rows: set[int] = set()
    for lo, hi in WEB_ERROR_RANGES:
        rows.update(range(lo, hi + 1))
    return rows


def build_cells() -> bytearray:
    rng = random.Random(7607)
    cells = bytearray(ROWS * STEPS)
    web_errors = _web_error_rows()

    def settled(step: int) -> int:
        return CACHED if rng.random() < CACHED_FRACTION[step] else OK

    for row in range(ROWS):
        # classify: everything settled, one timeout.
        if row in CLASSIFY_ERROR_ROWS:
            cells[row * STEPS + CLASSIFY] = ERROR
        else:
            cells[row * STEPS + CLASSIFY] = settled(CLASSIFY)

        # A failed dependency leaves everything downstream pending.
        blocked = row in CLASSIFY_ERROR_ROWS

        for step in (WEB, SUMM, SCORE, EMAIL):
            i = row * STEPS + step
            if blocked:
                cells[i] = PENDING
                continue
            frontier = FRONTIER[step]
            if row < frontier:
                if (step == WEB and row in web_errors) or (
                    step == SUMM and row in SUMM_ERROR_ROWS
                ):
                    cells[i] = ERROR
                    blocked = True
                elif step == EMAIL and rng.random() < EMAIL_SKIP_FRACTION:
                    cells[i] = SKIPPED
                else:
                    cells[i] = settled(step)
            elif row < frontier + RUNNING_BAND[step]:
                cells[i] = RUNNING
                blocked = True  # downstream of an in-flight cell is pending
            else:
                cells[i] = PENDING
                blocked = True

    # Stamp the design window verbatim.
    for row, states in WINDOW.items():
        for step, state in enumerate(states):
            cells[row * STEPS + step] = state

    return cells


def main(argv: list[str]) -> int:
    run_json = Path(argv[1]) if len(argv) > 1 else Path(__file__).parent / "run.json"
    doc = json.loads(run_json.read_text())
    cells = build_cells()
    doc["cells"] = {
        "encoding": "b64",
        "rows": ROWS,
        "steps": STEPS,
        "data": base64.b64encode(bytes(cells)).decode("ascii"),
    }
    run_json.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {ROWS}x{STEPS} cells ({len(cells)} bytes) into {run_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
