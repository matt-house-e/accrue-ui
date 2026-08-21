# API shapes (v0.2 contract)

The frontend consumes these routes; the server (issue #1/#3) implements them.
This document is the contract — both lanes code to it. v0.2 is additive over
v0.1: the run log's new `row_attempt` records give `/api/cell` a real
per-attempt timeline and captured prompt/response bodies (issue #14); every
v0.1 field is unchanged, and a v0.1 log still reads correctly. All routes are
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

State 4 (retrying) has two real sources, never an inference from preview text:
a `retry_start` record (a `retry_failed()` segment names the cells it will
re-run), and — new in v0.2 — a **failed `row_attempt`** during the initial
pass. A `row_attempt` whose `status` is not `ok` means a try failed and
another (or a terminal `row_complete`) is coming, so a not-yet-settled cell
shows as retrying in the interim. Either way it holds until the cell's own
`row_complete` arrives. While a cell is retrying it is **not** counted
anywhere as settled: it leaves `steps[].done`, `stats.errors`, its error group
and `rows.done`, and rejoins them with whatever its next `row_complete`
produces. (A `retry_start` cell was already terminal, so its prior tally is
unwound then; an initial-pass cell was only ever pending, so there is nothing
to unwind.) States 0 and 1 come from the emitter; the v1 log has no per-row
start event, so a step in progress leaves its unfinished cells pending (0).

## `GET /api/run`

Full snapshot of the run, including the complete cell-state array.

```jsonc
{
  "run": {
    "id": "2026-08-17a",           // run identifier (log-derived)
    "name": "acme-enrichment",     // log-derived name (the file's stem)
    "started_at": "2026-08-17T09:35:12Z",  // string | null: absent or
                                   // unparseable pipeline_start.started_at
    "live": true,                  // log was written to in the last few
                                   // seconds — mtime recency ONLY
    "elapsed_s": 401,              // live: seconds since started_at.
                                   // finished: pipeline_end's elapsed_s.
                                   // interrupted and cold: the log's own
                                   // span (its last `t`), never a clock
                                   // that keeps climbing for a dead run
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
    "spend": 4.83,                 // number | null: USD spent so far, null
                                   // when nothing in the run could be priced
    "cache_hit_rate": 0.72,        // 0..1
    "errors": 43,                  // total errored cells
    "throughput_per_min": 248,     // number | null: completed cells/min over
                                   // the recent window. null until the log
                                   // holds at least 5s, below which the
                                   // divisor makes the rate meaningless
    "eta_s": 384,                  // estimated seconds remaining; null when
                                   // there is no throughput to derive it from
    "cache_saved": 1.94            // number | null, same rule as spend
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
      "first_t": "2026-08-17T09:38:32Z",     // string | null: null when the
      "last_t": "2026-08-17T09:41:29Z",      // run has no parseable start
                                             // time to offset log `t` from
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
    "wasted": 0.13,                // number | null: USD spent on cells that
                                   // ultimately failed
    "batch_saved": 1.28            // number | null: USD saved by batch pricing
  },
  "retry": {
    "available": false,            // POST /api/retry would work
    "reason": "launched without --pipeline",  // string | null (null when available)
    "resume_command": "accrue-ui .accrue/runs/2026-08-17a.jsonl --pipeline enrich:pipeline",
    "running": false,              // a retry is in flight right now
    "last_error": null             // string | null: why the last retry task died
  },
  "overview": {                    // pipeline blueprint (see below); a log with
    "present": false               // no manifest carries ONLY {"present": false}
  }
}
```

`resume_command` is the actual command line that reproduces this server with
retry enabled — including `--data` when the launch used it. When the server
was launched without `--pipeline` it carries the `<module:attr>` placeholder.

The four dollar figures — `stats.spend`, `stats.cache_saved`, `cost.wasted`,
`cost.batch_saved` — are `number | null` **together**: a run whose steps have
no model (function steps) or only unknown models cannot be priced at all, and
the server reports null rather than a misleading `0`. `cost.by_step` /
`cost.by_model` then hold only the steps that *could* be priced, and are
`{}` when none could. Clients must render null as an em-dash, not as blank
and not as `$0.00`.

Invariants the server must keep:

- **When `stats.spend` is non-null:** `stats.spend ==
  sum(cost.by_step.values()) == sum(cost.by_model.values())` (rounding to
  cents allowed). When it is null, both maps are empty and the invariant
  does not apply.
- `stats.errors == sum(g.count for g in error_groups) == sum(s.errors for s in steps)`.
- `cells.rows * cells.steps == len(decoded bytes)`; every byte is `0..6`.
- `steps` order matches `stepIndex` used by `cells` and SSE deltas.
- Row indices in the log outside `0..rows.total-1` are ignored, never
  grown into: a corrupt `row` cannot resize the grid. Without a declared
  `num_rows`, indices at or above 1,000,000 are treated as corrupt.

### `overview` — the pipeline blueprint

The Overview view (accrue-ui #19) renders the run's *definition* — steps,
types, models, params, produced fields, and the enrichment-field schema — from
the run log's `pipeline_start.manifest` (accrue's introspection of the pipeline
at run start; contract: `docs/guides/run-log.md`). It is **read-only**:
accrue-ui observes a run, it never configures the pipeline. The block rides
`/api/run` (no separate route), so the same launch-token + Origin/Host
middleware guards it.

A log **without** a manifest — older runs, or a metadata capture tier that
predates it — carries exactly `{"present": false}`; the view then degrades to
what `steps[]` alone gives rather than crashing. When present:

```jsonc
"overview": {
  "present": true,
  "accrue_version": "1.3.0",       // string | null
  "config": {                      // verbatim manifest.config; {} if absent
    "max_workers": 6,
    "caching": false,
    "checkpointing": true,
    "batch": false,
    "capture": "prompts"           // "metadata" | "prompts" | "full"
  },
  "sample_size": 12,               // pipeline_start.num_rows (declared row count)
  "pipeline_wall_s": 4.72,         // number | null: total pipeline wall-clock
                                   // (pipeline_end.elapsed_s); null until the
                                   // run ends — the view falls back to
                                   // run.elapsed_s for a live run
  "providers": ["openrouter"],     // distinct step-model providers, sorted; a
                                   // FunctionStep's null model contributes none
  "steps": [                       // pipeline order (== /api/run steps order)
    {
      "name": "assess",
      "type": "LLMStep",           // "unknown" if accrue could not introspect it
      "model": {                   // null for FunctionSteps
        "id": "google/gemini-3.5-flash-lite",
        "provider": "openrouter",
        "temperature": 0.2,
        "max_tokens": 4000
      },
      "system_prompt": "You are ...",  // string | null: the step's row-
                                   // independent system prompt (the exact
                                   // cached prefix, #107), secret-redacted by
                                   // accrue. null for FunctionSteps / steps
                                   // without one (accrue manifest #140)
      "produces": ["one_liner", "icp_fit"],  // output field names
      "depends_on": ["classify"],  // upstream step names (the DAG edges)
      "condition": null,           // run-if expression, or null
      "level": 1,                  // dependency level, from the live step
      "mode": "live",              // "live" | "batch", from the live step
      "outcome": {                 // live annotation, by step name
        "done": 12,                // settled cells (ok+cached+error+skipped)
        "total": 12,
        "errors": 12,
        "cost": 0.0027,            // number | null, same rule/value as cost.by_step
        "wall_s": 1.66,            // number | null: step wall-clock
                                   // (step_end.elapsed_s, max over segments);
                                   // null until the step ends
        "ended": true,             // step_end seen — false => in progress, and
                                   // the card shows a running state, not a
                                   // final duration
        "latency_ms": {            // per-row latency, cached rows EXCLUDED
                                   // (cached cells settle in ~0ms). null for
                                   // batch steps (their elapsed_ms includes
                                   // provider queue time) and steps with no
                                   // timed, non-cached row yet
          "p50": 632.9,
          "p95": 887.3,
          "n": 12                  // sample count behind the percentiles
        },
        "retry": {                 // object | null: null when the step never
                                   // retried (count 0), so the card omits the
                                   // chip. Emitted at every capture tier —
                                   // row_attempt lands even at capture=metadata
          "count": 47,             // row_attempt records with attempt > 1
          "by_status": {           // FAILED attempts (status != "ok") by
                                   // status, descending — the retry reasons
            "rate_limited": 41,
            "timeout": 6
          },
          "by_kind": { "api": 47 },  // same failed attempts by kind (api/parse)
          "dominant": {            // the top by_status bucket, for the chip
            "status": "rate_limited",
            "count": 41
          }
        }
      }
    }
  ],
  "fields": [                      // the enrichment-field schema
    {
      "name": "icp_fit",
      "type": "enum",              // "str" | "int" | "enum" | ... | "unknown"
      "enum": ["strong", "good", "weak"],  // list | null (only for enum types)
      "description": "Fit as a customer for a developer-tools startup.",  // string | null
      "step": "assess",            // the producing step
      "internal": false            // true for "__"-prefixed inter-step fields
    }
  ]
}
```

- `steps[].outcome` is looked up by name in the same per-step state that backs
  the top-level `steps[]`, so the blueprint and the running numbers never
  diverge: `outcome.done/total/errors` equal the matching `steps[]` entry, and
  `outcome.cost` equals `cost.by_step[name]` (null when the step's model cannot
  be priced — an em-dash, never `$0.00`).
- `type` is `"unknown"` for a step or field accrue could not introspect; render
  it plainly rather than hiding it.
- `sample_size` is the declared `num_rows`, falling back to the widest row
  index seen if the log never declared one.
- `outcome.latency_ms` percentiles are computed over the same per-row
  `elapsed_ms` the inspector shows, with `from_cache` rows dropped, so the
  numbers reconcile with the per-cell timelines. `outcome.retry.count` equals
  the number of `attempt > 1` records across the step's cells — the same
  attempt timelines `/api/cell` renders — and `by_status`/`by_kind` tally the
  failed (`status != "ok"`) attempts. Both are additive over the v0.2 shape:
  an older log with no `row_attempt`/`step_end` simply reports `retry: null`
  and `latency_ms: null`.

## `GET /api/values?start=A&count=N`

Windowed row values for the data render mode and row labels. `start` is a
row index, `count` a row count; the server clamps to the log's bounds **and
to 1000 rows per request**. A client whose visible window spans more than
1000 row indices (a sparse filter over a large run) must therefore page —
asking for the whole span returns a short answer, and re-asking for the same
over-wide window is a refetch loop, not a retry.

```jsonc
{
  "start": 1281,
  "rows": [
    {
      "row": 1281,
      "key": "stripe.com",         // display key for the row
      "cells": {
        "classify":   {
          "v": "fintech / payments",
          "f": { "category": "fintech / payments", "hq_country": "US" },
          "s": 2
        },
        "web_search": { "v": "Series I · $6.5B raised", "f": { "summary": "Series I · $6.5B raised" }, "s": 3 },
        "summarize":  { "v": null, "f": null, "s": 0 }
      }
    }
  ]
}
```

- `v` — short string preview of the cell's value (server-rendered, one line,
  already truncated), or `null` when there is nothing to show (pending,
  running, most skips). For error/retrying cells `v` MAY carry a short error
  preview (e.g. `"RateLimitError · 3 attempts"`). Kept for back-compat; it is
  always `f`'s first entry when `f` is non-null.
- `f` — field name -> rendered preview string, one entry per non-internal
  field the step produced (same truncation rules as `v`), or `null` for
  cells with nothing to render (pending/running/error/retrying/skipped, or a
  step with no values yet). This is what lets the data grid's field-chip
  actually switch the rendered value, not just the column label
  (accrue-ui#23) — a client must read the chosen field out of `f`, not
  assume `v` tracks the selection.
- `s` — the cell-state byte, same encoding as `cells.data`.

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
  "attempts": [                    // null when no attempt ran — pending,
                                   // skipped, or served from cache. Capped at
                                   // the newest 50. TWO shapes, by log tier:
    // v0.2 (log has row_attempt records): one entry per real try, in attempt
    // order. `kind`/`status`/`backoff_s`/`error`/`prompt_ref` are additive
    // over the v0.1 shape below.
    {
      "n": 1,                      // 1-based display index (dense, 1..N)
      "attempt": 1,                // the log's own attempt number
      "kind": "api",               // "api" | "parse": the retry loop this try
                                   // ran in (an api call vs. parsing/validating
                                   // its result)
      "at": "2026-08-17T09:41:02Z", // string | null (needs a run start time)
      "latency_ms": 1900,          // number | null
      "status": "rate_limited",    // "ok" | "rate_limited" | "timeout" |
                                   // "api_error" | "parse_error" |
                                   // "validation_error"
      "backoff_s": 2.0,            // sleep before the next try; null when none
      "error": { "type": "LLMAPIError", "msg": "..." },  // null on success
      "prompt_ref": { "off": 1276, "len": 1283 }  // byte span in the sidecar,
                                   // or null (api-error tries and every try of
                                   // a metadata-tier run carry no body)
    }
    // v0.1 (no row_attempt records): synthesized one-per-row_complete, keys
    // { "n", "kind": "live"|"retry"|"batch", "at", "latency_ms",
    //   "status": "ok"|"error", "backoff_s": null }.
  ],
  "prompt": {                      // captured request/response for the cell's
                                   // last body-bearing attempt; null when the
                                   // run captured no bodies or this cell had
                                   // none. Already secret-redacted by accrue.
    "messages": [{ "role": "system", "content": "..." }],
    "response": "{\"grade\": \"B\"}",  // string | object (the raw model output)
    "parsed": { "grade": "B" }     // object | null (the parsed result)
  },
  "capture_available": true,       // did THIS run capture prompt bodies (does
                                   // the <run>.prompts.jsonl sidecar exist)?
                                   // false => the inspector shows a "re-run
                                   // with capture=\"prompts\"" hint instead of
                                   // an empty prompt pane
  "values": { "funding": "..." },  // full output values; null unless ok/cached.
                                   // "__"-prefixed keys are internal fields.
  "raw_events": [ { "t": "...", "type": "..." } ]  // original run-log JSONL
                                   // records for this cell (row_attempt and
                                   // row_complete), verbatim, in log order
}
```

`prompt` is resolved by **seeking the sidecar** (`<run_id>.prompts.jsonl`,
beside the main log) to the recorded byte offset and reading exactly `len`
bytes — the whole capture is never loaded, so a 500MB sidecar costs one seek
per inspected cell. `prompt_ref` on each attempt is that byte span; the server
resolves only the last one that has a body (the try that settled the cell) into
`prompt`.

## `GET /api/events` (SSE)

`text/event-stream` of coalesced deltas, event name `delta`, at most 10
events/second (the server accumulates changes and a coalescer flushes them
at most every 100ms, however fast the log grows). Each `data:` payload:

```jsonc
{
  "t": 402.6,                      // seconds since run start
  "cells": [[1283, 1, 2]],         // [row, stepIndex, newState] triples;
                                   // stepIndex = index into /api/run steps
  "stats": { "spend": 4.84, "rows_done": 3413, "live": true },  // ONLY the
                                   // keys that changed (subset of /api/run
                                   // "stats", plus "rows_done" and "live");
                                   // may be {}
  "steps": [                       // ONLY steps whose counters changed; may be []
    { "name": "web_search", "done": 4391, "errors": 38 }
  ],
  "reset": true                    // OPTIONAL, absent = false. The server
                                   // rebuilt its index (truncate-and-regrow);
                                   // see below.
}
```

`stats.rows_done` and `stats.live` are the two delta-only keys, each mirroring
a field that lives outside `stats` in `/api/run` so it can ride the stream:

- `stats.rows_done` mirrors the snapshot's `rows.done` (rows complete through
  the last step) so the progress tile tracks a live run — or a retry healing
  cells — without refetching the snapshot.
- `stats.live` mirrors `run.live` (mtime recency). It is included only when it
  changes, so a run that **finishes while the page is open** emits one delta
  carrying `"live": false` a few seconds after its last write — the client
  clears the LIVE badge and stops the elapsed ticker without refetching.
  Backward-compatible: a consumer that ignores it simply keeps its snapshot's
  `run.live`.

`reset` is an **optional** boolean, absent (i.e. false) on every ordinary
delta. The server sets it on the first delta published after it rebuilds its
in-memory index — which happens when the follower detects the log was
**truncated and regrown** (a new run written over the same path; the tail's
generation bumps). After such a rebuild, cells that existed only in the old
file are still *in-grid*, so a client cannot gap-detect them (a gap is an
out-of-grid row/step index). A `reset` delta tells the client its whole
snapshot is stale: it must **refetch `/api/run` wholesale**, not just
gap-refetch. The flag survives delta coalescing (a slow client's merged
payload keeps `reset` if either half set it). Older clients that do not know
the key fall back to their existing gap-refetch heuristic.

Deltas are **state, not a journal**: a cell that changed several times
between flushes appears once with its latest state, and `stats`/`steps`
carry current values. A slow consumer is never queued unboundedly — its
unread payload is merged with the next flush (drop-and-coalesce), so
per-client memory is bounded by the grid size.

Clients merge deltas into the `/api/run` snapshot. **Connect race:** a delta
published between a client's `/api/run` fetch and its EventSource attaching
is not replayed — the stream continues from live state. A client that
detects a gap (a delta referencing rows or steps outside its known grid) —
**or receives a `reset` delta** — should re-fetch `/api/run`; payloads are
idempotent state, so re-applying deltas after the refetch is safe.

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

`Content-Type: application/json` is **required** on every `/api/*` mutation.
Without it the request would be a CORS "simple request" — no preflight, and
the launch-token cookie attached automatically — so the server refuses it
with 415 before the route sees it (`security.py`).

The Origin check compares the **whole origin**, `scheme://host:port`, against
the server's own. `http://127.0.0.1:7607` and `http://localhost:7607` are the
same origin as far as it is concerned; `http://127.0.0.1:31337` is a
different site running on the same loopback interface, and is rejected.

| Status | Body | When |
|--------|------|------|
| 202 | `{"accepted": 12}` | Accepted; 12 rows are being retried in the background |
| 400 | `{"detail": "..."}` | Malformed body, no selector (or more than one), a row outside `0..rows.total-1`, or nothing failed |
| 401 / 403 | `{"detail": "..."}` | Missing token / an Origin that is not this server's (see `security.py`) |
| 404 | `{"detail": "no error group ..."}` | The named group is not in the index (it may have healed already) |
| 405 | `{"detail": "Method Not Allowed"}` | Mutations are POST-only |
| 409 | the `retry` block, `reason` saying why | Retry unavailable, or `"retry already running"` (the block is read fresh, so `running` is `true` alongside that reason) |
| 415 | `{"detail": "expected Content-Type: application/json"}` | The body was not declared as JSON |

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
      "id": "2026-08-17a",         // run id from pipeline_start, else the stem
      "name": "acme-enrichment",   // log-derived: the file's stem
      "path": ".accrue/runs/2026-08-17a.jsonl",
      "started_at": "2026-08-17T09:35:12Z",  // string | null
      "live": true                 // mtime recency, same rule as run.live
    }
  ]
}
```

`name` and `id` are frequently the same string (a log named after its run).
Clients should show `name / id` only when they differ, and the id alone
otherwise.

## `GET /api/report`

A **self-contained static HTML report** of the served run, for handing a
stakeholder a shareable artifact. Not JSON: the response is
`text/html; charset=utf-8` with

```
Content-Disposition: attachment; filename="<run_id>.html"
```

so a browser saves it rather than navigating. The toolbar's "Export report"
button triggers the download with a same-origin GET (the launch-token cookie
authenticates it — this route sits under `/api/*`, so the same security
middleware guards it as every other API route).

The document is rendered server-side from the same in-memory index that backs
`/api/run` (steps, cell states, per-step and total cost, error groups,
timings) — no new accrue core release is involved. It is **fully offline**:
one file, all CSS inlined, all data embedded in the markup, **no external
references** (no CDN scripts, no Google Fonts link, no remote images) and no
JavaScript, so it opens from `file://` with no server and is CSP-clean. It
reflects the run at the moment of export; it does not update.
