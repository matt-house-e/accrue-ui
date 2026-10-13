# accrue-ui

Run-observability dashboard for [accrue](https://github.com/matt-house-e/accrue)
enrichment pipelines: watch a run live cell-by-cell, inspect any cell's prompt
and response, triage failures, and track cost — all from the JSONL run log
accrue emits.

**Status: early release.** 0.1.0 is the first published version; the dashboard
is built issue-by-issue, so expect gaps — see
[the issues](https://github.com/matt-house-e/accrue-ui/issues).

## Install

To look at a run log without installing anything, use
[uv](https://docs.astral.sh/uv/):

```bash
uvx --from accrue-ui accrue-ui .accrue/runs/2026-08-20a.jsonl
```

`uvx` runs in a throwaway environment that can't import your pipeline, so
for [retries](#retrying-failed-rows), or to keep it around, install it in
the environment your pipeline runs in:

```bash
pip install accrue-ui
```

Then point it at a run log:

```bash
accrue-ui .accrue/runs/2026-08-20a.jsonl
```

accrue 1.4 and later ship an `accrue watch` handoff that opens the same
dashboard:

```bash
accrue watch .accrue/runs/2026-08-20a.jsonl
```

Requires Python 3.10+ and a run log — write one with
`pipeline.run(..., run_log=True)`.

## Retrying failed rows

Point the dashboard at the pipeline that produced the run and the triage
tab's retry buttons go live — one click re-runs just the failed cells
(everything else is served from the run's checkpoint) and the grid heals in
place:

```bash
# attr is a zero-arg callable returning (pipeline, data)
accrue-ui .accrue/runs/2026-08-20a.jsonl --pipeline enrich:target

# ...or the Pipeline itself, with the data alongside it
accrue-ui .accrue/runs/2026-08-20a.jsonl --pipeline enrich:pipeline --data enrich:rows
```

Run it from the same directory the pipeline ran in, so the retry resolves
the same checkpoint. Without `--pipeline` the buttons stay disabled and say
why.

## Prices

accrue's run log records tokens but no dollars, so the dashboard prices them
from a built-in table of list prices: current Claude models at Anthropic's
API rates, plus a few others. If a model isn't in the table, the dashboard
shows "no price for <model>" where the figure would go. To supply a price,
or to override one (a negotiated rate, a gateway markup), pass a TOML file
of USD per 1M tokens:

```toml
# prices.toml — quote each model id
["claude-sonnet-4-6"]
input = 3.00
output = 15.00
cache_read = 0.30    # optional
cache_write = 3.75   # optional; 5-minute cache writes

["house-llm-2"]      # not built in: input and output are required
input = 0.80
output = 3.20
```

```bash
accrue-ui .accrue/runs/2026-08-20a.jsonl --prices prices.toml
```

A field you leave out keeps its built-in value. If a file can't be used,
accrue-ui exits before it starts serving and says what is wrong.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

## No-build frontend

The frontend (`accrue_ui/static/`) is plain ES modules served as-is:
Preact + HTM + signals, vendored and pinned under
`accrue_ui/static/vendor/` (see `VERSIONS.md` there). There is no build
step, no node toolchain, and no npm — ever. See `CLAUDE.md` for the full
conventions.

## Related

- [accrue](https://github.com/matt-house-e/accrue) — the pipeline engine
  whose run log this dashboard consumes.
