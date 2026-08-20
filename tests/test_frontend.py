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
    "views/grid.js",
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
            "count",
            "message",
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
    assert set(doc["usage"]) == {"in", "out", "cost"}
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
        # rows_done is the one delta-only stats key (docs/api-shapes.md).
        assert set(delta["stats"]) <= stats_keys | {"rows_done"}
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
    assert "group: { step: group.step, type: group.type }" in triage
    assert "retryBusy()" in triage

    inspector = (STATIC / "views" / "inspector.js").read_text()
    assert "postRetry({ rows: [sel.row] }" in inspector
    assert "retryBusy()" in inspector


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
    memo_deps = grid.split("computeRows(arr, nsteps, filter, query)", 1)[1]
    memo_deps = memo_deps.split("]", 1)[0]
    assert "vversion" not in memo_deps
    assert "VALUES_PAGE_MAX = 1000" in grid  # matches the server's clamp
    assert "missingWindows" in grid
    assert "!valuesCache.has(r)" in grid
    # The fetch effect keys off the row SET, not every cell-state bump.
    effect_deps = grid.split("}, 150);", 1)[1].split("[", 1)[1].split("]", 1)[0]
    assert "version" not in effect_deps
    assert "rows.length" in effect_deps


def test_grid_clamps_the_first_visible_row():
    """Switching to a sparser filter must not blank the grid for a frame."""
    grid = (STATIC / "views" / "grid.js").read_text()
    assert "Math.max(0, rows.length - 1)" in grid


def test_search_placeholder_admits_the_cache_limit():
    assert 'placeholder="Search loaded keys…"' in (STATIC / "app.js").read_text()


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
    assert "fmtMoney(stats.spend) ?? DASH" in app_js
    css = (STATIC / "style.css").read_text()
    assert ".cost-tile .v.dim" in css


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
