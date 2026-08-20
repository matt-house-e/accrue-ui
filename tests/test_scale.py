"""Synthetic scale: a 20k-row x 5-step log (~100k records) indexes fast.

Bounds are generous for CI; typical local numbers are well under them.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from accrue_ui.server.index import RunIndex
from accrue_ui.server.routes import VALUES_MAX_COUNT
from accrue_ui.server.tail import Tail
from tests.conftest import age_file, client_for

NROWS = 20_000
NSTEPS = 5

BUILD_BUDGET_S = 5.0
SNAPSHOT_BUDGET_S = 0.5


def _build_scale_log(path: Path) -> None:
    steps = [
        {
            "name": f"step{i}",
            "level": i,
            "mode": "realtime",
            "model": "claude-haiku-4-5",
        }
        for i in range(NSTEPS)
    ]
    usage = {"in": 100, "out": 10, "cost": None}
    with open(path, "w", encoding="utf-8") as f:

        def emit(obj: dict) -> None:
            f.write(json.dumps(obj) + "\n")

        emit(
            {
                "v": 1,
                "t": 0.0,
                "type": "pipeline_start",
                "run_id": "2026-08-19-120000",
                "started_at": "2026-08-19T12:00:00+00:00",
                "num_rows": NROWS,
                "display_key": None,
                "steps": steps,
                "plan": None,
            }
        )
        t = 0.0
        for i, step in enumerate(steps):
            emit(
                {
                    "v": 1,
                    "t": t,
                    "type": "step_start",
                    "step": step["name"],
                    "level": i,
                    "mode": "realtime",
                    "num_rows": NROWS,
                }
            )
            for r in range(NROWS):
                t += 0.001
                emit(
                    {
                        "v": 1,
                        "t": t,
                        "type": "row_complete",
                        "step": step["name"],
                        "row": r,
                        "status": "ok",
                        "from_cache": False,
                        "values": {"f": f"v{r}"},
                        "error": None,
                        "usage": usage,
                        "elapsed_ms": 1.0,
                    }
                )
            emit(
                {
                    "v": 1,
                    "t": t,
                    "type": "step_end",
                    "step": step["name"],
                    "num_errors": 0,
                    "usage": {"in": NROWS * 100, "out": NROWS * 10, "cost": None},
                    "elapsed_s": 20.0,
                    "batch_id": None,
                }
            )
        emit(
            {
                "v": 1,
                "t": t,
                "type": "pipeline_end",
                "num_rows": NROWS,
                "total_errors": 0,
                "cost": {
                    "in": NROWS * 100 * NSTEPS,
                    "out": NROWS * 10 * NSTEPS,
                    "cost": None,
                },
                "elapsed_s": t,
            }
        )


@pytest.fixture(scope="module")
def scale_log(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("scale") / "big.jsonl"
    _build_scale_log(path)
    age_file(path)
    return path


def test_full_index_build_under_budget(scale_log: Path):
    tail = Tail(scale_log)
    index = RunIndex(scale_log)
    t0 = time.perf_counter()
    for record in tail.read_available():
        index.apply(record)
    build_s = time.perf_counter() - t0
    tail.close()

    assert index.nrows == NROWS
    assert len(index.steps) == NSTEPS
    assert sum(s.done for s in index.steps) == NROWS * NSTEPS
    print(f"\nindex build: {NROWS}x{NSTEPS} in {build_s:.2f}s")
    assert build_s < BUILD_BUDGET_S


def test_api_run_snapshot_latency(scale_log: Path):
    with client_for(scale_log) as client:
        # Warm-up call, then the measured one.
        assert client.get("/api/run").status_code == 200
        t0 = time.perf_counter()
        resp = client.get("/api/run")
        snapshot_s = time.perf_counter() - t0

        assert resp.status_code == 200
        body = resp.json()
        assert body["rows"] == {"total": NROWS, "done": NROWS}
        data = base64.b64decode(body["cells"]["data"])
        assert len(data) == NROWS * NSTEPS
        assert set(data) == {2}  # every cell ok
        print(f"/api/run snapshot: {snapshot_s * 1000:.0f}ms")
        assert snapshot_s < SNAPSHOT_BUDGET_S


def test_values_windows_page_at_the_server_cap(scale_log: Path):
    """A window wider than the cap comes back in pages, not silently short.

    The grid asks for values by window; when a sparse filter makes that
    window span more indices than VALUES_MAX_COUNT the server clamps, so
    the client has to page rather than re-ask for the same over-wide window
    forever (the ~6Hz refetch loop).
    """
    with client_for(scale_log) as client:
        first = client.get("/api/values?start=0&count=5000").json()
        second = client.get(f"/api/values?start={VALUES_MAX_COUNT}&count=5000").json()
    assert len(first["rows"]) == VALUES_MAX_COUNT
    assert [r["row"] for r in first["rows"][:2]] == [0, 1]
    assert second["start"] == VALUES_MAX_COUNT
    assert [r["row"] for r in second["rows"][:2]] == [
        VALUES_MAX_COUNT,
        VALUES_MAX_COUNT + 1,
    ]
    # Two pages, no gap and no overlap.
    assert first["rows"][-1]["row"] + 1 == second["rows"][0]["row"]


def test_values_window_mid_range(scale_log: Path):
    with client_for(scale_log) as client:
        body = client.get("/api/values?start=10000&count=3").json()
    assert body["start"] == 10000
    assert [r["row"] for r in body["rows"]] == [10000, 10001, 10002]
    row0 = body["rows"][0]
    assert row0["key"] == "row 10000"  # display_key null -> fallback label
    assert row0["cells"]["step0"] == {"v": "v10000", "s": 2}
    assert row0["cells"]["step4"] == {"v": "v10000", "s": 2}
