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

import pytest

from accrue_ui.server.app import create_app
from accrue_ui.server.events import (
    FLUSH_INTERVAL_S,
    DeltaBroadcaster,
    merge_deltas,
)
from accrue_ui.server.index import RunIndex
from accrue_ui.server.tail import TailRecord
from tests.conftest import TOKEN, SSEStream, age_file, lifespan_ctx, row, write_log

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
        "rows_done",  # delta-only mirror of the snapshot's rows.done
        "live",  # delta-only mirror of run.live (mtime recency)
    }
    # A first drain with no reset pending carries no reset key (absent=false).
    assert "reset" not in first
    # ...and with no changes since, the next drain has nothing to say.
    assert index.drain_delta() is None


def test_drain_reports_rows_done_as_rows_complete():
    """rows_done tracks rows finished through the LAST step, not cells."""
    index = _index_for(_start_records(num_rows=3))
    index.apply(
        TailRecord(
            0,
            1,
            {
                "v": 1,
                "t": 0.2,
                "type": "step_start",
                "step": "score",
                "level": 1,
                "mode": "realtime",
                "num_rows": 3,
            },
        )
    )
    index.drain_delta()  # prime the baseline

    index.apply(TailRecord(0, 1, row("fetch", 0, 1.0)))
    assert index.drain_delta()["stats"].get("rows_done") is None  # 1 of 2 steps

    index.apply(TailRecord(0, 1, row("score", 0, 1.1)))
    delta = index.drain_delta()
    assert delta["stats"]["rows_done"] == 1
    assert index.snapshot()["rows"] == {"total": 3, "done": 1}

    index.apply(TailRecord(0, 1, row("fetch", 1, 1.2)))
    index.apply(TailRecord(0, 1, row("score", 1, 1.3)))
    delta = index.drain_delta()
    assert delta["stats"]["rows_done"] == 2
    assert index.snapshot()["rows"]["done"] == 2


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
    # Past the throughput floor, so a rate exists to report at all.
    index.apply(TailRecord(0, 1, row("fetch", 0, 10.0)))
    delta = index.drain_delta()
    assert delta is not None
    stats = delta["stats"]
    assert "throughput_per_min" in stats  # null -> a number: changed
    assert "errors" not in stats  # still 0: unchanged
    assert "spend" not in stats  # still null: unchanged
    assert "cache_hit_rate" not in stats  # still 0.0: unchanged


def _index_over(path: Path, records: list[dict]) -> RunIndex:
    """Build an index over a REAL file so is_live()'s mtime check has one."""
    write_log(path, records)
    index = RunIndex(path)
    for record in records:
        index.apply(TailRecord(0, 1, record))
    return index


# ---- reset flag (issue #11): truncate-and-regrow reindex ------------------


def test_mark_reset_makes_the_next_delta_carry_reset_then_clears():
    index = _index_for(_start_records())
    index.drain_delta()  # prime the baseline; no reset pending yet
    index.apply(TailRecord(0, 1, row("fetch", 0, 1.0)))

    index.mark_reset()
    delta = index.drain_delta()
    assert delta is not None
    assert delta["reset"] is True
    # It is a one-shot: the following delta does not repeat it.
    index.apply(TailRecord(0, 1, row("fetch", 1, 1.1)))
    assert "reset" not in index.drain_delta()


def test_reset_emits_even_with_no_other_change():
    """A truncate to a bare pipeline_start must still tell clients to refetch."""
    index = _index_for(_start_records())
    index.drain_delta()  # prime; nothing dirty now
    index.mark_reset()
    delta = index.drain_delta()  # no cells/steps/stats changed
    assert delta is not None
    assert delta["reset"] is True


def test_merge_deltas_preserves_a_reset_flag():
    """A slow client's coalesced payload must not lose the reset signal."""
    base = _delta(1.0, [[0, 0, OK]], {}, [])
    base["reset"] = True
    new = _delta(2.0, [[1, 0, OK]], {}, [])  # no reset
    merged = merge_deltas(base, new)
    assert merged["reset"] is True
    # Symmetric, and absent on both sides means absent in the result.
    assert merge_deltas(new, base)["reset"] is True
    assert "reset" not in merge_deltas(new, _delta(3.0, [], {}, []))


async def test_events_sends_reset_after_truncate_and_regrow(tmp_path: Path):
    """A new run written over the same path bumps the tail generation; the
    server rebuilds its index and the next delta carries reset:true."""
    log = tmp_path / "live.jsonl"
    write_log(log, _start_records(num_rows=5))
    app = create_app(log, token=TOKEN)
    async with lifespan_ctx(app):
        stream = SSEStream(app)
        try:
            await stream.wait_for(lambda s: s.chunks)  # connected
            # Regrow in place with a DIFFERENT run (new run_id => new head
            # signature), which is what the tail keys truncate-detection off.
            second = _start_records(num_rows=8)
            second[0] = {**second[0], "run_id": "second-run"}
            second.append(row("fetch", 0, 1.0))
            await asyncio.to_thread(write_log, log, second)
            await stream.wait_for(lambda s: any(d.get("reset") for d in s.deltas))
            assert any(d.get("reset") is True for d in stream.deltas)
        finally:
            await stream.close()


# ---- live in delta stats (issue #12): LIVE badge persistence --------------


def test_live_is_a_delta_stat_that_flips_false_when_the_log_goes_cold(tmp_path: Path):
    log = tmp_path / "finishing.jsonl"
    index = _index_over(log, _start_records())
    first = index.drain_delta()  # primes stats, including live
    assert first["stats"]["live"] is True  # file just written -> live
    assert index.drain_delta() is None  # nothing changed since

    age_file(log)  # backdate mtime past the recency window: the run went cold
    delta = index.drain_delta()
    assert delta is not None
    assert delta["stats"]["live"] is False  # the badge-clearing signal
    # And it does not keep re-announcing the same value.
    assert index.drain_delta() is None


def test_live_absent_from_the_snapshot_stats_block(tmp_path: Path):
    """live rides the delta only; /api/run keeps it in run, not stats."""
    log = tmp_path / "snap.jsonl"
    index = _index_over(log, _start_records())
    snap = index.snapshot()
    assert "live" not in snap["stats"]
    assert "live" in snap["run"]


def test_throughput_is_null_until_the_log_has_time_in_it():
    """A rate over the first milliseconds of a run is noise, not throughput."""
    index = _index_for(_start_records())
    index.apply(TailRecord(0, 1, row("fetch", 0, 0.01)))
    index.apply(TailRecord(0, 1, row("fetch", 1, 0.02)))
    stats = index.snapshot()["stats"]
    assert stats["throughput_per_min"] is None  # not 6000/min
    assert stats["eta_s"] is None  # and no ETA derived from it

    index.apply(TailRecord(0, 1, row("fetch", 2, 30.0)))
    stats = index.snapshot()["stats"]
    # 3 completions over 30s of log time -> 6/min, and an ETA from that.
    assert stats["throughput_per_min"] == pytest.approx(6.0)
    assert stats["eta_s"] == pytest.approx((5 - 3) / 6.0 * 60.0)


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
