"""Live SSE: RunIndex delta tracking, drop-and-coalesce fan-out, /api/events.

The ≤10Hz bound is structural — the coalescer sleeps FLUSH_INTERVAL_S between
drains — so these tests pin the constant and the coalescing semantics rather
than racing wall-clock timers.
"""

from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from typing import Any

from accrue_ui.server.app import create_app
from accrue_ui.server.events import (
    FLUSH_INTERVAL_S,
    DeltaBroadcaster,
    merge_deltas,
)
from accrue_ui.server.index import RunIndex
from accrue_ui.server.tail import TailRecord
from tests.conftest import TOKEN, SSEStream, lifespan_ctx, row, write_log

PENDING, RUNNING, OK, CACHED, RETRYING, ERROR, SKIPPED = range(7)


def _start_records(num_rows: int = 5) -> list[dict]:
    return [
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": "live-run",
            "started_at": "2026-08-20T10:00:00+00:00",
            "num_rows": num_rows,
            "display_key": "domain",
            "steps": [{"name": "fetch", "level": 0, "mode": "realtime", "model": None}],
            "plan": None,
        },
        {
            "v": 1,
            "t": 0.1,
            "type": "step_start",
            "step": "fetch",
            "level": 0,
            "mode": "realtime",
            "num_rows": num_rows,
        },
    ]


def _index_for(records: list[dict]) -> RunIndex:
    index = RunIndex(Path("unused.jsonl"))
    for record in records:
        index.apply(TailRecord(0, 1, record))
    return index


def _merged(deltas: list[dict]) -> dict:
    return functools.reduce(merge_deltas, deltas)


def _cellmap(delta: dict) -> dict[tuple[int, int], int]:
    return {(r, s): state for r, s, state in delta["cells"]}


def _append_lines(log: Path, records: list[dict]) -> None:
    """Append records one write at a time (a warm-cache replay burst)."""
    with open(log, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
            f.flush()


def _covered(stream: Any, want: int) -> bool:
    return bool(stream.deltas) and len(_cellmap(_merged(stream.deltas))) >= want


# ---- RunIndex.drain_delta -------------------------------------------------


def test_first_drain_primes_stats_then_none():
    index = _index_for(_start_records())
    first = index.drain_delta()
    # The very first drain reports the initial stats baseline...
    assert first is not None
    assert first["cells"] == []
    assert first["steps"] == []
    assert set(first["stats"]) == {
        "spend",
        "cache_hit_rate",
        "errors",
        "throughput_per_min",
        "eta_s",
        "cache_saved",
    }
    # ...and with no changes since, the next drain has nothing to say.
    assert index.drain_delta() is None


def test_drain_coalesces_to_latest_cell_state():
    index = _index_for(_start_records())
    index.drain_delta()  # prime the baseline
    index.apply(TailRecord(0, 1, row("fetch", 0, 1.0)))
    index.apply(  # duplicate delivery for the same cell: latest state wins
        TailRecord(
            0,
            1,
            row("fetch", 0, 1.5, status="error", error={"type": "Boom", "msg": "x"}),
        )
    )
    delta = index.drain_delta()
    assert delta is not None
    assert delta["cells"] == [[0, 0, ERROR]]  # ONE triple, not a journal
    assert delta["steps"] == [{"name": "fetch", "done": 1, "errors": 1}]
    assert delta["stats"]["errors"] == 1
    assert index.drain_delta() is None


def test_drain_stats_carries_only_changed_keys():
    index = _index_for(_start_records())
    index.drain_delta()  # prime the baseline
    index.apply(TailRecord(0, 1, row("fetch", 0, 1.0)))
    delta = index.drain_delta()
    assert delta is not None
    stats = delta["stats"]
    assert "throughput_per_min" in stats  # 0 -> >0: changed
    assert "errors" not in stats  # still 0: unchanged
    assert "spend" not in stats  # still null: unchanged
    assert "cache_hit_rate" not in stats  # still 0.0: unchanged


def test_flush_interval_pins_10hz_bound():
    assert FLUSH_INTERVAL_S >= 0.1  # docs/api-shapes.md: at most 10 events/s


# ---- DeltaBroadcaster -----------------------------------------------------


def _delta(t: float, cells: list, stats: dict, steps: list) -> dict[str, Any]:
    return {"t": t, "cells": cells, "stats": stats, "steps": steps}


def test_broadcaster_drop_and_coalesce_for_slow_client():
    broadcaster = DeltaBroadcaster()
    client = broadcaster.register()
    d1 = _delta(
        1.0, [[0, 0, OK]], {"spend": 1.0}, [{"name": "fetch", "done": 1, "errors": 0}]
    )
    d2 = _delta(
        2.0,
        [[0, 0, ERROR], [1, 0, OK]],
        {"errors": 1},
        [{"name": "fetch", "done": 2, "errors": 1}],
    )
    broadcaster.publish(d1)
    broadcaster.publish(d2)  # client hasn't read d1: merge, don't queue

    merged = client.take()
    assert merged["t"] == 2.0
    assert _cellmap(merged) == {(0, 0): ERROR, (1, 0): OK}
    assert merged["stats"] == {"spend": 1.0, "errors": 1}
    assert merged["steps"] == [{"name": "fetch", "done": 2, "errors": 1}]
    assert client.take() is None  # consumed: the slot is empty again

    # publish() never mutates the payloads it was handed.
    assert d1 == _delta(
        1.0, [[0, 0, OK]], {"spend": 1.0}, [{"name": "fetch", "done": 1, "errors": 0}]
    )
    assert d2["cells"] == [[0, 0, ERROR], [1, 0, OK]]


def test_broadcaster_register_unregister():
    broadcaster = DeltaBroadcaster()
    a, b = broadcaster.register(), broadcaster.register()
    assert broadcaster.client_count == 2
    broadcaster.publish(_delta(1.0, [[0, 0, OK]], {}, []))
    assert a.take() is not None
    assert b.take() is not None
    broadcaster.unregister(a)
    assert broadcaster.client_count == 1
    broadcaster.publish(_delta(2.0, [[1, 0, OK]], {}, []))
    assert a.take() is None  # unregistered clients get nothing
    assert b.take() is not None


# ---- /api/events integration ---------------------------------------------


async def test_events_streams_live_deltas(tmp_path: Path):
    log = tmp_path / "live.jsonl"
    write_log(log, _start_records())
    app = create_app(log, token=TOKEN)
    async with lifespan_ctx(app):
        stream = SSEStream(app)
        try:
            await stream.wait_for(lambda s: s.chunks)
            assert stream.status == 200
            assert stream.headers["content-type"].startswith("text/event-stream")
            assert stream.comments[0] == "connected"

            appended = [
                row("fetch", 0, 1.0, values={"domain": "site0.com"}),
                row(
                    "fetch",
                    1,
                    1.5,
                    status="error",
                    error={"type": "BoomError", "msg": "nope"},
                ),
            ]
            await asyncio.to_thread(_append_lines, log, appended)

            await stream.wait_for(lambda s: _covered(s, 2))
            merged = _merged(stream.deltas)
            cells = _cellmap(merged)
            assert cells[(0, 0)] == OK
            assert cells[(1, 0)] == ERROR
            steps = {s["name"]: s for s in merged["steps"]}
            assert steps["fetch"] == {"name": "fetch", "done": 2, "errors": 1}
            assert merged["stats"]["errors"] == 1
        finally:
            await stream.close()


async def test_rapid_appends_coalesce_into_few_deltas(tmp_path: Path):
    nrows = 100
    log = tmp_path / "burst.jsonl"
    write_log(log, _start_records(num_rows=nrows))
    app = create_app(log, token=TOKEN)
    async with lifespan_ctx(app):
        stream = SSEStream(app)
        try:
            await stream.wait_for(lambda s: s.chunks)
            burst = [row("fetch", r, 1.0 + r * 0.001) for r in range(nrows)]
            await asyncio.to_thread(_append_lines, log, burst)

            await stream.wait_for(lambda s: _covered(s, nrows), timeout=10)
            # 100 appends collapse into a handful of flushes, not 100 events.
            assert len(stream.deltas) <= 10
            merged = _merged(stream.deltas)
            assert all(state == OK for state in _cellmap(merged).values())
            steps = {s["name"]: s for s in merged["steps"]}
            assert steps["fetch"]["done"] == nrows
        finally:
            await stream.close()
