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
        [sys.executable, "-m", "accrue_ui.devstub", "--port", str(port)],
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
    assert set(doc["retry"]) == {"available", "reason", "resume_command"}
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
        assert set(delta["stats"]) <= stats_keys
        for step in delta["steps"]:
            assert set(step) == {"name", "done", "errors"}
            assert step["name"] in step_names


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
