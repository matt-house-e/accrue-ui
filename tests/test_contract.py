"""Contract tests against the pinned golden fixture from accrue core.

The fixture (tests/fixtures/contract/run_small.jsonl, see the README there)
is a 12-row, 3-step run: row 7 errors in `score`, row 3 is skipped in `flag`.
These tests are the cross-repo handshake — if accrue changes the log shape,
they break here first.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from tests.conftest import GOLDEN_FIXTURE, client_for

NROWS = 12
STEPS = ["normalize", "score", "flag"]
ERROR_ROW = 7  # errors in step "score"
SKIP_ROW = 3  # skipped in step "flag"

PENDING, RUNNING, OK, CACHED, RETRYING, ERROR, SKIPPED = range(7)


def _fixture_records() -> list[dict]:
    with open(GOLDEN_FIXTURE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_run_meta(golden_log: Path):
    with client_for(golden_log) as client:
        body = client.get("/api/run").json()
    run = body["run"]
    assert run["id"] == "2026-08-19-205911"
    assert run["name"] == "run_small"
    assert run["started_at"] == "2026-08-19T19:59:11Z"
    assert run["schema_v"] == 1
    assert run["live"] is False  # finished, and mtime aged past the window
    assert 0 < run["elapsed_s"] < 1


def test_steps(golden_log: Path):
    with client_for(golden_log) as client:
        steps = client.get("/api/run").json()["steps"]
    assert [s["name"] for s in steps] == STEPS
    assert [s["level"] for s in steps] == [0, 1, 2]
    assert all(s["mode"] == "live" for s in steps)  # log "realtime" -> "live"
    assert all(s["model"] is None for s in steps)
    assert [s["done"] for s in steps] == [NROWS, NROWS, NROWS]
    assert [s["total"] for s in steps] == [NROWS, NROWS, NROWS]
    assert [s["errors"] for s in steps] == [0, 1, 0]
    assert [s["fields"] for s in steps] == [["name_upper"], ["score"], ["flagged"]]


def test_cells_b64_roundtrip(golden_log: Path):
    with client_for(golden_log) as client:
        body = client.get("/api/run").json()
    cells = body["cells"]
    assert cells["encoding"] == "b64"
    assert (cells["rows"], cells["steps"]) == (NROWS, len(STEPS))
    data = base64.b64decode(cells["data"])
    assert len(data) == NROWS * len(STEPS)
    assert all(0 <= b <= 6 for b in data)
    # Row-major [row * steps + stepIndex]: the error and skip land exactly.
    assert data[ERROR_ROW * 3 + 1] == ERROR
    assert data[SKIP_ROW * 3 + 2] == SKIPPED
    assert sum(1 for b in data if b == OK) == NROWS * 3 - 2

    assert body["rows"] == {"total": NROWS, "done": NROWS}


def test_stats_and_cost_with_no_pricing(golden_log: Path):
    """Function steps: model null everywhere -> all dollar figures are null."""
    with client_for(golden_log) as client:
        body = client.get("/api/run").json()
    stats = body["stats"]
    assert stats["spend"] is None
    assert stats["cache_saved"] is None
    assert stats["errors"] == 1
    assert stats["cache_hit_rate"] == 0.0
    assert stats["eta_s"] == 0.0  # finished run
    cost = body["cost"]
    assert cost["by_step"] == {}
    assert cost["by_model"] == {}
    assert cost["plan"] is None
    assert cost["tokens"] == {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    assert cost["wasted"] is None
    assert cost["batch_saved"] is None
    retry = body["retry"]
    assert retry["available"] is False
    assert retry["reason"]


def test_error_group(golden_log: Path):
    with client_for(golden_log) as client:
        groups = client.get("/api/run").json()["error_groups"]
    assert len(groups) == 1
    g = groups[0]
    assert (g["step"], g["type"], g["count"]) == ("score", "ValueError", 1)
    assert g["message"] == "cannot score company-07"
    assert g["rows"] == [[ERROR_ROW, ERROR_ROW]]
    assert g["first_t"] == g["last_t"] == "2026-08-19T19:59:11Z"
    assert len(g["histogram"]) == 22
    assert sum(g["histogram"]) == 1
    assert g["hint"] is None  # count < 10: no burst hint


def test_values_previews(golden_log: Path):
    with client_for(golden_log) as client:
        body = client.get(f"/api/values?start=0&count={NROWS}").json()
    assert body["start"] == 0
    assert len(body["rows"]) == NROWS
    row0 = body["rows"][0]
    # display_key "company" never appears in step outputs -> fallback label.
    assert row0["key"] == "row 0"
    assert row0["cells"]["normalize"] == {"v": "COMPANY-00", "s": OK}
    row7 = body["rows"][ERROR_ROW]
    assert row7["cells"]["score"]["s"] == ERROR
    assert row7["cells"]["score"]["v"].startswith("ValueError")
    row3 = body["rows"][SKIP_ROW]
    assert row3["cells"]["flag"] == {"v": None, "s": SKIPPED}


def test_values_window_clamps(golden_log: Path):
    with client_for(golden_log) as client:
        body = client.get("/api/values?start=10&count=50").json()
        beyond = client.get("/api/values?start=99&count=5").json()
    assert [r["row"] for r in body["rows"]] == [10, 11]
    assert beyond["rows"] == []


def test_cell_detail_and_raw_events(golden_log: Path):
    with client_for(golden_log) as client:
        detail = client.get(f"/api/cell/score/{ERROR_ROW}").json()
    assert detail["step"] == "score"
    assert detail["row"] == ERROR_ROW
    assert detail["status"] == "error"
    assert detail["from_cache"] is False
    assert detail["error"] == {"type": "ValueError", "msg": "cannot score company-07"}
    assert detail["usage"] is None
    assert detail["values"] is None
    assert detail["attempts"] == [
        {
            "n": 1,
            "kind": "live",
            "at": "2026-08-19T19:59:11Z",
            "latency_ms": detail["elapsed_ms"],
            "status": "error",
            "backoff_s": None,
        }
    ]
    # raw_events are the exact original records for this cell, verbatim.
    expected = [
        r
        for r in _fixture_records()
        if r.get("type") == "row_complete"
        and r.get("step") == "score"
        and r.get("row") == ERROR_ROW
    ]
    assert detail["raw_events"] == expected


def test_cell_detail_ok_row(golden_log: Path):
    with client_for(golden_log) as client:
        detail = client.get("/api/cell/normalize/0").json()
    assert detail["status"] == "ok"
    assert detail["values"] == {"name_upper": "COMPANY-00"}
    assert detail["error"] is None
    assert detail["attempts"][0]["status"] == "ok"


def test_cell_404s(golden_log: Path):
    with client_for(golden_log) as client:
        assert client.get("/api/cell/nope/0").status_code == 404
        assert client.get("/api/cell/score/99").status_code == 404


def test_runs_listing(golden_log: Path):
    with client_for(golden_log) as client:
        runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 1
    entry = runs[0]
    assert entry["id"] == "2026-08-19-205911"
    assert entry["name"] == "run_small"
    assert entry["path"].endswith("run_small.jsonl")
    assert entry["started_at"] == "2026-08-19T19:59:11Z"
    assert entry["live"] is False
