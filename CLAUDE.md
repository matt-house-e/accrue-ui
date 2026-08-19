# accrue-ui

Run-observability dashboard for [accrue](https://github.com/matt-house-e/accrue)
enrichment pipelines. Local, single-user, private-for-now.

**The contract: accrue core emits a JSONL run log (schema v1 — see accrue's
`docs/guides/run-log.md` once merged); this repo is a consumer. "The run log
is the API."** Never reach into accrue internals; everything the UI shows is
derived from the log (plus the small retry surface below).

## Commands

```bash
pip install -e ".[dev]"   # Dev install
pytest                    # Run tests
ruff check .              # Lint (CI enforces both)
accrue-ui [run_log] [--pipeline mod:attr] [--port 7607] [--no-browser]
```

## Architecture

Backend lives in `accrue_ui/server/`:

- **`app.py`** — FastAPI app factory + token & Origin middleware.
- **`routes.py`** —
  - `GET /api/run` — snapshot, including the full cell-state array
  - `GET /api/cell/{step}/{row}` — one cell's detail (prompt, response, error, cost)
  - `GET /api/values?rows=a-b` — windowed row values
  - `GET /api/events` — SSE stream of coalesced deltas, ≤10Hz
  - `POST /api/retry` — retry failed rows (requires `--pipeline`)
  - `GET /api/runs` — list known run logs
- **`tail.py`** — poll follower: `stat` every ~250ms + read appended bytes,
  partial-line safe. Polling on purpose — **no inotify, it breaks on WSL2**.
- **`index.py`** — in-memory run index built from the log → snapshot + deltas.
  Cell state fits one byte: `0` pending, `1` running, `2` ok, `3` cached,
  `4` retrying, `5` error, `6` skipped.
- **`retry.py`** — retry orchestration.
- **`security.py`** — bind `127.0.0.1` only, launch token, Origin/Host
  checks, mutations POST-only.

## Frontend rules

`accrue_ui/static/` is served **as-is**. **NO build step, NO node toolchain,
NO npm — ever.** Preact + HTM + signals via an import map pointing at
`vendor/` (pinned copies, versions in `vendor/VERSIONS.md`; never load from a
CDN at runtime).

Views:

- `grid.js` — the run grid; status and data render modes; hand-rolled row
  virtualization (no virtualization library).
- `inspector.js` — cell detail panel.
- `triage.js` — failure triage.
- `cost.js` — cost breakdown.
- `lib/store.js` — signals-based state store.
- `lib/sse.js` — SSE client, merges deltas into the store.

Charts are CSS only — no chart libraries.

## Design tokens

Dark theme, Radix scales.

| Role | Value |
|------|-------|
| Ground | `#111210` |
| Surface | `#181917` |
| Component | `#212220` |
| Hover | `#282a27` |
| Borders | `#383a36` / `#454843` |
| Faint text | `#687066` / `#767d74` |
| Muted text | `#afb5ad` |
| Ink | `#eceeec` |
| Jade accent (solid/buttons, white text) | `#29a383` |
| Jade accent (text/icons) | `#1fd8a4` |
| Done-cell tint | `#0f2e22` |
| Cached (bg/text) | `#291f43` / `#baa7ff` |
| Retry amber (bg/text) | `#302008` / `#ffca16` |
| Failed red (bg/text, solid) | `#3b1219` / `#ff9592`, solid `#e5484d` |
| Pending | `#212220` |

Fonts: **'IBM Plex Sans'** for UI labels/buttons/tooltips, **'IBM Plex Mono'**
for all data/numbers/IDs/code (Google Fonts link + system fallbacks).

Conventions: 8pt spacing grid; sentence-case labels; 11px font floor;
dotted underline = hover tooltip.

## Git

- Feature branches: `feature/<slug>`, PRs to `main`.
- Conventional commits: `type: Brief description` with
  `Co-Authored-By: Claude <noreply@anthropic.com>`.
- `ruff check .` and `pytest` must pass before merge.
