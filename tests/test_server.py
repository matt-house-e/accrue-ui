"""Endpoint behavior on a synthetic feature-rich log.

Exercises everything the golden fixture cannot: known/unknown model pricing,
cache hits, an error burst (hint), ``__``-internal fields, preview
truncation, batch-mode step_end usage, the SSE stub, retry, and live tailing.
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
    assert rows[0]["cells"]["fetch"] == {"v": "site0.com", "s": OK}
    assert rows[2]["cells"]["fetch"]["s"] == CACHED
    # Long values truncate to 160 chars with an ellipsis.
    probe_preview = rows[0]["cells"]["probe"]["v"]
    assert len(probe_preview) == 160
    assert probe_preview.endswith("…")
    # Non-string values render as JSON.
    assert rows[0]["cells"]["enrich"] == {"v": "true", "s": OK}


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


async def test_events_sse_stub(feature_log: Path):
    """The stub is an infinite stream, so drive the ASGI app directly and
    cancel after the first chunk — that is exactly what a client disconnect
    looks like; TestClient cannot close a never-ending response."""
    import asyncio
    import contextlib

    from accrue_ui.server.app import create_app
    from tests.conftest import TOKEN

    app = create_app(feature_log, token=TOKEN)
    started: dict = {}
    chunks: list[bytes] = []
    got_first = asyncio.Event()

    async def receive() -> dict:
        await asyncio.sleep(3600)  # hold the connection open
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            started.update(message)
        elif message["type"] == "http.response.body" and message.get("body"):
            chunks.append(message["body"])
            got_first.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/events",
        "raw_path": b"/api/events",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"host", b"localhost"), (b"x-accrue-token", TOKEN.encode())],
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 1234),
    }
    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(got_first.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert started["status"] == 200
    headers = {k.decode(): v.decode() for k, v in started["headers"]}
    assert headers["content-type"].startswith("text/event-stream")
    assert chunks[0] == b": keepalive\n\n"


def test_static_mounted_after_api(feature_log: Path):
    """The vendored frontend is served at /; /api/* still wins the match."""
    with client_for(feature_log) as client:
        resp = client.get("/vendor/preact.module.js")
        assert resp.status_code == 200
        assert client.get("/api/run").status_code == 200


def test_retry_stub_returns_409(feature_log: Path):
    with client_for(feature_log) as client:
        resp = client.post("/api/retry", headers={"Origin": "http://localhost"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["available"] is False
    assert body["reason"]


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
