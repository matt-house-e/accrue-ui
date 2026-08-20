# API shapes (v0.1 contract)

The frontend consumes these routes; the server (issue #1/#3) implements them.
This document is the contract — both lanes code to it. All routes are
same-origin (`/api/*`), JSON unless noted. Timestamps are ISO 8601 UTC
strings (`2026-08-17T09:41:02Z`). Money is USD as a JSON number. Reference
fixtures live in `tests/fixtures/api/` and conform to these shapes exactly.

## Cell-state bytes

Cell state fits one byte (also documented in CLAUDE.md):

| Byte | State |
|------|-----------|
| 0 | pending |
| 1 | running |
| 2 | ok |
| 3 | cached |
| 4 | retrying |
| 5 | error |
| 6 | skipped |

## `GET /api/run`

Full snapshot of the run, including the complete cell-state array.

```jsonc
{
  "run": {
    "id": "2026-08-17a",           // run identifier (log-derived)
    "name": "acme-enrichment",     // pipeline/run name
    "started_at": "2026-08-17T09:35:12Z",
    "live": true,                  // log is still being appended
    "elapsed_s": 401,              // seconds since started_at at snapshot time
    "schema_v": 1                  // run-log schema version
  },
  "steps": [                       // pipeline order; index = stepIndex in deltas
    {
      "name": "classify",
      "level": 0,                  // dependency level (L0 first)
      "mode": "live",              // "live" | "batch"
      "model": "claude-sonnet-5",
      "done": 5000,                // cells finished (ok + cached + error + skipped)
      "total": 5000,
      "errors": 1,                 // cells currently in error state
      "fields": ["category", "confidence"]  // OPTIONAL: output field names for
                                   // the data-mode field picker; "__"-prefixed
                                   // names are internal fields
    }
  ],
  "rows": { "total": 5000, "done": 3412 },  // done = rows complete through the last step
  "stats": {
    "spend": 4.83,                 // USD spent so far
    "cache_hit_rate": 0.72,        // 0..1
    "errors": 43,                  // total errored cells
    "throughput_per_min": 248,     // completed cells/min (recent window)
    "eta_s": 384,                  // estimated seconds remaining; null when unknown
    "cache_saved": 1.94            // USD saved by cache hits
  },
  "cells": {
    "encoding": "b64",
    "rows": 5000,
    "steps": 5,
    "data": "<base64>"             // base64 of a row-major Uint8Array,
                                   // length rows*steps; byte at [row*steps + stepIndex]
  },
  "error_groups": [                // one entry per (step, error type), worst first
    {
      "step": "web_search",
      "type": "RateLimitError",
      "count": 38,
      "message": "Rate limit reached for gpt-5.2-mini on tokens per minute (TPM): Limit 2,000,000, ...",
      "rows": [[1211, 1214], [1284, 1284]],  // inclusive row ranges, sorted
      "first_t": "2026-08-17T09:38:32Z",
      "last_t": "2026-08-17T09:41:29Z",
      "histogram": [0, 1, 6, 9, ...],        // ~22 equal time buckets first_t..last_t,
                                             // ints, sums to count
      "hint": "All 38 landed in a 3-minute burst — ..."  // string | null
    }
  ],
  "cost": {
    "by_step":  { "summarize": 2.61, "web_search": 1.37, "...": 0.0 },  // USD per step
    "by_model": { "claude-sonnet-5": 2.95, "gpt-5.2-mini": 1.88 },      // USD per model
    "plan": {                      // pipeline.plan() estimate; null when not planned
      "est_total": 6.10,
      "per_step": { "classify": 0.14, "...": 0.0 }
    },
    "tokens": {
      "input": 4102384,
      "output": 903117,
      "cache_read": 2914220,
      "cache_write": 388051
    },
    "wasted": 0.13,                // USD spent on cells that ultimately failed
    "batch_saved": 1.28            // USD saved by batch-mode pricing
  },
  "retry": {
    "available": false,            // POST /api/retry would work
    "reason": "launched without --pipeline",  // string | null (null when available)
    "resume_command": "accrue-ui .accrue/runs/2026-08-17a.jsonl --pipeline enrich:pipeline",
    "running": false,              // a retry is in flight right now
    "last_error": null             // string | null: why the last retry task died
  }
}
```

`resume_command` is the actual command line that reproduces this server with
retry enabled — including `--data` when the launch used it. When the server
was launched without `--pipeline` it carries the `<module:attr>` placeholder.

Invariants the server must keep:

- `stats.spend == sum(cost.by_step.values()) == sum(cost.by_model.values())`
  (rounding to cents allowed).
- `stats.errors == sum(g.count for g in error_groups) == sum(s.errors for s in steps)`.
- `cells.rows * cells.steps == len(decoded bytes)`; every byte is `0..6`.
- `steps` order matches `stepIndex` used by `cells` and SSE deltas.

## `GET /api/values?start=A&count=N`

Windowed row values for the data render mode and row labels. `start` is a
row index, `count` a row count; the server clamps to the log's bounds.

```jsonc
{
  "start": 1281,
  "rows": [
    {
      "row": 1281,
      "key": "stripe.com",         // display key for the row
      "cells": {
        "classify":   { "v": "fintech / payments", "s": 2 },
        "web_search": { "v": "Series I · $6.5B raised", "s": 3 },
        "summarize":  { "v": null, "s": 0 }
      }
    }
  ]
}
```

- `v` — short string preview of the cell's value (server-rendered, one line,
  already truncated), or `null` when there is nothing to show (pending,
  running, most skips). For error/retrying cells `v` MAY carry a short error
  preview (e.g. `"RateLimitError · 3 attempts"`).
- `s` — the cell-state byte, same encoding as `cells.data`.
- Reserved for v0.2: `&field.<step>=<name>` selects which output field the
  preview renders. v0.1 servers and the dev stub may ignore it (preview is
  the step's first non-internal field).

## `GET /api/cell/{step}/{row}`

One cell's full detail for the inspector.

```jsonc
{
  "step": "web_search",
  "row": 1284,
  "key": "vercel.com",
  "status": "error",               // "pending"|"running"|"ok"|"cached"|"retrying"|"error"|"skipped"
  "from_cache": false,
  "error": { "type": "RateLimitError", "msg": "Rate limit reached ..." },  // null unless failed
  "usage": { "in": 1204, "out": 0, "cost": 0.0031 },  // null when nothing was billed
  "elapsed_ms": 26100,             // queued -> terminal; null when not finished
  "queued_at": "2026-08-17T09:41:00Z",  // null when never queued
  "attempts": [                    // null when no attempt has started
    {
      "n": 1,                      // 1-based attempt number
      "kind": "live",              // "live" | "retry" | "batch"
      "at": "2026-08-17T09:41:02Z",
      "latency_ms": 1900,
      "status": "error",           // "ok" | "error"
      "backoff_s": 2               // sleep before next attempt; null on the final one
    }
  ],
  "values": { "funding": "..." },  // full output values; null unless ok/cached.
                                   // "__"-prefixed keys are internal fields.
  "raw_events": [ { "t": "...", "ev": "..." } ]  // original run-log JSONL records
                                   // for this cell, verbatim, in log order
}
```

## `GET /api/events` (SSE)

`text/event-stream` of coalesced deltas, event name `delta`, at most 10
events/second (the server accumulates changes and a coalescer flushes them
at most every 100ms, however fast the log grows). Each `data:` payload:

```jsonc
{
  "t": 402.6,                      // seconds since run start
  "cells": [[1283, 1, 2]],         // [row, stepIndex, newState] triples;
                                   // stepIndex = index into /api/run steps
  "stats": { "spend": 4.84, "rows_done": 3413 },  // ONLY the keys that changed
                                   // (subset of /api/run "stats", plus
                                   // "rows_done"); may be {}
  "steps": [                       // ONLY steps whose counters changed; may be []
    { "name": "web_search", "done": 4391, "errors": 38 }
  ]
}
```

`stats.rows_done` is the one delta-only key: it mirrors the snapshot's
`rows.done` (rows complete through the last step), which lives outside
`stats` in `/api/run` but rides along here so the progress tile tracks a
live run — or a retry healing cells — without refetching the snapshot.

Deltas are **state, not a journal**: a cell that changed several times
between flushes appears once with its latest state, and `stats`/`steps`
carry current values. A slow consumer is never queued unboundedly — its
unread payload is merged with the next flush (drop-and-coalesce), so
per-client memory is bounded by the grid size.

Clients merge deltas into the `/api/run` snapshot. **Connect race:** a delta
published between a client's `/api/run` fetch and its EventSource attaching
is not replayed — the stream continues from live state. A client that
detects a gap (a delta referencing rows or steps outside its known grid)
should re-fetch `/api/run`; payloads are idempotent state, so re-applying
deltas after the refetch is safe.

Keepalive comment lines (`: ...`) may appear at any time; the stream opens
with a `: connected` comment. `tests/fixtures/api/events_sample.ndjson`
holds sample payloads, one JSON payload per line (the dev stub replays them
as `delta` events).

## `POST /api/retry`

Re-runs failed cells with `Pipeline.retry_failed_async()` and appends the
result to the log being served (accrue writes a `retry_start` … `retry_end`
segment), so healed cells arrive through `/api/events` like any other
records. Same-origin POST: the launch-token cookie authenticates it, no
header needed. The body carries **exactly one** selector:

```jsonc
{ "rows": [1284, 1290] }                              // explicit row indices
{ "group": { "step": "web_search", "type": "RateLimitError" } }  // one error group
{ "all": true }                                       // every errored row
```

A `group` selector is resolved against the snapshot's `error_groups` and also
restricts the retry to that step; `rows` and `all` retry every failed step of
the rows named.

| Status | Body | When |
|--------|------|------|
| 202 | `{"accepted": 12}` | Accepted; 12 rows are being retried in the background |
| 400 | `{"detail": "..."}` | Malformed body, no selector (or more than one), or nothing failed |
| 401 / 403 | `{"detail": "..."}` | Missing token / non-loopback Origin (see `security.py`) |
| 404 | `{"detail": "no error group ..."}` | The named group is not in the index (it may have healed already) |
| 405 | `{"detail": "Method Not Allowed"}` | Mutations are POST-only |
| 409 | the `retry` block, `reason` saying why | Retry unavailable, or `"retry already running"` |

Only one retry runs at a time. While it does, `/api/run`'s `retry.running` is
`true`; when the task itself fails (bad checkpoint, pipeline raised), the
reason lands in `retry.last_error` and stays there until the next retry
starts.

## `GET /api/runs`

Known run logs, newest first.

```jsonc
{
  "runs": [
    {
      "id": "2026-08-17a",
      "name": "acme-enrichment",
      "path": ".accrue/runs/2026-08-17a.jsonl",
      "started_at": "2026-08-17T09:35:12Z",
      "live": true
    }
  ]
}
```
