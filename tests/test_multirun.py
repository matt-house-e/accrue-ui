"""Several runs in one log file (accrue-ui#33).

accrue appends: a rerun pointed at the same output path writes a second
``pipeline_start`` … ``pipeline_end`` frame, with its own ``run_id``, into the
same file. Each run must be indexed on its own — totals, cells and the header
describe one run — and a retry segment heals the run its ``run_id`` names.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from accrue_ui.server.app import create_app
from accrue_ui.server.index import RETRY_NOT_LATEST
from tests.conftest import TOKEN, SSEStream, age_file, client_for, lifespan_ctx, row

OK, ERROR = 2, 5
STEP = "classify"
MODEL = "claude-sonnet-4-6"
USAGE = {"in": 1000, "out": 100, "cost": None}
POST = {"Origin": "http://localhost", "Content-Type": "application/json"}


def _run(
    run_id: str,
    *,
    t0: float,
    started_at: str,
    nrows: int,
    failing: set[int],
    ended: bool = True,
) -> list[dict[str, Any]]:
    """One single-step run, its ``t`` starting at *t0* (accrue continues it)."""
    records: list[dict[str, Any]] = [
        {
            "v": 1,
            "t": t0,
            "type": "pipeline_start",
            "run_id": run_id,
            "started_at": started_at,
            "num_rows": nrows,
            "display_key": None,
            "steps": [{"name": STEP, "level": 0, "mode": "realtime", "model": MODEL}],
        },
        {
            "v": 1,
            "t": t0,
            "type": "step_start",
            "step": STEP,
            "level": 0,
            "mode": "realtime",
            "num_rows": nrows,
        },
    ]
    for r in range(nrows):
        if r in failing:
            records.append(
                row(
                    STEP,
                    r,
                    t0 + 1 + r * 0.1,
                    status="error",
                    error={"type": "RateLimitError", "msg": "429"},
                    usage=USAGE,
                )
            )
        else:
            records.append(
                row(
                    STEP,
                    r,
                    t0 + 1 + r * 0.1,
                    values={"label": f"{run_id}-{r}"},
                    usage=USAGE,
                )
            )
    records.append(
        {
            "v": 1,
            "t": t0 + 2,
            "type": "step_end",
            "step": STEP,
            "num_errors": len(failing),
            "usage": {"in": 1000 * nrows, "out": 100 * nrows, "cost": None},
            "elapsed_s": 2.0,
        }
    )
    if ended:
        records.append(
            {
                "v": 1,
                "t": t0 + 2.5,
                "type": "pipeline_end",
                "num_rows": nrows,
                "total_errors": len(failing),
                "cost": None,
                "elapsed_s": 2.5,
            }
        )
    return records


def _retry(run_id: str, *, t: float, rows: list[int]) -> list[dict[str, Any]]:
    """A ``retry_failed()`` segment that heals *rows*, keeping *run_id*."""
    return [
        {
            "v": 1,
            "t": t,
            "type": "retry_start",
            "run_id": run_id,
            "started_at": "2026-10-13T12:30:00Z",
            "num_rows": 4,
            "num_cells": len(rows),
            "cells": [{"step": STEP, "row": r} for r in rows],
        },
        {
            "v": 1,
            "t": t,
            "type": "step_start",
            "step": STEP,
            "level": 0,
            "mode": "realtime",
            "num_rows": len(rows),
        },
        *(row(STEP, r, t + 0.1, values={"label": "healed"}) for r in rows),
        {"v": 1, "t": t + 0.2, "type": "step_end", "step": STEP, "num_errors": 0},
        {"v": 1, "t": t + 0.3, "type": "retry_end", "num_cells": len(rows)},
    ]


RUN_A = _run("run-a", t0=0.0, started_at="2026-10-13T10:00:00Z", nrows=4, failing={2})
RUN_B = _run("run-b", t0=10.0, started_at="2026-10-13T11:00:00Z", nrows=3, failing={1})


def _write(path: Path, *segments: list[dict[str, Any]], aged: bool = True) -> Path:
    with open(path, "w", encoding="utf-8") as f:
        for segment in segments:
            f.writelines(json.dumps(r) + "\n" for r in segment)
    if aged:
        age_file(path)
    return path


def _append(path: Path, records: list[dict[str, Any]]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in records)


def _cells(snap: dict[str, Any]) -> list[int]:
    return list(base64.b64decode(snap["cells"]["data"]))


def test_two_runs_in_one_file_show_only_the_latest_by_default(tmp_path: Path):
    log = _write(tmp_path / "run.jsonl", RUN_A, RUN_B)
    with client_for(log) as client:
        snap = client.get("/api/run").json()

    assert snap["run"]["id"] == "run-b"
    assert snap["run"]["started_at"] == "2026-10-13T11:00:00Z"
    assert snap["rows"] == {"total": 3, "done": 3}
    assert snap["steps"][0]["done"] == 3
    assert snap["stats"]["errors"] == 1
    # Run B's 3 rows of tokens, not both runs' 7.
    assert snap["cost"]["tokens"]["input"] == 3000
    assert snap["cost"]["tokens"]["output"] == 300
    assert _cells(snap) == [OK, ERROR, OK]
    assert [(g["step"], g["rows"]) for g in snap["error_groups"]] == [(STEP, [[1, 1]])]
    assert snap["file_runs"] == [
        {
            "id": "run-b",
            "started_at": "2026-10-13T11:00:00Z",
            "finished": True,
            "live": False,
            "latest": True,
        },
        {
            "id": "run-a",
            "started_at": "2026-10-13T10:00:00Z",
            "finished": True,
            "live": False,
            "latest": False,
        },
    ]


def test_switching_to_an_earlier_run(tmp_path: Path):
    log = _write(tmp_path / "run.jsonl", RUN_A, RUN_B)
    with client_for(log) as client:
        snap = client.get("/api/run?run=run-a").json()
        values = client.get("/api/values?run=run-a&start=0&count=10").json()
        cell_a = client.get("/api/cell/classify/0?run=run-a").json()
        cell_b = client.get("/api/cell/classify/0").json()

    assert snap["run"]["id"] == "run-a"
    assert snap["rows"] == {"total": 4, "done": 4}
    assert snap["cost"]["tokens"]["input"] == 4000
    assert _cells(snap) == [OK, OK, ERROR, OK]
    assert [g["rows"] for g in snap["error_groups"]] == [[[2, 2]]]
    assert [r["cells"][STEP]["v"] for r in values["rows"]] == [
        "run-a-0",
        "run-a-1",
        "RateLimitError · 429",
        "run-a-3",
    ]
    # Each cell holds its own run's attempt only — never both runs' records.
    assert cell_a["values"] == {"label": "run-a-0"}
    assert len(cell_a["raw_events"]) == 1
    assert cell_b["values"] == {"label": "run-b-0"}
    assert len(cell_b["raw_events"]) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/api/run?run=nope",
        "/api/values?run=nope",
        "/api/cell/classify/0?run=nope",
        "/api/events?run=nope",
        "/api/report?run=nope",
    ],
)
def test_a_run_the_file_does_not_hold_is_a_404(tmp_path: Path, path: str):
    log = _write(tmp_path / "run.jsonl", RUN_A, RUN_B)
    with client_for(log) as client:
        assert client.get(path).status_code == 404


def test_a_retry_heals_the_run_it_names(tmp_path: Path):
    # A retry of run A appended after run B: it must heal A, not B.
    log = _write(tmp_path / "run.jsonl", RUN_A, RUN_B, _retry("run-a", t=20, rows=[2]))
    with client_for(log) as client:
        a = client.get("/api/run?run=run-a").json()
        b = client.get("/api/run").json()
        healed = client.get("/api/cell/classify/2?run=run-a").json()

    assert _cells(a) == [OK, OK, OK, OK]
    assert a["stats"]["errors"] == 0
    assert a["error_groups"] == []
    assert healed["values"] == {"label": "healed"}
    assert len(healed["raw_events"]) == 2  # the failure and the heal
    # Run B is untouched: still one error, still its own row 1.
    assert _cells(b) == [OK, ERROR, OK]
    assert b["stats"]["errors"] == 1


def test_a_retry_of_the_latest_run_keeps_working(tmp_path: Path):
    log = _write(tmp_path / "run.jsonl", RUN_A, RUN_B, _retry("run-b", t=20, rows=[1]))
    with client_for(log) as client:
        a = client.get("/api/run?run=run-a").json()
        b = client.get("/api/run").json()

    assert _cells(b) == [OK, OK, OK]
    assert b["rows"] == {"total": 3, "done": 3}
    assert b["error_groups"] == []
    assert _cells(a) == [OK, OK, ERROR, OK]


def test_times_are_measured_from_each_runs_own_start(tmp_path: Path):
    # accrue continues `t` across appended runs, so run B's records start at
    # t=10. Its error landed 1.1s into it, and it lasted 2s — not 12.
    interrupted_b = _run(
        "run-b",
        t0=10.0,
        started_at="2026-10-13T11:00:00Z",
        nrows=3,
        failing={1},
        ended=False,
    )
    log = _write(tmp_path / "run.jsonl", RUN_A, interrupted_b)
    with client_for(log) as client:
        snap = client.get("/api/run").json()

    assert snap["error_groups"][0]["first_t"] == "2026-10-13T11:00:01Z"
    assert snap["run"]["elapsed_s"] == pytest.approx(2.0)


def test_only_the_run_being_written_is_live(tmp_path: Path):
    log = _write(tmp_path / "run.jsonl", RUN_A, RUN_B, aged=False)
    with client_for(log) as client:
        latest = client.get("/api/run").json()
        earlier = client.get("/api/run?run=run-a").json()

    assert latest["run"]["live"] is True
    assert earlier["run"]["live"] is False
    assert [r["live"] for r in latest["file_runs"]] == [True, False]


def test_a_one_run_file_lists_one_run(tmp_path: Path):
    log = _write(tmp_path / "run.jsonl", RUN_A)
    with client_for(log) as client:
        snap = client.get("/api/run").json()
    assert snap["run"]["id"] == "run-a"
    assert [r["id"] for r in snap["file_runs"]] == ["run-a"]
    assert snap["cost"]["tokens"]["input"] == 4000


FAKE_PIPELINE = textwrap.dedent("""
    CALLS = []


    class FakePipeline:
        async def retry_failed_async(self, data, **kwargs):
            CALLS.append(kwargs)


    pipeline = FakePipeline()
    rows = [{"company": "a"}]
""")


def test_retry_is_offered_on_the_latest_run_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "multirun_fake.py").write_text(FAKE_PIPELINE)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    monkeypatch.delitem(sys.modules, "multirun_fake", raising=False)

    log = _write(tmp_path / "run.jsonl", RUN_A, RUN_B)
    spec = {"pipeline": "multirun_fake:pipeline", "data": "multirun_fake:rows"}
    with client_for(log, **spec) as client:
        earlier = client.get("/api/run?run=run-a").json()["retry"]
        latest = client.get("/api/run").json()["retry"]
        refused = client.post(
            "/api/retry?run=run-a", headers=POST, content=json.dumps({"all": True})
        )
        accepted = client.post(
            "/api/retry", headers=POST, content=json.dumps({"all": True})
        )

    assert earlier["available"] is False
    assert earlier["reason"] == RETRY_NOT_LATEST
    assert latest["available"] is True
    assert refused.status_code == 409
    assert refused.json()["reason"] == RETRY_NOT_LATEST
    assert accepted.status_code == 202
    # Rows come from the latest run's error groups: run B's row 1.
    assert sys.modules["multirun_fake"].CALLS[0]["rows"] == [1]


async def test_sse_follows_the_latest_run_and_pins_an_earlier_one(tmp_path: Path):
    log = _write(tmp_path / "live.jsonl", RUN_A, aged=False)
    app = create_app(log, token=TOKEN)
    async with lifespan_ctx(app):
        latest = SSEStream(app)
        pinned = SSEStream(app, query="run=run-a")
        try:
            await latest.wait_for(lambda s: s.chunks)
            await pinned.wait_for(lambda s: s.chunks)
            await asyncio.to_thread(_append, log, RUN_B)

            # A new run: everyone's run list is stale, so everyone resets...
            await latest.wait_for(lambda s: any(d.get("reset") for d in s.deltas))
            await pinned.wait_for(lambda s: any(d.get("reset") for d in s.deltas))
            # ...the follower then streams run B's cells...
            await latest.wait_for(lambda s: any(d["cells"] for d in s.deltas))
            # ...and the stream pinned to run A sees none of them, only A
            # going cold now that B owns the file's writes.
            await pinned.wait_for(
                lambda s: any(d["stats"].get("live") is False for d in s.deltas)
            )
            await asyncio.sleep(0.3)
            assert not any(d["cells"] for d in pinned.deltas)
        finally:
            await latest.close()
            await pinned.close()
