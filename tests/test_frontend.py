"""Frontend gate: the dev stub serves the app end-to-end and the fixtures
conform to docs/api-shapes.md (shapes, cell-state bytes, design window)."""

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "api"
STATIC = ROOT / "accrue_ui" / "static"

STATIC_FILES = [
    "index.html",
    "style.css",
    "app.js",
    "lib/html.js",
    "lib/fmt.js",
    "lib/icons.js",
    "lib/store.js",
    "lib/sse.js",
    "lib/tooltip.js",
    "lib/popover.js",
    "views/columns.js",
    "views/filters.js",
    "views/grid.js",
    "views/overview.js",
    "views/inspector.js",
    "views/triage.js",
    "views/cost.js",
    "vendor/preact.module.js",
    "vendor/hooks.module.js",
    "vendor/signals.module.js",
    "vendor/signals-core.module.js",
    "vendor/htm.module.js",
]


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_stub_cells", FIXTURES / "generate_stub_cells.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get(url: str, timeout: float = 5.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        assert resp.status == 200
        return resp.read()


@pytest.fixture(scope="module")
def stub_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "devstub.py"), "--port", str(port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                _get(url + "/api/run", timeout=0.5)
                break
            except (urllib.error.URLError, OSError):
                if proc.poll() is not None:
                    pytest.fail("devstub exited early")
                time.sleep(0.1)
        else:
            pytest.fail("devstub did not start")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ---- fixtures conform to docs/api-shapes.md -----------------------------


def _run_doc():
    return json.loads((FIXTURES / "run.json").read_text())


def _cells_bytes(doc):
    cells = doc["cells"]
    data = base64.b64decode(cells["data"])
    assert len(data) == cells["rows"] * cells["steps"]
    return data, cells["steps"]


def test_run_fixture_shape():
    doc = _run_doc()
    assert set(doc) == {
        "run",
        "steps",
        "rows",
        "stats",
        "cells",
        "error_groups",
        "cost",
        "retry",
    }
    assert set(doc["run"]) == {
        "id",
        "name",
        "started_at",
        "live",
        "elapsed_s",
        "schema_v",
    }
    step_keys = {"name", "level", "mode", "model", "done", "total", "errors"}
    for step in doc["steps"]:
        assert step_keys <= set(step)
        assert step["mode"] in ("live", "batch")
    assert set(doc["rows"]) == {"total", "done"}
    assert set(doc["stats"]) == {
        "spend",
        "cache_hit_rate",
        "errors",
        "throughput_per_min",
        "eta_s",
        "cache_saved",
    }
    assert set(doc["cost"]) == {
        "by_step",
        "by_model",
        "plan",
        "tokens",
        "wasted",
        "batch_saved",
    }
    assert set(doc["cost"]["tokens"]) == {
        "input",
        "output",
        "cache_read",
        "cache_write",
    }
    assert set(doc["retry"]) == {
        "available",
        "reason",
        "resume_command",
        "running",
        "last_error",
    }
    for group in doc["error_groups"]:
        assert set(group) == {
            "step",
            "type",
            "key",
            "count",
            "message",
            "retryable",
            "rows",
            "first_t",
            "last_t",
            "histogram",
            "hint",
        }


def test_run_fixture_invariants():
    doc = _run_doc()
    spend = doc["stats"]["spend"]
    # The sum invariant is scoped to a priced run: an unpriceable one reports
    # null spend and empty maps (docs/api-shapes.md, and test_contract.py's
    # test_stats_and_cost_with_no_pricing).
    assert spend is not None
    assert round(sum(doc["cost"]["by_step"].values()), 2) == spend
    assert round(sum(doc["cost"]["by_model"].values()), 2) == spend
    assert sum(g["count"] for g in doc["error_groups"]) == doc["stats"]["errors"]
    assert sum(s["errors"] for s in doc["steps"]) == doc["stats"]["errors"]
    plan = doc["cost"]["plan"]
    assert round(sum(plan["per_step"].values()), 2) == plan["est_total"]
    for group in doc["error_groups"]:
        n_rows = sum(hi - lo + 1 for lo, hi in group["rows"])
        assert n_rows == group["count"]
        assert sum(group["histogram"]) == group["count"]
    # by_model is consistent with the models on the steps
    by_model = {}
    for step in doc["steps"]:
        cost = doc["cost"]["by_step"][step["name"]]
        by_model[step["model"]] = round(by_model.get(step["model"], 0) + cost, 2)
    assert by_model == doc["cost"]["by_model"]


def test_cells_decode_and_range():
    doc = _run_doc()
    data, steps = _cells_bytes(doc)
    assert steps == len(doc["steps"])
    assert set(data) <= set(range(7))


def test_cells_window_matches_design():
    """Rows 1281-1292 mirror the approved design mock exactly."""
    doc = _run_doc()
    data, steps = _cells_bytes(doc)
    gen = _load_generator()
    for row, expected in gen.WINDOW.items():
        actual = list(data[row * steps : (row + 1) * steps])
        assert actual == expected, f"row {row}: {actual} != {expected}"
    web = [s["name"] for s in doc["steps"]].index("web_search")
    assert data[1284 * steps + web] == 5  # the design's error cell
    assert data[1283 * steps + web] == 4  # the design's retrying cell


def test_generator_is_deterministic(tmp_path):
    src = FIXTURES / "run.json"
    copy = tmp_path / "run.json"
    shutil.copy(src, copy)
    subprocess.run(
        [sys.executable, str(FIXTURES / "generate_stub_cells.py"), str(copy)],
        check=True,
        capture_output=True,
    )
    assert copy.read_text() == src.read_text()


def test_values_fixture_shape():
    doc = json.loads((FIXTURES / "values_1281.json").read_text())
    assert set(doc) == {"start", "rows"}
    assert doc["start"] == 1281
    assert [r["row"] for r in doc["rows"]] == list(range(1281, 1293))
    run = _run_doc()
    data, steps = _cells_bytes(run)
    step_names = [s["name"] for s in run["steps"]]
    for row in doc["rows"]:
        assert set(row) == {"row", "key", "cells"}
        assert set(row["cells"]) == set(step_names)
        for name, cell in row["cells"].items():
            assert set(cell) == {"v", "s"}
            assert cell["v"] is None or isinstance(cell["v"], str)
            # per-cell state agrees with the cell-state array
            si = step_names.index(name)
            assert cell["s"] == data[row["row"] * steps + si]


def test_cell_fixture_shape():
    doc = json.loads((FIXTURES / "cell_web_search_1284.json").read_text())
    assert set(doc) == {
        "step",
        "row",
        "key",
        "status",
        "from_cache",
        "error",
        "usage",
        "elapsed_ms",
        "queued_at",
        "attempts",
        "values",
        "raw_events",
    }
    assert doc["step"] == "web_search"
    assert doc["row"] == 1284
    assert doc["status"] == "error"
    assert set(doc["error"]) == {"type", "msg"}
    assert set(doc["usage"]) == {"in", "out", "cache_read", "cache_write", "cost"}
    assert len(doc["attempts"]) == 3
    for attempt in doc["attempts"]:
        assert set(attempt) == {"n", "kind", "at", "latency_ms", "status", "backoff_s"}
    assert [a["latency_ms"] for a in doc["attempts"]] == [1900, 4200, 8100]
    assert [a["backoff_s"] for a in doc["attempts"]] == [2, 8, None]
    assert doc["raw_events"], "raw_events must carry the original jsonl records"


def test_runs_fixture_shape():
    doc = json.loads((FIXTURES / "runs.json").read_text())
    assert set(doc) == {"runs"}
    for run in doc["runs"]:
        assert set(run) == {"id", "name", "path", "started_at", "live"}
    assert doc["runs"][0]["live"] is True


def test_events_fixture_shape():
    lines = (FIXTURES / "events_sample.ndjson").read_text().splitlines()
    assert lines
    run = _run_doc()
    nsteps = len(run["steps"])
    step_names = {s["name"] for s in run["steps"]}
    stats_keys = set(run["stats"])
    for line in lines:
        delta = json.loads(line)
        assert set(delta) == {"t", "cells", "stats", "steps"}
        for cell in delta["cells"]:
            _row, step_index, state = cell
            assert 0 <= step_index < nsteps
            assert 0 <= state <= 6
        # rows_done and live are the delta-only stats keys (docs/api-shapes.md).
        assert set(delta["stats"]) <= stats_keys | {"rows_done", "live"}
        for step in delta["steps"]:
            assert set(step) == {"name", "done", "errors"}
            assert step["name"] in step_names


def test_retry_buttons_are_wired_to_the_endpoint():
    """No JS runtime here (no node, ever) — pin the wiring at source level."""
    store = (STATIC / "lib" / "store.js").read_text()
    assert '"/api/retry"' in store
    assert '"POST"' in store
    assert "X-Accrue-Token" not in store  # same-origin: the cookie authenticates
    assert "rows_done" in store  # the delta-only key feeds rows.done

    triage = (STATIC / "views" / "triage.js").read_text()
    assert "postRetry({ all: true }" in triage.replace("\n", " ") or (
        "body=${{ all: true }}" in triage
    )
    assert "group: { step: group.step, type: group.type, key: group.key }" in triage
    assert "retryBusy()" in triage

    inspector = (STATIC / "views" / "inspector.js").read_text()
    assert "postRetry({ rows: [sel.row] }" in inspector
    assert "retryBusy()" in inspector


def test_delta_reset_triggers_a_full_snapshot_refetch():
    """A reset delta (truncate-and-regrow reindex) must refetch /api/run whole,
    not merely gap-refetch — stale in-grid cells are not gap-detectable."""
    store = (STATIC / "lib" / "store.js").read_text()
    body = store.split("export function applyDelta", 1)[1]
    # The reset branch is handled before (and separately from) cell merging …
    assert body.index("if (delta.reset)") < body.index("delta.cells")
    # … refetches the whole snapshot and short-circuits the stale merge.
    reset_region = body.split("if (delta.reset)", 1)[1].split("let gap", 1)[0]
    assert "loadRun()" in reset_region
    assert "return;" in reset_region


def test_the_event_stream_follows_a_selection_the_store_changes():
    """A rewrite that drops the pinned run makes loadRun() follow the latest
    (accrue-ui#33). The open stream must be reopened for it: left on the
    vanished run's ?run=, it carries only resets and the page freezes."""
    store = (STATIC / "lib" / "store.js").read_text()
    fallback = store.split("export async function loadRun", 1)[1].split("}", 1)[0]
    assert "selectedRun.value = null" in fallback
    sse = (STATIC / "lib" / "sse.js").read_text()
    assert "sourceRun = selectedRun.value" in sse.split("export function connect", 1)[1]
    reopen = sse.split("selectedRun.subscribe(", 1)[1]
    assert "disconnect();\n    connect();" in reopen


def test_delta_live_clears_the_badge_and_stops_the_ticker():
    """live is a delta-only stat that drives run.live, which app.js reads for
    both the LIVE badge and the elapsed ticker."""
    store = (STATIC / "lib" / "store.js").read_text()
    # Pulled out of stats alongside rows_done, then written to run.live.
    assert "rows_done: rowsDone, live," in store
    body = store.split('typeof live === "boolean"', 1)[1]
    assert "run: { ...snapshot.value.run, live }" in body
    # The ticker and badge both key off run.live (already the case in app.js).
    app_js = (STATIC / "app.js").read_text()
    assert "snap.run.live) elapsedS.value++" in app_js
    assert "${run.live &&" in app_js


def test_export_report_button_downloads_from_the_endpoint():
    """The toolbar action GETs /api/report; same-origin, so the cookie auths."""
    app_js = (STATIC / "app.js").read_text()
    assert 'class="export-btn"' in app_js
    assert "onClick=${exportReport}" in app_js
    export = app_js.split("function exportReport()", 1)[1].split("}", 1)[0]
    assert '"/api/report"' in export
    assert "X-Accrue-Token" not in export  # same-origin GET: the cookie authenticates
    # No fetch/blob dance needed — Content-Disposition drives the save.
    css = (STATIC / "style.css").read_text()
    assert ".export-btn" in css
    block = css.split(".export-btn {", 1)[1].split("}", 1)[0]
    assert "#" not in block  # colors from the :root token table only


def test_launch_token_is_scrubbed_from_the_address_bar():
    """?token= is exchanged for a cookie at boot; it must not linger in the URL."""
    app_js = (STATIC / "app.js").read_text()
    assert "history.replaceState" in app_js
    assert 'searchParams.delete("token")' in app_js


def test_devstub_is_not_inside_the_package():
    """The stub has no auth at all — it must never ship in the wheel."""
    assert not (ROOT / "accrue_ui" / "devstub.py").exists()
    assert (ROOT / "tools" / "devstub.py").is_file()
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'include = ["accrue_ui*"]' in pyproject
    assert '"tools*"' in pyproject


def test_values_fetching_pages_and_cannot_feed_itself():
    """No JS runtime here — pin the loop-break at source level.

    Two halves: the row-list memo must not depend on valuesVersion (loading
    values would otherwise recompute it, re-firing the fetch effect), and
    the fetch must ask only for uncached rows in server-sized pages.
    """
    grid = (STATIC / "views" / "grid.js").read_text()
    memo_deps = grid.split("computeRows(arr, nsteps, sr, active)", 1)[1]
    memo_deps = memo_deps.split("]", 1)[0]
    assert "vversion" not in memo_deps
    # Values are asked for by explicit row list — a sorted or filtered grid
    # shows scattered rows — paged at the server's cap (accrue-ui#37).
    store = (STATIC / "lib" / "store.js").read_text()
    assert "VALUES_PAGE_MAX = 1000" in store  # matches the server's clamp
    assert "/api/values?rows=" in store
    assert "missingRows" in grid
    assert "!valuesCache.has(r)" in grid
    # The fetch effect keys off the row SET, not every cell-state bump.
    effect_deps = grid.split("}, 150);", 1)[1].split("[", 1)[1].split("]", 1)[0]
    assert "version" not in effect_deps
    # The row list's identity: a re-sort with the same count must refetch.
    assert "rows" in effect_deps


def test_grid_clamps_the_first_visible_row():
    """Switching to a sparser filter must not blank the grid for a frame."""
    grid = (STATIC / "views" / "grid.js").read_text()
    assert "Math.max(0, rows.length - 1)" in grid


def test_nested_values_show_one_sub_key_and_hover_the_rest():
    """A {value, source} field shows one sub-key, the object on hover (#36).

    The server sends object-valued fields as {o, d}; the grid shows the
    column's sub-key choice (falling back to the object's own default), and
    the choice is per column so it survives the field-chip cycling.
    """
    grid = (STATIC / "views" / "grid.js").read_text()
    store = (STATIC / "lib" / "store.js").read_text()
    assert "v.o[subKeyOf(stepName, field, v)]" in grid
    assert "data-tip=${tip}" in grid  # the whole object, one sub-key per line
    assert "cycleSubKey(step, field)" in grid
    assert "choice in entry.o ? choice : entry.d" in store
    # Keyed per column — one column per field since accrue-ui#37.
    assert "subKeyChoice.value[colId(stepName, field)]" in store
    assert "subKeysSeen.clear()" in store  # another run's keys don't linger
    assert "noteSubKeys(row)" in store  # grown on load, not scanned per render


def test_null_renders_distinct_from_an_empty_string():
    """null is a faint "null"; "" and an absent field stay blank (#36)."""
    grid = (STATIC / "views" / "grid.js").read_text()
    css = (STATIC / "style.css").read_text()
    inspector = (STATIC / "views" / "inspector.js").read_text()
    assert "v === null && !!(cell && cell.f && field in cell.f)" in grid
    assert '<span class="val null">null</span>' in grid
    assert ".dcell .val.null { color: var(--faint-1); }" in css
    assert '<span class="null">null</span>' in inspector
    # The inspector's pretty-printed objects keep their indentation.
    assert ".values-table td.value.json { white-space: pre-wrap; }" in css


def test_overview_shows_type_mismatch_and_every_batch_id():
    """#40: a field whose values don't match its declared type gets a hover
    note, and a batch step lists every id it submitted."""
    overview = (STATIC / "views" / "overview.js").read_text()
    css = (STATIC / "style.css").read_text()
    assert "const kinds = f.type_mismatch;" in overview
    # The accrue 1.5.1 pointer only for the stale-manifest case it explains.
    assert 'f.type === "str" && kinds.includes("object")' in overview
    assert "<${MismatchChip} f=${f} />" in overview
    assert "<${BatchIds} ids=${o.batch_ids} />" in overview
    assert "ids.map((id) =>" in overview  # every id, none truncated
    # Retry amber, the existing "look again" tone; nothing under the 11px floor.
    mismatch = css.split(".ov-mismatch {", 1)[1].split("}", 1)[0]
    assert "var(--retry-text)" in mismatch
    assert "font-size: 11px" in mismatch


def test_search_filter_and_sort_are_one_server_query():
    """The grid shows exactly /api/rows' list whenever a query is active
    (accrue-ui#37): nothing is filtered client-side, so the summary line and
    the grid cannot disagree — the global state chips ride the same request
    as an any-step clause."""
    store = (STATIC / "lib" / "store.js").read_text()
    filters = (STATIC / "views" / "filters.js").read_text()
    grid = (STATIC / "views" / "grid.js").read_text()
    assert "/api/rows?" in store
    assert 'placeholder="Search values…"' in filters  # no "loaded keys" limit
    assert 'clauses.push({ col: "*", op: "state", v: [want] })' in store
    assert "Search loaded keys" not in (STATIC / "app.js").read_text()
    # Active query, no answer yet: an empty list, not an unfiltered flash.
    assert "if (active) return sr ? sr.rows.filter((r) => r < total) : [];" in grid
    # A stale response never overwrites a newer request's answer.
    assert "if (seq !== rowsSeq) return;" in store
    # A failed query keeps the last good list and says so.
    assert "rowsError.value = message;" in store
    assert "rowsError.value" in filters
    # Cell deltas mark the list stale rather than re-sorting under the reader.
    assert "if (serverRows.value) rowsStale.value = true;" in store
    assert "Results may be stale" in filters


def test_query_state_round_trips_through_the_url_hash():
    """A reload or a shared link restores the view (accrue-ui#37)."""
    store = (STATIC / "lib" / "store.js").read_text()
    app_js = (STATIC / "app.js").read_text()
    for key in ('"q"', '"in"', '"f"', '"sort"', '"rows"', '"hide"', '"wrap"', '"view"'):
        assert f"p.set({key}" in store, key
        assert f"p.get({key})" in store or f"p.has({key})" in store, key
    assert "history.replaceState" in store
    # Read before the first snapshot fetch, and never written before read.
    boot = app_js.split("readHash();", 1)[1]
    assert "loadRun()" in boot.split("connect()", 1)[0]
    assert "if (!hashReady" in store


def test_filters_are_typed_from_the_manifest():
    filters = (STATIC / "views" / "filters.js").read_text()
    assert 'spec.type === "enum"' in filters
    assert 'spec.type === "bool"' in filters
    assert "NUMERIC_TYPES.has(spec.type)" in filters
    for op in ("empty", "notempty", "in", "range", "contains", "eq", "regex", "state"):
        assert f'"{op}"' in filters, op
    # Enum counts come from the server, on the sub-key the column shows.
    assert "loadFacet(col, sub)" in filters
    # Object-valued columns filter on the sub-key the column shows.
    assert "if (clause && sub) clause.sub = sub;" in filters


def test_rows_fixture_shape():
    doc = json.loads((FIXTURES / "rows.json").read_text())
    assert set(doc) == {"rows", "total", "matched"}
    assert doc["matched"] == len(doc["rows"])
    run = _run_doc()
    assert all(0 <= r < run["rows"]["total"] for r in doc["rows"])


def test_error_groups_refetch_when_a_delta_moves_the_error_count():
    """Deltas carry counters, not groups: the triage list must catch up."""
    store = (STATIC / "lib" / "store.js").read_text()
    assert "refreshErrorGroups" in store
    assert "errorGroupsStale" in store
    assert "errorsMoved" in store
    triage = (STATIC / "views" / "triage.js").read_text()
    assert "errorGroupsStale" in triage


def test_stat_tiles_are_honest_when_the_run_is_not_live():
    """No throughput or ETA tile for a run nothing is writing to."""
    app_js = (STATIC / "app.js").read_text()
    assert "const live = snap.run.live" in app_js
    for label in ("Throughput", "Time remaining"):
        guarded = "${live &&\n    html`<${StatTile}\n      label=" + f'"{label}"'
        assert guarded in app_js, label
    # Throughput renders as an integer, and as an em-dash when unknown.
    assert "fmtInt(Math.round(throughput))" in app_js
    assert "throughput == null" in app_js


def test_nullable_money_renders_as_an_em_dash():
    """spend/cache_saved/wasted/batch_saved are number|null — never blank."""
    cost = (STATIC / "views" / "cost.js").read_text()
    assert 'const DASH = "—"' in cost
    assert "value ?? DASH" in cost
    assert "if (value == null) return null;" not in cost  # tiles are kept
    app_js = (STATIC / "app.js").read_text()
    assert "fmtMoney(stats.spend) ?? unpricedValue(unpriced) ?? DASH" in app_js
    css = (STATIC / "style.css").read_text()
    assert ".cost-tile .v.dim" in css


def test_unpriced_models_are_named_instead_of_dashed():
    """stats.unpriced: say which model has no price and how to supply one."""
    fmt = (STATIC / "lib" / "fmt.js").read_text()
    assert "no price for ${models[0]}" in fmt
    assert "--prices <file.toml>" in fmt
    app_js = (STATIC / "app.js").read_text()
    # The Spend tile falls back to the name before the em-dash.
    assert "fmtMoney(stats.spend) ?? unpricedValue(unpriced) ?? DASH" in app_js
    assert "unpricedTip(unpriced)" in app_js
    cost = (STATIC / "views" / "cost.js").read_text()
    assert "fmtMoney(actual) ?? fmtUnpriced(unpriced)" in cost
    # By model lists unpriced models as rows, so a partly-priced run says so.
    assert 'class="bar-row unpriced"' in cost
    assert "unpriced=${unpriced}" in cost
    css = (STATIC / "style.css").read_text()
    assert ".cost-tile .v.unpriced" in css
    assert ".stat-value .unpriced" in css


def test_unrecorded_cache_tokens_say_so_instead_of_zero():
    """cost.tokens.cache_* is null on a log from before the cache fields."""
    fmt = (STATIC / "lib" / "fmt.js").read_text()
    assert 'CACHE_NOT_RECORDED = "not recorded"' in fmt
    cost = (STATIC / "views" / "cost.js").read_text()
    assert "value == null" in cost.split("function TokenRow", 1)[1]
    assert "data-tip=${CACHE_NOT_RECORDED_TIP}" in cost
    assert 'label="Cache read" value=${cost.tokens.cache_read}' in cost
    assert 'label="Cache write" value=${cost.tokens.cache_write}' in cost
    # fmtInt(null) is "" — the old rows would have rendered a blank.
    assert "fmtInt(cost.tokens.cache_read)" not in cost
    inspector = (STATIC / "views" / "inspector.js").read_text()
    assert "d.usage.cache_read != null" in inspector
    # Not "Cache": that row is accrue's result cache (from_cache).
    assert '"Prompt cache"' in inspector
    css = (STATIC / "style.css").read_text()
    assert ".token-row .v.dim" in css


def test_run_chip_does_not_repeat_the_id():
    app_js = (STATIC / "app.js").read_text()
    assert "run.name !== run.id" in app_js
    assert "${runLabel(run)}" in app_js
    assert "${r.name} / ${r.id}" not in app_js


def test_attempt_count_is_pluralized():
    inspector = (STATIC / "views" / "inspector.js").read_text()
    assert 'after ${plural(n, "attempt")}' in inspector
    assert "attempts —" not in inspector


def test_cost_bars_match_the_approved_design():
    """Single-hue CSS bars: jade fill, 14px, rounded right end, mono value."""
    cost = (STATIC / "views" / "cost.js").read_text()
    # Label, bar, value — in that order, in the same row.
    row = cost.split('class="bar-row"', 1)[1].split("</div>", 1)[0]
    assert row.index("bar-label") < row.index("bar-fill") < row.index("bar-value")
    assert "(value / max) * 100" in row  # widths proportional to the max

    css = (STATIC / "style.css").read_text()
    fill = css.split(".bar-fill {", 1)[1].split("}", 1)[0]
    assert "height: 14px" in fill
    assert "background: var(--jade-9)" in fill  # #29a383, via the token table
    assert "border-radius: 0 4px 4px 0" in fill  # right end only
    assert "#" not in fill  # no literal colors outside :root
    value = css.split(".bar-row .bar-value {", 1)[1].split("}", 1)[0]
    assert "var(--font-mono)" in value
    assert "text-align: right" in value


def test_tooltip_floats_into_a_body_portal_that_escapes_overflow():
    """The CSS-only ::after tooltip was clipped by overflow ancestors and had
    no viewport-edge handling; it is replaced by one floating node portaled to
    <body> (lib/tooltip.js), positioned fixed and stacked above the inspector.
    """
    css = (STATIC / "style.css").read_text()
    # The clipped CSS-only painting is gone …
    assert ".tip::after" not in css
    assert ".tip::before" not in css
    # … replaced by a floating layer, styled from the :root tokens only.
    assert ".tip-floating" in css
    block = css.split(".tip-floating {", 1)[1].split("}", 1)[0]
    assert "position: fixed" in block  # no overflow ancestor can clip a fixed node
    assert "var(--hover)" in block and "var(--border-2)" in block
    assert "var(--ink)" in block
    assert "#" not in block  # no literal colors outside the :root token table
    z = int(block.split("z-index:", 1)[1].split(";", 1)[0].split("/", 1)[0].strip())
    assert z > 90  # above .inspector (z-index: 90) and its scrim (80)
    # The dotted-underline affordance stays on the trigger label.
    assert ".tip-label" in css
    label = css.split(".tip-label {", 1)[1].split("}", 1)[0]
    assert "border-bottom: 1px dotted" in label


def test_tooltip_module_delegates_and_handles_collisions():
    """One shared node, delegated events (so Preact re-renders need no
    re-binding), edge-flip/clamp geometry, and dismissal on scroll/resize."""
    tip = (STATIC / "lib" / "tooltip.js").read_text()
    assert "export function initTooltips" in tip
    assert "document.body.appendChild" in tip  # the body portal
    assert "getBoundingClientRect" in tip  # geometry from the trigger's rect
    assert "position: fixed" not in tip  # visual style lives in CSS, not JS
    # Delegation on document for both pointer and keyboard.
    for event in ("mouseover", "mouseout", "focusin", "focusout"):
        assert event in tip, event
    # Collision handling: flip on bottom overflow, clamp to the viewport.
    assert "innerHeight" in tip
    assert "innerWidth" in tip
    # A stale tooltip never floats over the wrong place.
    assert '"scroll"' in tip and '"resize"' in tip
    # Wired exactly once from the app entry.
    app_js = (STATIC / "app.js").read_text()
    assert "initTooltips" in app_js
    assert "lib/tooltip.js" in app_js


def test_retry_note_uses_existing_error_tokens():
    """Inline retry errors reuse the failed-red token; no new colors."""
    css = (STATIC / "style.css").read_text()
    assert ".retry-note" in css
    block = css.split(".retry-note", 1)[1].split("}", 1)[0]
    assert "var(--error-text)" in block
    assert "#" not in block  # no literal colors outside the :root token table


# ---- devstub serves the app + fixtures ----------------------------------


def test_index_served_with_import_map(stub_url):
    body = _get(stub_url + "/").decode()
    assert "importmap" in body
    assert '<div id="app">' in body
    for specifier in ("preact", "preact/hooks", "@preact/signals", "htm"):
        assert f'"{specifier}"' in body
    assert "fonts.googleapis.com" in body  # Google Fonts stylesheet link


def test_all_static_files_serve(stub_url):
    for path in STATIC_FILES:
        assert (STATIC / path).is_file(), f"missing on disk: {path}"
        body = _get(f"{stub_url}/{path}")
        assert body, f"empty response for {path}"


def test_api_run_served(stub_url):
    doc = json.loads(_get(stub_url + "/api/run"))
    _data, steps = _cells_bytes(doc)
    assert steps == len(doc["steps"])


def test_api_values_served(stub_url):
    doc = json.loads(_get(stub_url + "/api/values?start=0&count=40"))
    # stub returns the fixture regardless of params
    assert doc["start"] == 1281
    assert len(doc["rows"]) == 12


def test_api_cell_served(stub_url):
    doc = json.loads(_get(stub_url + "/api/cell/web_search/1284"))
    assert doc["key"] == "vercel.com"


def test_api_runs_served(stub_url):
    doc = json.loads(_get(stub_url + "/api/runs"))
    assert len(doc["runs"]) == 3


def test_api_events_streams_sse(stub_url):
    req = urllib.request.Request(stub_url + "/api/events")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        chunk = resp.read(64).decode()
    assert chunk.startswith("event: delta\ndata: ")


def test_retrying_errors_that_fail_again_asks_first():
    """A non-retryable group, Retry all over one, and a row in one all arm a
    second-click confirm instead of posting straight away (#34)."""
    triage = (STATIC / "views" / "triage.js").read_text()
    assert "export function useRetryConfirm()" in triage
    assert "confirm.click(warn, source, () => postRetry(body, source))" in triage
    assert "warn=${failsAgain}" in triage
    assert "warn=${groups.some((g) => g.retryable === false)}" in triage

    inspector = (STATIC / "views" / "inspector.js").read_text()
    assert "const warn = rowFailsAgain(sel.row);" in inspector
    assert "confirm.click(warn, source" in inspector

    store = (STATIC / "lib" / "store.js").read_text()
    assert "export function rowFailsAgain(row)" in store

    css = (STATIC / "style.css").read_text()
    warn = css[css.index(".btn-warn {") :].split("}", 1)[0]
    assert "var(--retry-bg)" in warn and "var(--retry-text)" in warn
