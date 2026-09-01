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
state 1 is reserved for a future emitter.

A ``retry_failed()`` segment appends a second ``row_complete`` for cells it
re-executes, framed by ``retry_start`` / ``retry_end``. ``retry_start`` names
those cells exactly, which is what makes state 4 (retrying) knowable: they
are marked in flight there and leave every settled tally (step counters,
error groups, ``rows.done``) until their own ``row_complete`` lands.
Re-delivery is therefore normal, not a fault: the new record replaces the
cell's state, and the error groups heal with it — a cell that flips
error -> ok leaves its group, and the group disappears once its last row
does.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .pricing import price_usd
from .retry import RetryController
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
#: Floor on the throughput divisor. Dividing a handful of completions by the
#: first fraction of a second of log time reports thousands of rows/min and
#: an ETA to match; below this much log time there is no honest rate to give.
THROUGHPUT_MIN_WINDOW_S = 5.0
#: Row indices this large are treated as corrupt when the log never declared
#: a row count: the grid is rows x steps bytes, so one bogus index would
#: otherwise allocate gigabytes.
ROW_INDEX_CAP = 1_000_000
#: Raw records kept per cell (newest first out of the log, oldest dropped).
MAX_CELL_EVENTS = 50
#: Ceiling on a single prompt/response body read out of the sidecar. A body is
#: one LLM exchange; a length past this is a corrupt or hostile ``prompt_ref``,
#: not a body, and reading it whole is exactly the memory blow-up the
#: seek-by-offset design exists to avoid.
MAX_PROMPT_BODY_BYTES = 16 * 1024 * 1024
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
    #: field name -> rendered preview string, one entry per non-internal
    #: field this cell's step produced (same truncation as ``preview``, which
    #: is just this map's first entry). Populated for OK/CACHED cells only.
    preview_fields: dict[str, str | None] | None = None
    #: One entry per row_complete seen for this cell: (offset, length,
    #: from_a_retry_segment). The bytes are re-read on demand; the flag is
    #: what makes the inspector's attempt list say "retry".
    events: list[tuple[int, int, bool]] = field(default_factory=list)
    #: One (offset, length) per row_attempt record for this cell, in log order
    #: (== attempt order within a pass). Like ``events``, the bytes stay on
    #: disk and are re-read on demand — the sidecar prompt_ref each one carries
    #: is resolved only when the inspector opens the cell.
    attempt_events: list[tuple[int, int]] = field(default_factory=list)


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
    # --- timing (P21) -----------------------------------------------------
    #: step_end.elapsed_s — the step's wall-clock, start->finish. ``max`` over
    #: segments (a retry_failed segment re-opens the step and emits its own,
    #: shorter step_end; the main pass is the meaningful wall-clock).
    elapsed_s: float | None = None
    # --- retries (P22) ----------------------------------------------------
    #: row_attempt records with ``attempt > 1`` — attempts beyond the first for
    #: a (step, row), i.e. retries. The counter is monotonic 1-based per cell
    #: across the api+parse loops, so this never double-counts within a cell.
    retries: int = 0
    #: FAILED attempts (``status != "ok"``) bucketed by ``status`` and by
    #: ``kind`` (api/parse) — the *reasons* the step retried, for the card's
    #: dominant bucket and hover breakdown.
    retry_status: dict[str, int] = field(default_factory=dict)
    retry_kind: dict[str, int] = field(default_factory=dict)

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
    """Failures of one (step, error type), keyed by row so retries can heal.

    ``rows`` maps row index -> log-time of that row's failure, which makes
    membership the single source of truth: ``count`` is ``len(rows)``, the
    histogram is built from the timestamps still in it, and a healed row is
    removed by deleting one key. Re-delivery of the same failure (a retry
    that fails again) overwrites its entry instead of double-counting.
    """

    step: str
    type: str
    message: str
    rows: dict[int, float] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.rows)


class RunIndex:
    """Snapshot state for one run log; feed it records via :meth:`apply`."""

    def __init__(self, path: str | Path, retry: RetryController | None = None):
        self.path = Path(path)
        #: Prompt sidecar beside the run log — ``<run>.prompts.jsonl``, the same
        #: rule accrue's ``prompt_sidecar_path`` uses. Present only when the run
        #: captured bodies (``capture="prompts"``/``"full"``); ``/api/cell``
        #: seeks into it by offset and never reads it whole.
        self._sidecar_path = self.path.with_suffix(".prompts.jsonl")
        #: Retry orchestration for this run (``POST /api/retry``); a bare
        #: controller with no ``--pipeline`` simply reports unavailable.
        self.retry = retry if retry is not None else RetryController(path)
        self.run_id: str | None = None
        self.started_at: datetime | None = None
        self.display_key: str | None = None
        self.schema_v: int = SCHEMA_V
        self.plan: dict[str, Any] | None = None
        #: The pipeline blueprint accrue introspects at run start
        #: (``pipeline_start.manifest``, additive in the v1 contract): step
        #: definitions with their types/models/params, the enrichment-field
        #: schema, and the run config. ``None`` for older/metadata logs that
        #: predate the manifest — the Overview view degrades in that case.
        self.manifest: dict[str, Any] | None = None
        self.finished = False
        self.final_elapsed_s: float | None = None
        self.last_t = 0.0

        self._steps: list[StepState] = []
        self._step_idx: dict[str, int] = {}
        self._declared_rows: int | None = None  # pipeline_start's num_rows
        self._in_retry = False  # inside a retry_start..retry_end segment
        self._nrows = 0
        self._cells = bytearray()
        # Terminal cells per row + the count of fully-done rows, maintained
        # incrementally so rows.done / rows_done never rescan the grid.
        self._row_terminal: list[int] = []
        self._rows_done = 0
        self._row_keys: dict[int, str] = {}  # values-derived heuristic labels
        self._row_keys_explicit: dict[int, str] = {}  # from row_complete "key"
        self._error_groups: dict[tuple[str, str], ErrorGroup] = {}
        # Completion log-times inside the throughput window, non-decreasing
        # (log guarantee). Trimmed from the left on every append, so a
        # million-row run holds a window's worth, not a million floats.
        self._completion_ts: deque[float] = deque()
        self._old_nsteps = 0  # column count the cells array was laid out with
        # --- SSE delta tracking (drained by the coalescer, see drain_delta) --
        self._dirty_cells: dict[tuple[int, int], int] = {}  # (row, step) -> state
        self._dirty_steps: set[str] = set()
        self._sent_stats: dict[str, Any] = {}  # stats as of the last drain
        #: Set when this index replaced a truncate-and-regrown one (a tail
        #: generation bump, see app._follow): the next delta carries
        #: ``reset: true`` so clients drop their stale grid and refetch the
        #: whole snapshot rather than only gap-refetching out-of-grid indices.
        self._reset_pending = False

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
        if old_nsteps == nsteps:
            # Rows only grew: the new ones start with nothing terminal.
            self._row_terminal.extend([0] * (nrows - len(self._row_terminal)))
        else:
            self._recount_rows_done()

    def _recount_rows_done(self) -> None:
        """Rebuild the per-row terminal counts (a column count changed)."""
        nsteps = self._old_nsteps
        cells = self._cells
        self._row_terminal = [0] * self._nrows
        self._rows_done = 0
        for r in range(self._nrows):
            base = r * nsteps
            done = sum(1 for j in range(nsteps) if cells[base + j] in TERMINAL)
            self._row_terminal[r] = done
            if done == nsteps:
                self._rows_done += 1

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

    @property
    def _row_limit(self) -> int:
        """One past the highest row index this log is allowed to mention.

        The declared ``num_rows`` when there is one, else a hard cap. The
        cell grid is ``rows * steps`` bytes, so a single corrupt index
        (``row: 50000000``) would otherwise resize it to gigabytes.
        """
        if self._declared_rows is not None:
            return self._declared_rows
        return ROW_INDEX_CAP

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
        elif rtype == "row_attempt":
            self._apply_row_attempt(record)
        elif rtype == "row_complete":
            self._apply_row_complete(record)
        elif rtype == "step_end":
            self._apply_step_end(data)
        elif rtype == "pipeline_end":
            self._apply_pipeline_end(data)
        elif rtype == "retry_start":
            self._apply_retry_start(data)
        elif rtype == "retry_end":
            self._in_retry = False
        # Unknown record types are ignored (v1 contract: additive changes).

    def _apply_pipeline_start(self, data: dict[str, Any]) -> None:
        self.run_id = data.get("run_id") or self.run_id
        self.display_key = data.get("display_key")
        v = data.get("v")
        if isinstance(v, int):
            self.schema_v = v
        plan = data.get("plan")
        self.plan = plan if isinstance(plan, dict) else None
        manifest = data.get("manifest")
        self.manifest = manifest if isinstance(manifest, dict) else None
        self.started_at = _parse_iso(data.get("started_at"))
        for spec in data.get("steps") or []:
            name = spec.get("name")
            if not isinstance(name, str):
                continue
            step = self._step(name)
            step.level = int(spec.get("level") or 0)
            step.mode = self._map_mode(spec.get("mode"))
            step.model = spec.get("model")
        num_rows = data.get("num_rows")
        if isinstance(num_rows, int) and 0 <= num_rows <= ROW_INDEX_CAP:
            self._declared_rows = num_rows
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
        if isinstance(num_rows, int) and 0 <= num_rows <= self._row_limit:
            # A retry segment re-opens the step with only the retried rows;
            # the grid still has the whole run in it, so totals never shrink.
            step.total = max(step.total, num_rows)
            self._ensure_dims(nrows=num_rows)

    def _apply_retry_start(self, data: dict[str, Any]) -> None:
        """Mark the cells this retry segment is about to re-execute.

        ``retry_start`` lists them exactly (``cells: [{step, row}, ...]``),
        which is the one moment state 4 (retrying) is knowable: the v1 log
        has no per-row start events, so between here and each cell's
        ``row_complete`` these are the cells in flight. Their old terminal
        state is unwound first — the counters, the error group and
        ``rows.done`` all describe cells that are no longer settled.
        """
        self._in_retry = True
        cells = data.get("cells")
        if not isinstance(cells, list):
            return
        for entry in cells:
            if not isinstance(entry, dict):
                continue
            name, row = entry.get("step"), entry.get("row")
            if not isinstance(name, str) or not isinstance(row, int) or row < 0:
                continue
            if row >= self._row_limit:
                continue
            step = self._step(name)
            self._ensure_dims(row=row)
            idx = self._step_idx[name]
            pos = row * len(self._steps) + idx
            prev = self._cells[pos]
            if prev == RETRYING:
                continue
            if prev in TERMINAL:
                self._clear_terminal(step, row, prev)
            self._cells[pos] = RETRYING
            self._dirty_cells[(row, idx)] = RETRYING
            self._dirty_steps.add(name)

    def _clear_terminal(self, step: StepState, row: int, prev: int) -> None:
        """Undo everything a settled cell contributed; it is being re-run."""
        step.done -= 1
        if prev == ERROR:
            step.errors -= 1
            self._forget_error(step.name, row, step.cells.get(row))
        elif prev == CACHED:
            step.cached_count -= 1
        if self._row_terminal[row] == len(self._steps):
            self._rows_done -= 1
        self._row_terminal[row] -= 1

    def _apply_row_attempt(self, record: TailRecord) -> None:
        """Record one api/parse attempt; drive the retrying state from a real one.

        The v0.2 log emits a ``row_attempt`` per try, each with its own
        ``kind`` (``api``/``parse``), status and — at ``capture>=prompts`` — a
        ``prompt_ref`` into the sidecar. Only the (offset, length) is kept; the
        body and even the attempt fields are re-read on demand in
        :meth:`cell_detail`, so a million-row run holds offsets, not prompts.

        A non-``ok`` attempt is the retry signal P22 asks for: the cell has
        failed a try and another one (or a terminal ``row_complete``) is
        coming, so it shows as RETRYING in the interim instead of sitting at
        pending. Only a cell that is not already settled is flipped — a
        ``retry_start`` segment marks RETRYING and unwinds the prior terminal
        itself, so touching a terminal cell here would double-unwind it.
        """
        data = record.data
        name = data.get("step")
        row = data.get("row")
        if not isinstance(name, str) or not isinstance(row, int) or row < 0:
            return
        if row >= self._row_limit:
            return
        step = self._step(name)
        self._ensure_dims(row=row)
        idx = self._step_idx[name]

        cell = step.cells.get(row)
        if cell is None:
            cell = Cell()
            step.cells[row] = cell
        cell.attempt_events.append((record.offset, record.length))
        if len(cell.attempt_events) > MAX_CELL_EVENTS:
            del cell.attempt_events[:-MAX_CELL_EVENTS]

        # --- retry aggregation (P22) -------------------------------------
        # A row_attempt record is applied exactly once (append-only log), so
        # incrementing here never double-counts — unlike row_complete, which
        # is re-delivered by retry segments and must be unwound.
        status = data.get("status")
        attempt_no = data.get("attempt")
        if isinstance(attempt_no, int) and attempt_no > 1:
            step.retries += 1
        if isinstance(status, str) and status != "ok":
            step.retry_status[status] = step.retry_status.get(status, 0) + 1
            kind = data.get("kind")
            if isinstance(kind, str):
                step.retry_kind[kind] = step.retry_kind.get(kind, 0) + 1

        if status is not None and status != "ok":
            pos = row * len(self._steps) + idx
            if self._cells[pos] not in TERMINAL:
                # Only the cell state moves — no step counter changes when a
                # cell goes pending -> retrying, so the step stays out of the
                # delta (its done/errors are unchanged).
                self._cells[pos] = RETRYING
                self._dirty_cells[(row, idx)] = RETRYING

    def _apply_row_complete(self, record: TailRecord) -> None:
        data = record.data
        name = data.get("step")
        row = data.get("row")
        if not isinstance(name, str) or not isinstance(row, int) or row < 0:
            return
        if row >= self._row_limit:
            # Beyond what the run declared (or beyond any plausible run):
            # a corrupt index, not a row. Sizing the grid to it would
            # allocate a 50M-cell array for one bad line.
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

        cell = step.cells.get(row)
        nsteps = len(self._steps)
        pos = row * nsteps
        prev = self._cells[pos + idx]
        self._cells[pos + idx] = state
        self._dirty_cells[(row, idx)] = state  # latest state wins (coalescing)
        self._dirty_steps.add(name)
        if prev in TERMINAL:
            # Re-delivery (a retry segment, or a duplicate): unwind the old
            # terminal state before applying the new one. A cell marked
            # RETRYING by retry_start was already unwound there.
            self._clear_terminal(step, row, prev)
        self._row_terminal[row] += 1
        if self._row_terminal[row] == nsteps:
            self._rows_done += 1
        step.done += 1
        if state == ERROR:
            step.errors += 1
        if state == CACHED:
            step.cached_count += 1

        t = data.get("t")
        t = float(t) if isinstance(t, (int, float)) else self.last_t
        self._trim_completions(t)

        values = data.get("values")
        values = values if isinstance(values, dict) else None
        error = data.get("error")
        error = error if isinstance(error, dict) else None
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else None
        elapsed_ms = data.get("elapsed_ms")
        elapsed_ms = float(elapsed_ms) if isinstance(elapsed_ms, (int, float)) else None

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
        cell.preview_fields = self._render_field_map(state, values)
        cell.events.append((record.offset, record.length, self._in_retry))
        if len(cell.events) > MAX_CELL_EVENTS:
            # A pathological log (or a cell retried hundreds of times) must
            # not grow this list forever; the inspector wants the recent
            # attempts, not all of them.
            del cell.events[:-MAX_CELL_EVENTS]

        # Row label: an explicit "key" field (additive in core, string|null)
        # wins over the values-derived heuristic; logs without it still label
        # rows via display_key found in step outputs.
        explicit_key = data.get("key")
        if isinstance(explicit_key, str):
            self._row_keys_explicit[row] = explicit_key

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
            etype = _error_type(error)
            group = self._error_groups.get((name, etype))
            if group is None:
                group = ErrorGroup(step=name, type=etype, message="")
                self._error_groups[(name, etype)] = group
            if not group.message:
                group.message = str((error or {}).get("msg") or "")
            group.rows[row] = t

    def _trim_completions(self, t: float) -> None:
        """Record a completion at log-time *t*, dropping ones out of window."""
        ts = self._completion_ts
        ts.append(t)
        cutoff = t - THROUGHPUT_WINDOW_S
        while ts and ts[0] < cutoff:
            ts.popleft()

    def _forget_error(self, step_name: str, row: int, cell: Cell | None) -> None:
        """Drop *row* from the group its previous failure belonged to.

        Called whenever a cell leaves the error state — healed by a retry, or
        re-failed with a different error type (it then joins the new group).
        The group itself disappears with its last row, so a fully healed run
        reports no error groups at all.
        """
        key = (step_name, _error_type(cell.error if cell else None))
        group = self._error_groups.get(key)
        if group is None:
            return
        group.rows.pop(row, None)
        if not group.rows:
            del self._error_groups[(group.step, group.type)]

    def _apply_step_end(self, data: dict[str, Any]) -> None:
        name = data.get("step")
        if not isinstance(name, str):
            return
        step = self._step(name)
        step.ended = True
        elapsed = data.get("elapsed_s")
        if isinstance(elapsed, (int, float)):
            # max over segments: a retry_failed segment re-opens the step with
            # a fresh, shorter step_end; the main pass is the wall-clock worth
            # showing, and the two segments do not overlap in wall time anyway.
            step.elapsed_s = max(step.elapsed_s or 0.0, float(elapsed))
        usage = data.get("usage")
        if isinstance(usage, dict):
            tokens_in = usage.get("in") if isinstance(usage.get("in"), int) else 0
            tokens_out = usage.get("out") if isinstance(usage.get("out"), int) else 0
            # Accumulate: a step_end arrives once per segment, so a retry adds
            # what it spent rather than replacing the run's aggregate.
            step.end_in += tokens_in
            step.end_out += tokens_out
            usd = usage.get("cost")
            if not isinstance(usd, (int, float)):
                usd = price_usd(step.model, tokens_in, tokens_out, batch=step.is_batch)
                if usd is not None and step.is_batch and not step.row_usage_seen:
                    full = price_usd(step.model, tokens_in, tokens_out)
                    step.batch_saved += (full or 0.0) - usd
            if usd is not None:
                step.end_cost = (step.end_cost or 0.0) + float(usd)

    def _apply_pipeline_end(self, data: dict[str, Any]) -> None:
        self.finished = True
        elapsed = data.get("elapsed_s")
        if isinstance(elapsed, (int, float)):
            self.final_elapsed_s = float(elapsed)

    # -------------------------------------------------------------- previews

    @staticmethod
    def _render_value(value: Any) -> str | None:
        """One-line, truncated rendering of a single field's value.

        Strings pass through as-is; other JSON-able values are dumped first.
        ``None`` means "nothing to show" and is preserved as ``None`` rather
        than the string ``"None"``.
        """
        if value is None:
            return None
        if isinstance(value, str):
            return _truncate(_one_line(value))
        return _truncate(_one_line(json.dumps(value, ensure_ascii=False, default=str)))

    @staticmethod
    def _render_preview(
        state: int,
        values: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> str | None:
        """One-line preview string, truncated; hides ``__``-internal fields.

        This is the step's *first* non-internal field only — for all of a
        cell's produced fields, see ``_render_field_map``.
        """
        if state == ERROR:
            etype = str((error or {}).get("type") or "Error")
            msg = str((error or {}).get("msg") or "")
            text = f"{etype} · {msg}" if msg else etype
            return _truncate(_one_line(text))
        if state in (OK, CACHED) and values:
            for key, value in values.items():
                if key.startswith("__"):
                    continue
                return RunIndex._render_value(value)
        return None

    @staticmethod
    def _render_field_map(
        state: int, values: dict[str, Any] | None
    ) -> dict[str, str | None] | None:
        """Per-field rendered previews for an OK/CACHED cell.

        One entry per non-internal field the step produced (same hiding and
        truncation rules as ``_render_preview``), so the data grid can show
        whichever field the column's field-chip currently selects. ``None``
        for cells with nothing to render (error/retrying/pending/skipped, or
        no values at all).
        """
        if state not in (OK, CACHED) or not values:
            return None
        return {
            key: RunIndex._render_value(value)
            for key, value in values.items()
            if not key.startswith("__")
        }

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
        """Was the log written to just now? That is the whole definition.

        Emphatically NOT "or it never saw a pipeline_end": a run killed
        three days ago has no pipeline_end and never will, and pulsing LIVE
        at it forever is a lie the whole UI then builds on (a climbing
        elapsed clock, an ETA, a throughput number).
        """
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            return False
        return (time.time() - mtime) < LIVE_MTIME_WINDOW_S

    def _elapsed_s(self) -> float:
        """Wall-clock for a live run; the log's own span for a dead one."""
        if self.finished and self.final_elapsed_s is not None:
            return self.final_elapsed_s
        if self.started_at is not None and self.is_live():
            started = self.started_at.astimezone(timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        # Interrupted and long cold: the run lasted until its last record,
        # not until now.
        return self.last_t

    def _stats_block(self, cost: dict[str, Any]) -> dict[str, Any]:
        """The ``/api/run`` "stats" object; *cost* comes from _cost_block()."""
        total_done = sum(s.done for s in self._steps)
        total_errors = sum(s.errors for s in self._steps)
        total_cached = sum(s.cached_count for s in self._steps)
        throughput = self._throughput_per_min()
        total_cells = self._nrows * len(self._steps)
        remaining = max(0, total_cells - total_done)
        if self.finished:
            eta_s: float | None = 0.0
        elif throughput:
            eta_s = remaining / (throughput / 60.0)
        else:
            eta_s = None
        return {
            "spend": cost["_spend"],
            "cache_hit_rate": (total_cached / total_done) if total_done else 0.0,
            "errors": total_errors,
            "throughput_per_min": throughput,
            "eta_s": eta_s,
            "cache_saved": cost["_cache_saved"],
        }

    def snapshot(self) -> dict[str, Any]:
        nsteps = len(self._steps)
        cells = self._cells
        cost = self._cost_block()
        stats = self._stats_block(cost)
        del cost["_spend"], cost["_cache_saved"]

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
            "rows": {"total": self._nrows, "done": self._rows_done},
            "stats": stats,
            "cells": {
                "encoding": "b64",
                "rows": self._nrows,
                "steps": nsteps,
                "data": base64.b64encode(bytes(cells)).decode("ascii"),
            },
            "error_groups": self._error_groups_block(),
            "cost": cost,
            "retry": self.retry_block(),
            "overview": self.overview_block(cost["by_step"]),
        }

    def retry_block(self) -> dict[str, Any]:
        """``/api/run``'s retry block — availability comes from the controller."""
        return self.retry.block()

    def overview_block(
        self, by_step_cost: dict[str, float] | None = None
    ) -> dict[str, Any]:
        """The pipeline blueprint for the Overview view (docs/api-shapes.md).

        Sourced from ``pipeline_start.manifest`` — accrue's introspection of
        the pipeline at run start: each step's type, model + params, produced
        fields, dependencies and condition, plus the enrichment-field schema
        and the run config. Every manifest step is annotated with its *live*
        outcome (rows done/errors and priced cost) by looking the step up by
        name in the same per-step state that backs ``/api/run`` — so the
        blueprint and the running numbers never diverge.

        ``present`` is ``False`` for older/metadata logs that carry no
        manifest; the view then degrades to what ``steps[]`` alone gives
        rather than crashing. *by_step_cost* is the ``cost.by_step`` map (the
        snapshot passes its own to avoid recomputing); omitted, it is derived.
        """
        manifest = self.manifest
        if manifest is None:
            return {"present": False}

        if by_step_cost is None:
            by_step_cost = self._cost_block()["by_step"]
        live_by_name = {s.name: s for s in self._steps}

        steps_out: list[dict[str, Any]] = []
        providers: set[str] = set()
        for spec in manifest.get("steps") or []:
            if not isinstance(spec, dict):
                continue
            name = spec.get("name")
            model = spec.get("model") if isinstance(spec.get("model"), dict) else None
            if model and isinstance(model.get("provider"), str):
                providers.add(model["provider"])
            live = live_by_name.get(name) if isinstance(name, str) else None
            sys_prompt = spec.get("system_prompt")
            steps_out.append(
                {
                    "name": name,
                    # A type accrue could not introspect is "unknown" — render
                    # it plainly rather than hiding the step.
                    "type": spec.get("type") or "unknown",
                    "model": model,  # null for FunctionSteps
                    # The row-independent system prompt (the exact cached
                    # prefix, #107), secret-redacted by accrue. null for
                    # FunctionSteps / steps without one — the panel is omitted.
                    "system_prompt": sys_prompt
                    if isinstance(sys_prompt, str)
                    else None,
                    "produces": [
                        p for p in (spec.get("produces") or []) if isinstance(p, str)
                    ],
                    "depends_on": [
                        d for d in (spec.get("depends_on") or []) if isinstance(d, str)
                    ],
                    "condition": spec.get("condition"),  # may be null
                    "level": live.level if live else 0,
                    "mode": live.mode if live else "live",
                    "outcome": {
                        "done": live.done if live else 0,
                        "total": live.total if live else 0,
                        "errors": live.errors if live else 0,
                        # number | null, exactly as cost.by_step: a step with no
                        # priceable model contributes nothing and reads as an
                        # em-dash, never a misleading $0.00.
                        "cost": by_step_cost.get(name)
                        if isinstance(name, str)
                        else None,
                        # --- timing (P21) --------------------------------
                        # Wall-clock (step_end.elapsed_s); null until the step
                        # ends. `ended` lets the view show a running state
                        # instead of a final duration for in-progress steps.
                        "wall_s": live.elapsed_s if live else None,
                        "ended": bool(live.ended) if live else False,
                        # p50/p95 of per-row latency, cached rows EXCLUDED;
                        # null for batch steps and steps with no timed row.
                        "latency_ms": self._step_latency(live) if live else None,
                        # --- retries (P22) -------------------------------
                        # {count, by_status, by_kind, dominant}; null when the
                        # step never retried (count == 0) so the card can omit
                        # the chip entirely.
                        "retry": self._step_retry(live) if live else None,
                    },
                }
            )

        fields_out: list[dict[str, Any]] = []
        for f in manifest.get("fields") or []:
            if not isinstance(f, dict):
                continue
            fields_out.append(
                {
                    "name": f.get("name"),
                    "type": f.get("type") or "unknown",
                    "enum": f.get("enum") if isinstance(f.get("enum"), list) else None,
                    "description": f.get("description")
                    if isinstance(f.get("description"), str)
                    else None,
                    "step": f.get("step"),
                    "internal": bool(f.get("internal")),
                }
            )

        config = manifest.get("config")
        version = manifest.get("accrue_version")
        return {
            "present": True,
            "accrue_version": version if isinstance(version, str) else None,
            "config": config if isinstance(config, dict) else {},
            # The declared sample size (pipeline_start.num_rows), falling back
            # to the widest row index seen when the log never declared one.
            "sample_size": self._declared_rows
            if self._declared_rows is not None
            else self._nrows,
            # Total pipeline wall-clock (pipeline_end.elapsed_s); null until the
            # run ends. For a live run the view falls back to run.elapsed_s.
            "pipeline_wall_s": self.final_elapsed_s,
            "providers": sorted(providers),
            "steps": steps_out,
            "fields": fields_out,
        }

    def _step_latency(self, step: StepState) -> dict[str, Any] | None:
        """p50/p95 of per-row latency for a step, cached rows EXCLUDED (P21).

        Cached cells settle in ~0ms (no API call happened), so counting them
        would drag the percentiles toward zero and understate real call
        latency — they are dropped from the distribution. Batch steps get
        ``None``: their per-row ``elapsed_ms`` includes provider queue time, so
        a per-row latency there is misleading (the card shows wall-clock only).
        ``None`` when no timed, non-cached row exists yet.

        Sourced from the same ``cell.elapsed_ms`` the inspector shows, so the
        percentiles reconcile with the per-cell timelines.
        """
        if step is None or step.is_batch:
            return None
        samples = sorted(
            c.elapsed_ms
            for c in step.cells.values()
            if c.elapsed_ms is not None and not c.from_cache
        )
        if not samples:
            return None
        return {
            "p50": round(_percentile(samples, 0.50), 3),
            "p95": round(_percentile(samples, 0.95), 3),
            "n": len(samples),
        }

    def _step_retry(self, step: StepState) -> dict[str, Any] | None:
        """Per-step retry aggregation for the Overview card (P22).

        ``count`` = row_attempt records with ``attempt > 1`` (attempts beyond
        the first for a cell). ``by_status`` / ``by_kind`` tally the *failed*
        attempts (``status != "ok"``) — the reasons the step retried — and
        ``dominant`` names the top status bucket for the card's inline chip.
        ``None`` when the step never retried, so the card omits the chip
        entirely (noise reduction). Works at every capture tier: row_attempt
        is emitted even at ``capture="metadata"``.
        """
        if step is None or step.retries <= 0:
            return None
        by_status = dict(
            sorted(step.retry_status.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        dominant = None
        if by_status:
            name, count = next(iter(by_status.items()))
            dominant = {"status": name, "count": count}
        return {
            "count": step.retries,
            "by_status": by_status,
            "by_kind": dict(
                sorted(step.retry_kind.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
            "dominant": dominant,
        }

    # ---------------------------------------------------------------- deltas

    def mark_reset(self) -> None:
        """Flag the next delta as a reset (truncate-and-regrow reindex).

        Called by the follower when the tail's generation bumped and it built
        a fresh index over the new file. The next :meth:`drain_delta` then
        carries ``reset: true`` — the signal a client uses to drop its stale
        grid and refetch the whole snapshot, since cells that existed only in
        the old file are in-grid (not gap-detectable) and would otherwise keep
        their stale bytes.
        """
        self._reset_pending = True

    def drain_delta(self) -> dict[str, Any] | None:
        """Collect all changes since the last drain into one SSE delta payload.

        Coalescing happens here: a cell touched several times between drains
        yields ONE triple carrying its latest state, ``steps`` report current
        counters, and ``stats`` holds only the keys whose value changed since
        the last drain (plus the two delta-only keys, ``rows_done`` — the
        mirror of the snapshot's ``rows.done`` — and ``live`` — the mirror of
        ``run.live`` — so the progress bar and the LIVE badge track a live run
        without refetching). The payload is state, not a journal — draining
        resets the accumulators, so memory stays bounded by the grid size no
        matter how fast the log grows. Returns None when nothing changed
        (unless a reset is pending, which always emits). Shape:
        docs/api-shapes.md, ``GET /api/events``.
        """
        cells = [
            [row, step_i, state] for (row, step_i), state in self._dirty_cells.items()
        ]
        steps = [
            {"name": s.name, "done": s.done, "errors": s.errors}
            for s in self._steps
            if s.name in self._dirty_steps
        ]
        stats = {
            **self._stats_block(self._cost_block()),
            "rows_done": self._rows_done,
            # Delta-only mirror of run.live: mtime recency, so a run that
            # finishes with the page open clears its LIVE badge and stops the
            # elapsed ticker when this flips false, without a snapshot refetch.
            "live": self.is_live(),
        }
        changed_stats = {
            k: v
            for k, v in stats.items()
            if k not in self._sent_stats or self._sent_stats[k] != v
        }
        reset = self._reset_pending
        if not cells and not steps and not changed_stats and not reset:
            return None
        self._dirty_cells.clear()
        self._dirty_steps.clear()
        self._sent_stats = stats
        self._reset_pending = False
        delta: dict[str, Any] = {
            "t": round(self._elapsed_s(), 3),
            "cells": cells,
            "stats": changed_stats,
            "steps": steps,
        }
        if reset:
            # Absent means false (backward-compatible); present only when set.
            delta["reset"] = True
        return delta

    def _throughput_per_min(self) -> float | None:
        """Completions per minute over the recent window, or None if unknowable.

        The divisor is log time, floored at THROUGHPUT_MIN_WINDOW_S: the
        first few completions of a run land within milliseconds of each
        other, and dividing by that reports a rate of tens of thousands per
        minute (and an ETA of seconds for an hour-long run). Until the log
        has that much time in it there is no rate to report, so the tile
        shows an em-dash rather than a fantasy.
        """
        ts = self._completion_ts
        if not ts:
            return None
        window = min(THROUGHPUT_WINDOW_S, ts[-1])
        if window < THROUGHPUT_MIN_WINDOW_S:
            return None
        # The deque holds exactly the window (trimmed on append), so its
        # length is the count — no scan, no bisect.
        return len(ts) / window * 60.0

    def _error_groups_block(self) -> list[dict[str, Any]]:
        groups = sorted(self._error_groups.values(), key=lambda g: -g.count)
        out = []
        for g in groups:
            ts = list(g.rows.values())
            first_t, last_t = min(ts), max(ts)
            out.append(
                {
                    "step": g.step,
                    "type": g.type,
                    "count": g.count,
                    "message": g.message,
                    "rows": _compress_ranges(sorted(g.rows)),
                    "first_t": self._iso(first_t),
                    "last_t": self._iso(last_t),
                    "histogram": _histogram(ts, first_t, last_t),
                    "hint": _burst_hint(g.count, first_t, last_t),
                }
            )
        return out

    # ------------------------------------------------------------ retry rows

    def error_rows(self) -> list[int]:
        """Every row currently in an error state, deduped and sorted."""
        rows: set[int] = set()
        for group in self._error_groups.values():
            rows.update(group.rows)
        return sorted(rows)

    def group_rows(self, step: str, error_type: str) -> list[int] | None:
        """Rows of one error group, or None when no such group exists."""
        group = self._error_groups.get((step, error_type))
        return sorted(group.rows) if group is not None else None

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
        """Display label: explicit ``row_complete.key`` wins over heuristic."""
        explicit = self._row_keys_explicit.get(row)
        if explicit is not None:
            return explicit
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
                    # Field -> rendered value, all of the step's produced
                    # (non-internal) fields — what lets the data grid's
                    # field-chip cycle the actually-rendered value, not just
                    # the column label (accrue-ui#23). "v" above is kept for
                    # back-compat; it is this map's first entry.
                    "f": cell.preview_fields if cell else None,
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

        completions, attempt_recs, raw_events = self._read_cell_records(cell)

        # Real per-attempt timeline when the log carries row_attempt records
        # (v0.2); otherwise synthesize one attempt per row_complete (v0.1 logs,
        # which have no row_attempt — the pinned run_small fixture is one).
        prompt = None
        if attempt_recs:
            attempts = self._attempts_from_records(attempt_recs)
            prompt = self._resolve_prompt(attempt_recs)
        elif completions and cell is not None and not cell.from_cache:
            attempts = self._attempts(step, state, completions)
        else:
            attempts = None

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
            # Captured request/response body for this cell's last body-bearing
            # attempt, or null. capture_available says which empty state the
            # inspector shows: a real "no capture on this run" hint vs. a cell
            # that simply had no body (a pure api error).
            "prompt": prompt,
            "capture_available": self._sidecar_path.exists(),
            "values": cell.values if cell and state in (OK, CACHED) else None,
            "raw_events": raw_events,
        }

    def _attempts(
        self, step: StepState, state: int, records: list[tuple[dict[str, Any], bool]]
    ) -> list[dict[str, Any]] | None:
        """One attempt per row_complete this cell actually produced.

        A retried cell has two or more records in the log, so "Failed after
        1 attempts" was never true of a cell you had already retried — the
        count came from a hard-coded single-entry list. Records that landed
        inside a ``retry_start`` … ``retry_end`` segment are kind ``retry``;
        the rest are ``batch`` or ``live`` after the step's mode.
        """
        if state in (PENDING, SKIPPED):
            return None  # nothing ever ran for this cell
        attempts = []
        for n, (record, from_retry) in enumerate(records, start=1):
            if from_retry:
                kind = "retry"
            else:
                kind = "batch" if step.is_batch else "live"
            latency = record.get("elapsed_ms")
            t = record.get("t")
            attempts.append(
                {
                    "n": n,
                    "kind": kind,
                    "at": self._iso(float(t)) if isinstance(t, (int, float)) else None,
                    "latency_ms": latency
                    if isinstance(latency, (int, float))
                    else None,
                    "status": "error" if record.get("status") == "error" else "ok",
                    # The v1 log records no inter-attempt sleep.
                    "backoff_s": None,
                }
            )
        return attempts

    def _read_cell_records(
        self, cell: Cell | None
    ) -> tuple[
        list[tuple[dict[str, Any], bool]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        """Re-read every raw record for a cell from the log in one pass, by offset.

        Nothing is cached in RAM — only the byte spans are, so this seeks each
        line back out on demand. Returns ``(completions, attempts, raw_events)``:
        completions pair each ``row_complete`` with its retry-segment flag (the
        v0.1 fallback attempt list needs it), attempts are the ``row_attempt``
        records, and raw_events is every record for the cell in log order (by
        offset) — the verbatim JSONL the inspector's Raw tab shows.
        """
        completions: list[tuple[dict[str, Any], bool]] = []
        attempts: list[dict[str, Any]] = []
        raw_events: list[dict[str, Any]] = []
        if cell is None:
            return completions, attempts, raw_events
        # (offset, length, is_row_complete, from_retry). Sorting by offset is
        # log order, which for attempts is also attempt order within a pass.
        tagged = [(off, ln, True, retry) for off, ln, retry in cell.events]
        tagged += [(off, ln, False, False) for off, ln in cell.attempt_events]
        if not tagged:
            return completions, attempts, raw_events
        tagged.sort(key=lambda item: item[0])
        with open(self.path, "rb") as f:
            for offset, length, is_complete, from_retry in tagged:
                f.seek(offset)
                raw = f.read(length)
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # file was replaced under us; offsets are stale
                if not isinstance(parsed, dict):
                    continue
                raw_events.append(parsed)
                if is_complete:
                    completions.append((parsed, from_retry))
                else:
                    attempts.append(parsed)
        return completions, attempts, raw_events

    def _attempts_from_records(
        self, attempt_recs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """One entry per ``row_attempt`` record, in log (attempt) order.

        Carries the real ``kind`` (``api``/``parse``), status, latency, backoff
        and error the log recorded — so the count is the true number of tries
        (P21), not a hard-coded 1 — plus the ``prompt_ref`` that tells a client
        which tries have a captured body.
        """
        attempts = []
        for n, rec in enumerate(attempt_recs, start=1):
            t = rec.get("t")
            latency = rec.get("latency_ms")
            backoff = rec.get("backoff_s")
            status = rec.get("status")
            error = rec.get("error")
            attempt_no = rec.get("attempt")
            attempts.append(
                {
                    "n": n,
                    "attempt": attempt_no if isinstance(attempt_no, int) else n,
                    "kind": rec.get("kind"),
                    "at": self._iso(float(t)) if isinstance(t, (int, float)) else None,
                    "latency_ms": latency
                    if isinstance(latency, (int, float))
                    else None,
                    "status": status if isinstance(status, str) else None,
                    "backoff_s": backoff if isinstance(backoff, (int, float)) else None,
                    "error": error if isinstance(error, dict) else None,
                    "prompt_ref": _prompt_ref(rec.get("prompt_ref")),
                }
            )
        return attempts

    def _resolve_prompt(
        self, attempt_recs: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Captured body for the cell's last body-bearing attempt, or None.

        The last attempt carrying a ``prompt_ref`` is the try that actually
        settled the cell (an api error carries none), so its body is the
        prompt/response worth showing. Resolved by seeking the sidecar to the
        recorded byte offset — never by reading the file whole.
        """
        ref = None
        for rec in attempt_recs:
            candidate = _prompt_ref(rec.get("prompt_ref"))
            if candidate is not None:
                ref = candidate
        if ref is None:
            return None
        return self._read_prompt_body(ref["off"], ref["len"])

    def _read_prompt_body(self, off: int, length: int) -> dict[str, Any] | None:
        """Seek the sidecar to *off*, read exactly *length* bytes, parse one body.

        Seek-only by contract: a 500MB capture is never read whole, so a single
        prompt costs one seek and one bounded read. Returns the
        ``{messages, response, parsed}`` body (already secret-redacted by accrue
        at capture time) or None when the sidecar is absent, the span is corrupt,
        or the bytes do not parse.
        """
        if off < 0 or length <= 0 or length > MAX_PROMPT_BODY_BYTES:
            return None
        try:
            with open(self._sidecar_path, "rb") as f:
                f.seek(off)
                raw = f.read(length)
        except OSError:
            return None
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        return body if isinstance(body, dict) else None


# ------------------------------------------------------------------- helpers


def _error_type(error: dict[str, Any] | None) -> str:
    """Group key for a failure; the v1 log's error object may be null."""
    return str((error or {}).get("type") or "Error")


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolation percentile of a *pre-sorted* list (numpy default).

    ``q`` in ``0..1``. Interpolates between the two ranks straddling the
    target position, so p50 of an even-length list is the mean of the middle
    pair and a small sample never snaps to a misleading exact member. Assumes
    a non-empty, ascending input (the one caller guarantees both).
    """
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _prompt_ref(raw: Any) -> dict[str, int] | None:
    """Validate a ``row_attempt`` ``prompt_ref`` into ``{off, len}`` or None.

    Null for api-error attempts and for every attempt of a metadata-tier run;
    a well-formed one is two non-negative byte integers into the sidecar.
    """
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("off"), int)
        and isinstance(raw.get("len"), int)
        and raw["off"] >= 0
        and raw["len"] >= 0
    ):
        return {"off": raw["off"], "len": raw["len"]}
    return None


def _parse_iso(raw: Any) -> datetime | None:
    """Parse a log timestamp, tolerating the ``Z`` suffix.

    ``datetime.fromisoformat`` only learned ``Z`` in 3.11 and this package
    supports 3.10 — and Z-form is exactly what the fixtures (and any emitter
    using ``isoformat().replace("+00:00", "Z")``) write.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


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
            parsed = _parse_iso(first.get("started_at"))
            started_at = _iso_utc(parsed) if parsed else None
        # Live means "being written to right now" — same rule as
        # RunIndex.is_live(), and for the same reason (an interrupted run
        # has no pipeline_end and is not live because of it).
        live = (time.time() - st.st_mtime) < LIVE_MTIME_WINDOW_S
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
