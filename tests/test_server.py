"""Endpoint behavior on a synthetic feature-rich log.

Exercises everything the golden fixture cannot: known/unknown model pricing,
cache hits, an error burst (hint), ``__``-internal fields, preview
truncation, batch-mode step_end usage, the SSE stream, explicit row keys,
retry, and live tailing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.conftest import age_file, client_for, row, write_log

PENDING, RUNNING, OK, CACHED, RETRYING, ERROR, SKIPPED = range(7)

NROWS = 20
ERROR_ROWS = list(range(5, 17))  # 12 errors -> burst hint fires
CACHED_ROWS = [2, 3]
OK_ROWS = [0, 1, 4, 17, 18, 19]

# Per-row fetch cost on claude-sonnet-5: (1000*3.00 + 100*15.00) / 1e6.
FETCH_ROW_USD = 0.0045
FETCH_USD = FETCH_ROW_USD * (len(OK_ROWS) + len(ERROR_ROWS))  # 0.081
# enrich is batch on claude-haiku-4-5: (100_000*1.00 + 10_000*5.00)/1e6 * 0.5.
ENRICH_USD = 0.075
SPEND = FETCH_USD + ENRICH_USD


def _feature_records() -> list[dict]:
    usage = {"in": 1000, "out": 100, "cost": None}

    def fetch_values(r: int) -> dict:
        return {
            "__web_context": f"internal context {r}",
            "domain": f"site{r}.com",
            "summary": f"summary {r}",
        }

    records = [
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": "2026-08-19-100000",
            "started_at": "2026-08-19T10:00:00+00:00",
            "num_rows": NROWS,
            "display_key": "domain",
            "steps": [
                {
                    "name": "fetch",
                    "level": 0,
                    "mode": "realtime",
                    "model": "claude-sonnet-5",
                },
                {"name": "probe", "level": 0, "mode": "realtime", "model": "mystery-9"},
                {
                    "name": "enrich",
                    "level": 1,
                    "mode": "batch",
                    "model": "claude-haiku-4-5",
                },
            ],
            "plan": None,
        },
        {
            "v": 1,
            "t": 0.5,
            "type": "step_start",
            "step": "fetch",
            "level": 0,
            "mode": "realtime",
            "num_rows": NROWS,
        },
    ]
    t = 1.0
    for r in OK_ROWS:
        records.append(row("fetch", r, t, values=fetch_values(r), usage=usage))
        t += 0.5
    for r in CACHED_ROWS:
        records.append(row("fetch", r, t, from_cache=True, values=fetch_values(r)))
        t += 0.5
    for i, r in enumerate(ERROR_ROWS):
        records.append(
            row(
                "fetch",
                r,
                10.0 + i,
                status="error",
                error={"type": "RateLimitError", "msg": "Rate limit reached (TPM)"},
                usage=usage,
            )
        )
    records.append(
        {
            "v": 1,
            "t": 25.0,
            "type": "step_end",
            "step": "fetch",
            "num_errors": len(ERROR_ROWS),
            "usage": {"in": 18000, "out": 1800, "cost": None},
            "elapsed_s": 24.5,
            "batch_id": None,
        }
    )

    records.append(
        {
            "v": 1,
            "t": 26.0,
            "type": "step_start",
            "step": "probe",
            "level": 0,
            "mode": "realtime",
            "num_rows": NROWS,
        }
    )
    for r in range(NROWS):
        records.append(
            row(
                "probe",
                r,
                27.0 + r * 0.1,
                values={"long": "x" * 400},
                usage={"in": 2000, "out": 200, "cost": None},
            )
        )
    records.append(
        {
            "v": 1,
            "t": 30.0,
            "type": "step_end",
            "step": "probe",
            "num_errors": 0,
            "usage": {"in": 40000, "out": 4000, "cost": None},
            "elapsed_s": 4.0,
            "batch_id": None,
        }
    )

    records.append(
        {
            "v": 1,
            "t": 31.0,
            "type": "step_start",
            "step": "enrich",
            "level": 1,
            "mode": "batch",
            "num_rows": NROWS,
        }
    )
    for r in range(NROWS):
        records.append(
            row(
                "enrich",
                r,
                32.0 + r * 0.1,
                values={"enriched": True},
                usage=None,
                elapsed_ms=None,
            )
        )
    records.append(
        {
            "v": 1,
            "t": 55.0,
            "type": "step_end",
            "step": "enrich",
            "num_errors": 0,
            "usage": {"in": 100000, "out": 10000, "cost": None},
            "elapsed_s": 24.0,
            "batch_id": "batch_abc123",
        }
    )
    records.append(
        {
            "v": 1,
            "t": 60.0,
            "type": "pipeline_end",
            "num_rows": NROWS,
            "total_errors": len(ERROR_ROWS),
            "cost": {"in": 158000, "out": 15800, "cost": None},
            "elapsed_s": 60.0,
        }
    )
    return records


@pytest.fixture
def feature_log(tmp_path: Path) -> Path:
    path = tmp_path / "feature.jsonl"
    write_log(path, _feature_records())
    age_file(path)
    return path


def test_cost_aggregation_and_invariants(feature_log: Path):
    with client_for(feature_log) as client:
        body = client.get("/api/run").json()
    stats, cost = body["stats"], body["cost"]

    assert stats["spend"] == pytest.approx(SPEND)
    # Unknown model "mystery-9": dollars omitted, only known models sum.
    assert cost["by_step"] == {
        "fetch": pytest.approx(FETCH_USD),
        "enrich": pytest.approx(ENRICH_USD),
    }
    assert cost["by_model"] == {
        "claude-sonnet-5": pytest.approx(FETCH_USD),
        "claude-haiku-4-5": pytest.approx(ENRICH_USD),
    }
    # Contract invariants.
    assert stats["spend"] == pytest.approx(sum(cost["by_step"].values()))
    assert stats["spend"] == pytest.approx(sum(cost["by_model"].values()))
    # Tokens count unknown-model and batch (step_end) usage too.
    assert cost["tokens"] == {
        "input": 18000 + 40000 + 100000,
        "output": 1800 + 4000 + 10000,
        "cache_read": 0,
        "cache_write": 0,
    }
    assert cost["wasted"] == pytest.approx(FETCH_ROW_USD * len(ERROR_ROWS))
    assert cost["batch_saved"] == pytest.approx(ENRICH_USD)  # 50% discount
    assert stats["cache_saved"] == pytest.approx(FETCH_ROW_USD * len(CACHED_ROWS))
    assert stats["cache_hit_rate"] == pytest.approx(2 / 60)
    assert stats["errors"] == len(ERROR_ROWS)
    assert stats["eta_s"] == 0.0
    assert body["rows"] == {"total": NROWS, "done": NROWS}


def test_steps_modes_and_fields(feature_log: Path):
    with client_for(feature_log) as client:
        steps = client.get("/api/run").json()["steps"]
    by_name = {s["name"]: s for s in steps}
    assert by_name["fetch"]["mode"] == "live"
    assert by_name["enrich"]["mode"] == "batch"
    assert by_name["fetch"]["fields"] == ["__web_context", "domain", "summary"]
    assert by_name["fetch"]["errors"] == len(ERROR_ROWS)
    assert sum(s["errors"] for s in steps) == len(ERROR_ROWS)


def test_error_group_burst_hint(feature_log: Path):
    with client_for(feature_log) as client:
        groups = client.get("/api/run").json()["error_groups"]
    assert len(groups) == 1
    g = groups[0]
    assert (g["step"], g["type"], g["count"]) == ("fetch", "RateLimitError", 12)
    assert g["rows"] == [[5, 16]]  # contiguous rows compress to one range
    assert len(g["histogram"]) == 22
    assert sum(g["histogram"]) == 12
    # 12 errors in an 11s span: burst -> suggest halving concurrency.
    assert g["hint"] is not None
    assert "halving" in g["hint"]


def test_values_previews_hide_internal_fields(feature_log: Path):
    with client_for(feature_log) as client:
        body = client.get("/api/values?start=0&count=5").json()
    rows = {r["row"]: r for r in body["rows"]}
    # Key comes from display_key ("domain") found in fetch outputs.
    assert rows[0]["key"] == "site0.com"
    # Preview = first NON-__ field; __web_context must never leak here.
    assert rows[0]["cells"]["fetch"] == {
        "v": "site0.com",
        "f": {"domain": "site0.com", "summary": "summary 0"},
        "s": OK,
    }
    assert rows[2]["cells"]["fetch"]["s"] == CACHED
    # Long values truncate to 160 chars with an ellipsis.
    probe_preview = rows[0]["cells"]["probe"]["v"]
    assert len(probe_preview) == 160
    assert probe_preview.endswith("…")
    # "f" carries every non-internal field, each truncated the same way.
    probe_f = rows[0]["cells"]["probe"]["f"]["long"]
    assert probe_f == probe_preview
    # Non-string values render as JSON.
    assert rows[0]["cells"]["enrich"] == {
        "v": "true",
        "f": {"enriched": "true"},
        "s": OK,
    }


def test_values_field_map_null_for_errored_cell(feature_log: Path):
    with client_for(feature_log) as client:
        body = client.get(f"/api/values?start={ERROR_ROWS[0]}&count=1").json()
    cell = body["rows"][0]["cells"]["fetch"]
    assert cell["s"] == ERROR
    assert cell["v"].startswith("RateLimitError")
    # Errored cells have no produced fields to choose between -> f is null,
    # so the client's field-chip lookup falls back to v (grid.js DataCell).
    assert cell["f"] is None


def test_cell_detail_full_values_include_internal(feature_log: Path):
    with client_for(feature_log) as client:
        detail = client.get("/api/cell/fetch/0").json()
    # __-prefixed keys are hidden from previews but PRESENT in full values.
    assert detail["values"]["__web_context"] == "internal context 0"
    assert detail["values"]["domain"] == "site0.com"
    assert detail["usage"] == {"in": 1000, "out": 100, "cost": pytest.approx(0.0045)}
    assert detail["attempts"][0]["kind"] == "live"
    assert detail["key"] == "site0.com"


def test_cell_detail_cached_batch_and_error(feature_log: Path):
    with client_for(feature_log) as client:
        cached = client.get("/api/cell/fetch/2").json()
        batch = client.get("/api/cell/enrich/1").json()
        errored = client.get("/api/cell/fetch/5").json()
    assert cached["status"] == "cached"
    assert cached["from_cache"] is True
    assert cached["attempts"] is None  # no attempt on a cache hit
    assert cached["usage"] is None  # cache hits carry usage null in v1

    assert batch["status"] == "ok"
    assert batch["attempts"][0]["kind"] == "batch"
    assert batch["usage"] is None  # batch mode has no per-row usage in v1

    assert errored["status"] == "error"
    assert errored["error"]["type"] == "RateLimitError"
    assert errored["usage"]["cost"] == pytest.approx(0.0045)
    assert len(errored["raw_events"]) == 1
    assert errored["raw_events"][0]["row"] == 5


async def test_events_sse_headers_and_open_comment(feature_log: Path):
    """/api/events is an infinite stream: drive the ASGI app directly and
    cancel to disconnect — TestClient cannot close a never-ending response.
    Live delta payloads are covered in tests/test_events.py."""
    from accrue_ui.server.app import create_app
    from tests.conftest import TOKEN, SSEStream, lifespan_ctx

    app = create_app(feature_log, token=TOKEN)
    async with lifespan_ctx(app):
        stream = SSEStream(app, token=TOKEN)
        try:
            await stream.wait_for(lambda s: s.chunks)
            assert stream.status == 200
            assert stream.headers["content-type"].startswith("text/event-stream")
            assert stream.chunks[0] == b": connected\n\n"
        finally:
            await stream.close()


def test_static_mounted_after_api(feature_log: Path):
    """The vendored frontend is served at /; /api/* still wins the match."""
    with client_for(feature_log) as client:
        resp = client.get("/vendor/preact.module.js")
        assert resp.status_code == 200
        assert client.get("/api/run").status_code == 200


def test_retry_stub_returns_409(feature_log: Path):
    with client_for(feature_log) as client:
        resp = client.post(
            "/api/retry", json={"all": True}, headers={"Origin": "http://localhost"}
        )
    assert resp.status_code == 409
    body = resp.json()
    assert body["available"] is False
    assert body["reason"]


def _keyed_records() -> list[dict]:
    """Two steps, display_key "domain"; rows exercise every key-source combo."""
    records = [
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": "keyed-run",
            "started_at": "2026-08-20T10:00:00+00:00",
            "num_rows": 4,
            "display_key": "domain",
            "steps": [
                {"name": "fetch", "level": 0, "mode": "realtime", "model": None},
                {"name": "enrich", "level": 1, "mode": "realtime", "model": None},
            ],
            "plan": None,
        },
    ]
    # Row 0: explicit key AND a display_key value -> explicit wins.
    r0 = row("fetch", 0, 1.0, values={"domain": "site0.com"})
    r0["key"] = "explicit.com"
    # Row 1: no key field at all (old log) -> values-derived heuristic.
    r1 = row("fetch", 1, 1.1, values={"domain": "site1.com"})
    # Row 2: key present but null -> heuristic fallback.
    r2 = row("fetch", 2, 1.2, values={"domain": "site2.com"})
    r2["key"] = None
    # Row 3: explicit key with no values to derive from.
    r3 = row("fetch", 3, 1.3, status="skipped")
    r3["key"] = "only-key.com"
    records += [r0, r1, r2, r3]
    # A later step's heuristic hit must NOT overwrite row 0's explicit key.
    records.append(row("enrich", 0, 2.0, values={"domain": "other.com"}))
    return records


def test_row_complete_key_wins_over_heuristic(tmp_path: Path):
    log = tmp_path / "keyed.jsonl"
    write_log(log, _keyed_records())
    age_file(log)
    with client_for(log) as client:
        values = client.get("/api/values?start=0&count=4").json()
        detail = client.get("/api/cell/fetch/0").json()
    keys = {r["row"]: r["key"] for r in values["rows"]}
    assert keys == {
        0: "explicit.com",  # explicit beats the display_key heuristic
        1: "site1.com",  # no key field: heuristic still works
        2: "site2.com",  # null key: tolerated, heuristic fallback
        3: "only-key.com",  # explicit key needs no values
    }
    assert detail["key"] == "explicit.com"


def test_interrupted_old_run_is_not_live(tmp_path: Path):
    """No pipeline_end, no writes for hours: dead, not LIVE forever."""
    log = tmp_path / "interrupted.jsonl"
    records = _feature_records()
    cut = next(i for i, r in enumerate(records) if r["type"] == "pipeline_end")
    write_log(log, records[:cut])  # everything but the pipeline_end
    age_file(log, seconds=3 * 24 * 3600)
    with client_for(log) as client:
        run = client.get("/api/run").json()["run"]
        runs = client.get("/api/runs").json()["runs"]
    assert run["live"] is False
    # elapsed is the log's own span (last t), not now-minus-started_at.
    assert run["elapsed_s"] == pytest.approx(55.0)
    assert runs[0]["live"] is False


def test_bogus_row_index_is_ignored(tmp_path: Path):
    """One corrupt `row` must not resize the grid to millions of cells."""
    log = tmp_path / "bogus.jsonl"
    records = _feature_records()[:2]
    records.append(row("fetch", 50_000_000, 1.0, values={"domain": "x"}))
    records.append(row("fetch", 0, 1.1, values={"domain": "ok"}))
    write_log(log, records)
    age_file(log)
    with client_for(log) as client:
        body = client.get("/api/run").json()
    assert body["cells"]["rows"] == NROWS  # the declared count, unchanged
    assert body["steps"][0]["done"] == 1  # only the real row landed


def test_row_index_cap_applies_without_a_declared_row_count(tmp_path: Path):
    """A log with no pipeline_start still cannot be talked into a huge grid."""
    log = tmp_path / "headless.jsonl"
    write_log(log, [row("fetch", 5_000_000, 1.0), row("fetch", 3, 1.1)])
    age_file(log)
    with client_for(log) as client:
        body = client.get("/api/run").json()
    assert body["cells"]["rows"] == 4  # row 3 only
    assert body["steps"][0]["done"] == 1


def test_z_form_timestamps_are_parsed(tmp_path: Path):
    """started_at may end in Z; 3.10's fromisoformat cannot read that alone."""
    log = tmp_path / "zform.jsonl"
    records = _feature_records()[:3]
    records[0] = {**records[0], "started_at": "2026-08-19T10:00:00Z"}
    write_log(log, records)
    age_file(log)
    with client_for(log) as client:
        body = client.get("/api/run").json()
    assert body["run"]["started_at"] == "2026-08-19T10:00:00Z"
    assert body["error_groups"] == []


def test_completion_timestamps_stay_bounded(tmp_path: Path):
    """The throughput window is a window, not an ever-growing list."""
    from accrue_ui.server.index import THROUGHPUT_WINDOW_S, RunIndex
    from accrue_ui.server.tail import TailRecord

    index = RunIndex(tmp_path / "unused.jsonl")
    index.apply(TailRecord(0, 1, _feature_records()[0]))
    for i in range(5000):
        index.apply(TailRecord(0, 1, row("fetch", i % NROWS, i * 0.1)))
    span = index._completion_ts[-1] - index._completion_ts[0]
    assert span <= THROUGHPUT_WINDOW_S
    assert len(index._completion_ts) <= THROUGHPUT_WINDOW_S / 0.1 + 1


def test_cell_events_are_capped(tmp_path: Path):
    """A cell retried hundreds of times keeps the recent records, not all."""
    from accrue_ui.server.index import MAX_CELL_EVENTS, RunIndex
    from accrue_ui.server.tail import TailRecord

    index = RunIndex(tmp_path / "unused.jsonl")
    index.apply(TailRecord(0, 1, _feature_records()[0]))
    for i in range(MAX_CELL_EVENTS * 3):
        index.apply(TailRecord(i, 1, row("fetch", 0, 1.0 + i)))
    cell = index.steps[0].cells[0]
    assert len(cell.events) == MAX_CELL_EVENTS
    assert cell.events[-1][0] == MAX_CELL_EVENTS * 3 - 1  # newest kept


def test_index_rebuilds_when_the_log_is_replaced_in_place(tmp_path: Path):
    """A second run written over the same path must not splice onto the first."""
    log = tmp_path / "reused.jsonl"
    first = _feature_records()[:2]
    first.append(row("fetch", 0, 1.0, values={"domain": "first.com"}))
    write_log(log, first)
    with client_for(log) as client:
        deadline = time.time() + 5
        while client.get("/api/run").json()["steps"][0]["done"] == 0:
            assert time.time() < deadline, "first run never indexed"
            time.sleep(0.05)

        second = _feature_records()[:2]
        second[0] = {**second[0], "run_id": "second-run", "num_rows": NROWS}
        second += [
            row("fetch", r, 1.0 + r, values={"domain": f"s{r}.com"}) for r in (1, 2)
        ]
        write_log(log, second)  # same inode, longer than what was consumed

        deadline = time.time() + 5
        while True:
            snap = client.get("/api/run").json()
            if snap["run"]["id"] == "second-run":
                break
            assert time.time() < deadline, "index never rebuilt"
            time.sleep(0.05)
        values = client.get("/api/values?start=0&count=3").json()
    assert snap["steps"][0]["done"] == 2  # the new run's rows only
    keys = {r["row"]: r["key"] for r in values["rows"]}
    assert keys[1] == "s1.com"
    assert keys[0] == "row 0"  # the old run's row 0 is gone, not spliced in


def test_app_follows_appended_records(tmp_path: Path):
    """The background follower keeps the shared index current."""
    log = tmp_path / "live.jsonl"
    records = _feature_records()
    write_log(log, records[:2])  # pipeline_start + fetch step_start
    with client_for(log) as client:
        snap = client.get("/api/run").json()
        assert snap["run"]["live"] is True
        assert snap["steps"][0]["done"] == 0

        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(records[2]) + "\n")  # fetch row 0 completes

        deadline = time.time() + 5
        done = 0
        while time.time() < deadline:
            snap = client.get("/api/run").json()
            done = snap["steps"][0]["done"]
            if done:
                break
            time.sleep(0.05)
        assert done == 1
