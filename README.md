# accrue-ui

Run-observability dashboard for [accrue](https://github.com/matt-house-e/accrue)
enrichment pipelines: watch a run live cell-by-cell, inspect any cell's prompt
and response, triage failures, and track cost — all from the JSONL run log
accrue emits.

**Status: under construction** — built issue-by-issue, see
[the issues](https://github.com/matt-house-e/accrue-ui/issues).

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
