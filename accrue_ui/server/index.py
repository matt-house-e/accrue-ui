"""In-memory run index built from the run log.

Consumes :class:`~accrue_ui.server.tail.TailRecord` objects and derives
everything the API serves: run meta, per-step counters, the row-major
cell-state byte array, per-row values and previews, error groups, and cost
aggregation. Raw log lines are **not** kept in memory — each cell stores the
byte offsets of its records, and ``/api/cell`` re-reads them from the file on
demand.

Cell state fits one byte: 0 pending, 1 running, 2 ok, 3 cached, 4 retrying,
5 error, 6 skipped. The v1 log has no per-row start events, so "running" (1)
cannot be inferred cheaply from step_start..row_complete bracketing — a step
in progress with rows not yet completed leaves those cells pending (0), and
state 1 is reserved for a future emitter. "retrying" (4) is likewise
reserved: v1 emits exactly one terminal row_complete per cell.
"""

from __future__ import annotations

import base64
import bisect
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .pricing import price_usd
from .tail import TailRecord

PENDING, RUNNING, OK, CACHED, RETRYING, ERROR, SKIPPED = range(7)
STATE_NAMES = ("pending", "running", "ok", "cached", "retrying", "error", "skipped")
TERMINAL = frozenset({OK, CACHED, ERROR, SKIPPED})

PREVIEW_MAX_CHARS = 160
HISTOGRAM_BUCKETS = 22
BURST_MIN_COUNT = 10
BURST_MAX_SPAN_S = 240.0
LIVE_MTIME_WINDOW_S = 5.0
THROUGHPUT_WINDOW_S = 60.0
SCHEMA_V = 1


@dataclass(slots=True)
class Cell:
    """Everything remembered about one (step, row) cell."""

    from_cache: bool = False
    error: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None  # priced dollars (batch discount applied)
    elapsed_ms: float | None = None
    t: float | None = None  # log-time of the terminal record
    values: dict[str, Any] | None = None
    preview: str | None = None
    events: list[tuple[int, int]] = field(default_factory=list)  # (offset, len)


@dataclass
class StepState:
    name: str
    level: int = 0
    mode: str = "live"  # "live" | "batch" (log "realtime" maps to "live")
    model: str | None = None
    total: int = 0
    done: int = 0
    errors: int = 0
    fields: list[str] = field(default_factory=list)
    cells: dict[int, Cell] = field(default_factory=dict)
    # --- cost tallies -----------------------------------------------------
    row_in: int = 0
    row_out: int = 0
    row_usage_seen: bool = False
    row_cost: float | None = None  # priced dollars summed over rows
    wasted: float = 0.0  # priced dollars on rows that ended in error
    batch_saved: float = 0.0
    cached_count: int = 0
    priced_ok_cost: float = 0.0  # priced, non-cached, ok rows (cache_saved est.)
    priced_ok_count: int = 0
    end_in: int = 0
    end_out: int = 0
    end_cost: float | None = None  # priced dollars from step_end aggregate
    ended: bool = False

    @property
    def is_batch(self) -> bool:
        return self.mode == "batch"

    def effective_tokens(self) -> tuple[int, int]:
        """Row-level sums when any row carried usage, else the step_end aggregate.

        Batch mode leaves per-row usage null and reports tokens only on
        step_end; live mode reports per row. Never both, so no double count.
        """
        if self.row_usage_seen:
            return self.row_in, self.row_out
        return self.end_in, self.end_out

    def effective_cost(self) -> float | None:
        if self.row_usage_seen:
            return self.row_cost
        return self.end_cost


@dataclass
class ErrorGroup:
    step: str
    type: str
    message: str
    count: int = 0
    rows: set[int] = field(default_factory=set)
    first_t: float = 0.0
    last_t: float = 0.0
    ts: list[float] = field(default_factory=list)


class RunIndex:
    """Snapshot state for one run log; feed it records via :meth:`apply`."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.run_id: str | None = None
        self.started_at: datetime | None = None
        self.display_key: str | None = None
        self.schema_v: int = SCHEMA_V
        self.plan: dict[str, Any] | None = None
        self.finished = False
        self.final_elapsed_s: float | None = None
        self.last_t = 0.0

        self._steps: list[StepState] = []
        self._step_idx: dict[str, int] = {}
        self._nrows = 0
        self._cells = bytearray()
        self._row_keys: dict[int, str] = {}
        self._error_groups: dict[tuple[str, str], ErrorGroup] = {}
        self._completion_ts: list[float] = []  # non-decreasing (log guarantee)
        self._old_nsteps = 0  # column count the cells array was laid out with

    # ------------------------------------------------------------------ dims

    @property
    def nrows(self) -> int:
        return self._nrows

    @property
    def steps(self) -> list[StepState]:
        return self._steps

    def _resize(self, nrows: int, nsteps: int) -> None:
        old = self._cells
        old_nsteps = self._old_nsteps
        new = bytearray(nrows * nsteps)
        keep = min(old_nsteps, nsteps)
        for r in range(min(self._nrows, nrows)):
            src = r * old_nsteps
            dst = r * nsteps
            new[dst : dst + keep] = old[src : src + keep]
        self._cells = new
        self._nrows = nrows
        self._old_nsteps = nsteps

    def _ensure_dims(self, nrows: int | None = None, row: int | None = None) -> None:
        want_rows = self._nrows
        if nrows is not None:
            want_rows = max(want_rows, nrows)
        if row is not None:
            want_rows = max(want_rows, row + 1)
        want_steps = len(self._steps)
        if want_rows != self._nrows or want_steps != self._old_nsteps:
            self._resize(want_rows, want_steps)

    def _step(self, name: str, **defaults: Any) -> StepState:
        idx = self._step_idx.get(name)
        if idx is not None:
            return self._steps[idx]
        step = StepState(name=name, **defaults)
        self._step_idx[name] = len(self._steps)
        self._steps.append(step)
        self._ensure_dims()
        return step

    @staticmethod
    def _map_mode(mode: Any) -> str:
        return "batch" if mode == "batch" else "live"

    # ----------------------------------------------------------------- apply

    def apply(self, record: TailRecord) -> None:
        data = record.data
        t = data.get("t")
        if isinstance(t, (int, float)):
            self.last_t = max(self.last_t, float(t))
        rtype = data.get("type")
        if rtype == "pipeline_start":
            self._apply_pipeline_start(data)
        elif rtype == "step_start":
            self._apply_step_start(data)
        elif rtype == "row_complete":
            self._apply_row_complete(record)
        elif rtype == "step_end":
            self._apply_step_end(data)
        elif rtype == "pipeline_end":
            self._apply_pipeline_end(data)
        # Unknown record types are ignored (v1 contract: additive changes).

    def _apply_pipeline_start(self, data: dict[str, Any]) -> None:
        self.run_id = data.get("run_id") or self.run_id
        self.display_key = data.get("display_key")
        v = data.get("v")
        if isinstance(v, int):
            self.schema_v = v
        plan = data.get("plan")
        self.plan = plan if isinstance(plan, dict) else None
        started = data.get("started_at")
        if isinstance(started, str):
            try:
                self.started_at = datetime.fromisoformat(started)
            except ValueError:
                self.started_at = None
        for spec in data.get("steps") or []:
            name = spec.get("name")
            if not isinstance(name, str):
                continue
            step = self._step(name)
            step.level = int(spec.get("level") or 0)
            step.mode = self._map_mode(spec.get("mode"))
            step.model = spec.get("model")
        num_rows = data.get("num_rows")
        if isinstance(num_rows, int):
            self._ensure_dims(nrows=num_rows)
            for step in self._steps:
                step.total = step.total or num_rows

    def _apply_step_start(self, data: dict[str, Any]) -> None:
        name = data.get("step")
        if not isinstance(name, str):
            return
        step = self._step(name)
        step.level = int(data.get("level") or step.level)
        step.mode = self._map_mode(data.get("mode"))
        num_rows = data.get("num_rows")
        if isinstance(num_rows, int):
            step.total = num_rows
            self._ensure_dims(nrows=num_rows)

    def _apply_row_complete(self, record: TailRecord) -> None:
        data = record.data
        name = data.get("step")
        row = data.get("row")
        if not isinstance(name, str) or not isinstance(row, int) or row < 0:
            return
        step = self._step(name)
        self._ensure_dims(row=row)
        idx = self._step_idx[name]

        status = data.get("status")
        from_cache = bool(data.get("from_cache"))
        if status == "error":
            state = ERROR
        elif status == "skipped":
            state = SKIPPED
        elif from_cache:
            state = CACHED
        else:
            state = OK

        pos = row * len(self._steps)
        prev = self._cells[pos + idx]
        self._cells[pos + idx] = state
        if prev in TERMINAL:  # duplicate delivery: don't double-count
            step.done -= 1
            if prev == ERROR:
                step.errors -= 1
        step.done += 1
        if state == ERROR:
            step.errors += 1
        if state == CACHED:
            step.cached_count += 1

        t = data.get("t")
        t = float(t) if isinstance(t, (int, float)) else self.last_t
        self._completion_ts.append(t)

        values = data.get("values")
        values = values if isinstance(values, dict) else None
        error = data.get("error")
        error = error if isinstance(error, dict) else None
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else None
        elapsed_ms = data.get("elapsed_ms")
        elapsed_ms = float(elapsed_ms) if isinstance(elapsed_ms, (int, float)) else None

        cell = step.cells.get(row)
        if cell is None:
            cell = Cell()
            step.cells[row] = cell
        cell.from_cache = from_cache
        cell.error = error
        cell.usage = usage
        cell.elapsed_ms = elapsed_ms
        cell.t = t
        cell.values = values
        cell.preview = self._render_preview(state, values, error)
        cell.events.append((record.offset, record.length))

        if values:
            for key in values:
                if key not in step.fields:
                    step.fields.append(key)
            if self.display_key and self.display_key in values:
                key_val = values[self.display_key]
                if key_val is not None:
                    self._row_keys[row] = str(key_val)

        # --- cost --------------------------------------------------------
        if usage is not None:
            tokens_in = usage.get("in") if isinstance(usage.get("in"), int) else 0
            tokens_out = usage.get("out") if isinstance(usage.get("out"), int) else 0
            step.row_usage_seen = True
            step.row_in += tokens_in
            step.row_out += tokens_out
            usd = usage.get("cost")
            if not isinstance(usd, (int, float)):
                usd = price_usd(step.model, tokens_in, tokens_out, batch=step.is_batch)
                if usd is not None and step.is_batch:
                    full = price_usd(step.model, tokens_in, tokens_out)
                    step.batch_saved += (full or 0.0) - usd
            if usd is not None:
                cell.cost_usd = float(usd)
                step.row_cost = (step.row_cost or 0.0) + float(usd)
                if state == ERROR:
                    step.wasted += float(usd)
                elif state == OK and not from_cache:
                    step.priced_ok_cost += float(usd)
                    step.priced_ok_count += 1

        # --- error groups -------------------------------------------------
        if state == ERROR:
            etype = str((error or {}).get("type") or "Error")
            group = self._error_groups.get((name, etype))
            if group is None:
                group = ErrorGroup(step=name, type=etype, message="", first_t=t)
                self._error_groups[(name, etype)] = group
            if not group.message:
                group.message = str((error or {}).get("msg") or "")
            group.count += 1
            group.rows.add(row)
            group.first_t = min(group.first_t, t)
            group.last_t = max(group.last_t, t)
            group.ts.append(t)

    def _apply_step_end(self, data: dict[str, Any]) -> None:
        name = data.get("step")
        if not isinstance(name, str):
            return
        step = self._step(name)
        step.ended = True
        usage = data.get("usage")
        if isinstance(usage, dict):
            tokens_in = usage.get("in") if isinstance(usage.get("in"), int) else 0
            tokens_out = usage.get("out") if isinstance(usage.get("out"), int) else 0
            step.end_in = tokens_in
            step.end_out = tokens_out
            usd = usage.get("cost")
            if not isinstance(usd, (int, float)):
                usd = price_usd(step.model, tokens_in, tokens_out, batch=step.is_batch)
                if usd is not None and step.is_batch and not step.row_usage_seen:
                    full = price_usd(step.model, tokens_in, tokens_out)
                    step.batch_saved += (full or 0.0) - usd
            step.end_cost = float(usd) if usd is not None else None

    def _apply_pipeline_end(self, data: dict[str, Any]) -> None:
        self.finished = True
        elapsed = data.get("elapsed_s")
        if isinstance(elapsed, (int, float)):
            self.final_elapsed_s = float(elapsed)

    # -------------------------------------------------------------- previews

    @staticmethod
    def _render_preview(
        state: int,
        values: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> str | None:
        """One-line preview string, truncated; hides ``__``-internal fields."""
        if state == ERROR:
            etype = str((error or {}).get("type") or "Error")
            msg = str((error or {}).get("msg") or "")
            text = f"{etype} · {msg}" if msg else etype
            return _truncate(_one_line(text))
        if state in (OK, CACHED) and values:
            for key, value in values.items():
                if key.startswith("__"):
                    continue
                if value is None:
                    return None
                if isinstance(value, str):
                    return _truncate(_one_line(value))
                return _truncate(
                    _one_line(json.dumps(value, ensure_ascii=False, default=str))
                )
        return None

    # -------------------------------------------------------------- snapshot

    def _iso(self, t: float | None) -> str | None:
        if t is None or self.started_at is None:
            return None
        return _iso_utc(self.started_at + timedelta(seconds=t))

    def run_meta(self) -> dict[str, Any]:
        return {
            "id": self.run_id or self.path.stem,
            "name": self.path.stem,
            "started_at": _iso_utc(self.started_at) if self.started_at else None,
            "live": self.is_live(),
            "elapsed_s": self._elapsed_s(),
            "schema_v": self.schema_v,
        }

    def is_live(self) -> bool:
        """Recently written, or the log never saw its pipeline_end."""
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = 0.0
        recently_written = (time.time() - mtime) < LIVE_MTIME_WINDOW_S
        return recently_written or not self.finished

    def _elapsed_s(self) -> float:
        if self.finished and self.final_elapsed_s is not None:
            return self.final_elapsed_s
        if self.started_at is not None:
            started = self.started_at.astimezone(timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        return self.last_t

    def snapshot(self) -> dict[str, Any]:
        nsteps = len(self._steps)
        total_done = sum(s.done for s in self._steps)
        total_errors = sum(s.errors for s in self._steps)
        total_cached = sum(s.cached_count for s in self._steps)

        rows_done = 0
        cells = self._cells
        for r in range(self._nrows):
            base = r * nsteps
            if all(cells[base + j] in TERMINAL for j in range(nsteps)):
                rows_done += 1

        throughput = self._throughput_per_min()
        total_cells = self._nrows * nsteps
        remaining = max(0, total_cells - total_done)
        if self.finished:
            eta_s: float | None = 0.0
        elif throughput > 0:
            eta_s = remaining / (throughput / 60.0)
        else:
            eta_s = None

        cost = self._cost_block()
        spend = cost.pop("_spend")
        cache_saved = cost.pop("_cache_saved")

        return {
            "run": self.run_meta(),
            "steps": [
                {
                    "name": s.name,
                    "level": s.level,
                    "mode": s.mode,
                    "model": s.model,
                    "done": s.done,
                    "total": s.total,
                    "errors": s.errors,
                    "fields": list(s.fields),
                }
                for s in self._steps
            ],
            "rows": {"total": self._nrows, "done": rows_done},
            "stats": {
                "spend": spend,
                "cache_hit_rate": (total_cached / total_done) if total_done else 0.0,
                "errors": total_errors,
                "throughput_per_min": throughput,
                "eta_s": eta_s,
                "cache_saved": cache_saved,
            },
            "cells": {
                "encoding": "b64",
                "rows": self._nrows,
                "steps": nsteps,
                "data": base64.b64encode(bytes(cells)).decode("ascii"),
            },
            "error_groups": self._error_groups_block(),
            "cost": cost,
            "retry": self.retry_block(),
        }

    def retry_block(self) -> dict[str, Any]:
        return {
            "available": False,
            "reason": "launched without --pipeline",
            "resume_command": f"accrue-ui {self.path} --pipeline <module:attr>",
        }

    def _throughput_per_min(self) -> float:
        ts = self._completion_ts
        if not ts:
            return 0.0
        newest = ts[-1]
        window = min(THROUGHPUT_WINDOW_S, newest) or newest
        if window <= 0:
            return 0.0
        count = len(ts) - bisect.bisect_left(ts, newest - window)
        return count / window * 60.0

    def _error_groups_block(self) -> list[dict[str, Any]]:
        groups = sorted(self._error_groups.values(), key=lambda g: -g.count)
        out = []
        for g in groups:
            out.append(
                {
                    "step": g.step,
                    "type": g.type,
                    "count": g.count,
                    "message": g.message,
                    "rows": _compress_ranges(sorted(g.rows)),
                    "first_t": self._iso(g.first_t),
                    "last_t": self._iso(g.last_t),
                    "histogram": _histogram(g.ts, g.first_t, g.last_t),
                    "hint": _burst_hint(g.count, g.first_t, g.last_t),
                }
            )
        return out

    def _cost_block(self) -> dict[str, Any]:
        by_step: dict[str, float] = {}
        by_model: dict[str, float] = {}
        tokens_in = tokens_out = 0
        wasted = 0.0
        batch_saved = 0.0
        cache_saved = 0.0
        priced_any = False

        for s in self._steps:
            eff_in, eff_out = s.effective_tokens()
            tokens_in += eff_in
            tokens_out += eff_out
            usd = s.effective_cost()
            if usd is not None:
                priced_any = True
                by_step[s.name] = round(usd, 6)
                if s.model:
                    by_model[s.model] = round(by_model.get(s.model, 0.0) + usd, 6)
                wasted += s.wasted
                batch_saved += s.batch_saved
                if s.cached_count and s.priced_ok_count:
                    cache_saved += (
                        s.priced_ok_cost / s.priced_ok_count
                    ) * s.cached_count

        spend = round(sum(by_step.values()), 6) if priced_any else None
        return {
            "by_step": by_step,
            "by_model": by_model,
            "plan": self.plan,
            "tokens": {
                "input": tokens_in,
                "output": tokens_out,
                # v1 usage objects carry no cache token counts.
                "cache_read": 0,
                "cache_write": 0,
            },
            "wasted": round(wasted, 6) if priced_any else None,
            "batch_saved": round(batch_saved, 6) if priced_any else None,
            "_spend": spend,
            "_cache_saved": round(cache_saved, 6) if priced_any else None,
        }

    # ---------------------------------------------------------------- values

    def row_key(self, row: int) -> str:
        return self._row_keys.get(row, f"row {row}")

    def values_window(self, start: int, count: int) -> dict[str, Any]:
        nsteps = len(self._steps)
        start = max(0, min(start, self._nrows))
        end = max(start, min(start + max(0, count), self._nrows))
        rows = []
        for r in range(start, end):
            base = r * nsteps
            cells: dict[str, Any] = {}
            for j, s in enumerate(self._steps):
                cell = s.cells.get(r)
                cells[s.name] = {
                    "v": cell.preview if cell else None,
                    "s": self._cells[base + j],
                }
            rows.append({"row": r, "key": self.row_key(r), "cells": cells})
        return {"start": start, "rows": rows}

    # ------------------------------------------------------------------ cell

    def cell_detail(self, step_name: str, row: int) -> dict[str, Any] | None:
        idx = self._step_idx.get(step_name)
        if idx is None or row < 0 or row >= self._nrows:
            return None
        step = self._steps[idx]
        state = self._cells[row * len(self._steps) + idx]
        cell = step.cells.get(row)

        usage = None
        if cell is not None and cell.usage is not None:
            cost = cell.cost_usd
            if cost is None:
                raw_cost = cell.usage.get("cost")
                cost = raw_cost if isinstance(raw_cost, (int, float)) else None
            usage = {
                "in": cell.usage.get("in"),
                "out": cell.usage.get("out"),
                "cost": round(cost, 6) if cost is not None else None,
            }

        attempts = None
        if cell is not None and state in (OK, ERROR) and not cell.from_cache:
            attempts = [
                {
                    "n": 1,
                    "kind": "batch" if step.is_batch else "live",
                    "at": self._iso(cell.t),
                    "latency_ms": cell.elapsed_ms,
                    "status": "error" if state == ERROR else "ok",
                    "backoff_s": None,
                }
            ]

        return {
            "step": step_name,
            "row": row,
            "key": self.row_key(row),
            "status": STATE_NAMES[state],
            "from_cache": bool(cell.from_cache) if cell else False,
            "error": cell.error if cell else None,
            "usage": usage,
            "elapsed_ms": cell.elapsed_ms if cell else None,
            # The v1 log has no queue timestamps; a future emitter may add them.
            "queued_at": None,
            "attempts": attempts,
            "values": cell.values if cell and state in (OK, CACHED) else None,
            "raw_events": self._read_raw(cell.events) if cell else [],
        }

    def _read_raw(self, events: list[tuple[int, int]]) -> list[dict[str, Any]]:
        """Re-read raw records from the log by offset; nothing cached in RAM."""
        out: list[dict[str, Any]] = []
        if not events:
            return out
        with open(self.path, "rb") as f:
            for offset, length in events:
                f.seek(offset)
                raw = f.read(length)
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # file was replaced under us; offsets are stale
                if isinstance(parsed, dict):
                    out.append(parsed)
        return out


# ------------------------------------------------------------------- helpers


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _truncate(text: str, limit: int = PREVIEW_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compress_ranges(rows: list[int]) -> list[list[int]]:
    """[1, 2, 3, 7] -> [[1, 3], [7, 7]] (inclusive, sorted input)."""
    ranges: list[list[int]] = []
    for row in rows:
        if ranges and row == ranges[-1][1] + 1:
            ranges[-1][1] = row
        else:
            ranges.append([row, row])
    return ranges


def _histogram(ts: list[float], first_t: float, last_t: float) -> list[int]:
    buckets = [0] * HISTOGRAM_BUCKETS
    span = last_t - first_t
    for t in ts:
        if span <= 0:
            buckets[0] += 1
        else:
            i = min(
                HISTOGRAM_BUCKETS - 1, int((t - first_t) / span * HISTOGRAM_BUCKETS)
            )
            buckets[i] += 1
    return buckets


def _burst_hint(count: int, first_t: float, last_t: float) -> str | None:
    """The one implemented heuristic: error bursts suggest lowering concurrency."""
    span = last_t - first_t
    if count < BURST_MIN_COUNT or span >= BURST_MAX_SPAN_S:
        return None
    if span < 60:
        duration = f"{max(1, round(span))}-second"
    else:
        duration = f"{span / 60:.0f}-minute"
    return (
        f"All {count} landed in a {duration} burst — likely rate limiting; "
        "try halving concurrency (max_workers)."
    )


def scan_runs(directory: str | Path) -> list[dict[str, Any]]:
    """List known run logs in *directory*, newest first (for /api/runs)."""
    directory = Path(directory)
    runs = []
    if not directory.is_dir():
        return runs
    for path in directory.glob("*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        first = _read_first_record(path)
        started_at = None
        run_id = path.stem
        if first and first.get("type") == "pipeline_start":
            run_id = first.get("run_id") or run_id
            raw = first.get("started_at")
            if isinstance(raw, str):
                try:
                    started_at = _iso_utc(datetime.fromisoformat(raw))
                except ValueError:
                    started_at = None
        ended = _last_record_is_pipeline_end(path)
        live = (time.time() - st.st_mtime) < LIVE_MTIME_WINDOW_S or not ended
        mtime_iso = _iso_utc(datetime.fromtimestamp(st.st_mtime, tz=timezone.utc))
        runs.append(
            {
                "id": run_id,
                "name": path.stem,
                "path": str(path),
                "started_at": started_at,
                "live": live,
                "_sort": started_at or mtime_iso,
            }
        )
    runs.sort(key=lambda r: r["_sort"], reverse=True)
    for r in runs:
        del r["_sort"]
    return runs


def _read_first_record(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as f:
            line = f.readline(1 << 20)
    except OSError:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _last_record_is_pipeline_end(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - (1 << 16)))
            chunk = f.read()
    except OSError:
        return False
    for line in reversed(chunk.splitlines()):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            return False
        return isinstance(parsed, dict) and parsed.get("type") == "pipeline_end"
    return False
