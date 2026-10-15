"""``--runs-dir``: every run log under a folder, and switching between them
(accrue-ui#38).

amana keeps one run per folder — ``out/<run>/<model>/run.jsonl`` — so every
log has the same stem and the sibling listing offered one entry, "run".
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from accrue_ui.server.app import create_app
from accrue_ui.server.index import scan_runs
from accrue_ui.server.retry import NO_PIPELINE
from tests.conftest import (
    AUTH_HEADERS,
    TOKEN,
    SSEStream,
    age_file,
    client_for,
    lifespan_ctx,
    row,
    write_log,
)

POST = {"Origin": "http://localhost", "Content-Type": "application/json"}


def _log(
    path: Path, run_id: str, started_at: str, *, nrows: int = 2, ended: bool = True
) -> Path:
    """A one-step run log at *path* (folders created); *ended*: finished."""
    path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = [
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": run_id,
            "started_at": started_at,
            "num_rows": nrows,
            "display_key": None,
            "steps": [{"name": "classify", "level": 0, "mode": "realtime"}],
        },
        *(
            row("classify", r, 1.0 + r, values={"who": f"{run_id}-{r}"})
            for r in range(nrows)
        ),
    ]
    if ended:
        records.append({"v": 1, "t": 9.0, "type": "pipeline_end", "elapsed_s": 9.0})
    write_log(path, records)
    age_file(path)
    return path


def amana_tree(root: Path) -> dict[str, Path]:
    """``out/<run>/<model>/run.jsonl`` for two passes, plus the clutter a
    real ``out/`` holds: a data ``*.jsonl``, a capture sidecar, a dot-dir."""
    out = root / "out"
    logs = {
        "2026-09-24-full-a/claude-sonnet-4-6": _log(
            out / "2026-09-24-full-a" / "claude-sonnet-4-6" / "run.jsonl",
            "full-a",
            "2026-09-24T09:00:00+00:00",
        ),
        "2026-09-24-full-a/gpt-5-mini": _log(
            out / "2026-09-24-full-a" / "gpt-5-mini" / "run.jsonl",
            "full-a",
            "2026-09-24T10:00:00+00:00",
            nrows=3,
        ),
        "2026-09-25-full-b/claude-sonnet-4-6": _log(
            out / "2026-09-25-full-b" / "claude-sonnet-4-6" / "run.jsonl",
            "full-b",
            "2026-09-25T09:00:00+00:00",
        ),
    }
    write_log(out / "2026-09-24-full-a" / "input.jsonl", [{"company": "acme"}])
    # Beside a run log, in its own folder: must not cost that log its name.
    write_log(
        out / "2026-09-24-full-a" / "gpt-5-mini" / "output.jsonl", [{"company": "acme"}]
    )
    write_log(
        out / "2026-09-25-full-b" / "claude-sonnet-4-6" / "run.prompts.jsonl",
        [{"messages": [], "response": "body"}],
    )
    _log(out / ".cache" / "stale" / "run.jsonl", "cached", "2026-09-30T00:00:00+00:00")
    return logs


def test_runs_dir_lists_every_run_by_folder_newest_first(tmp_path: Path):
    logs = amana_tree(tmp_path)
    runs = scan_runs(tmp_path / "out", recursive=True)
    assert [r["name"] for r in runs] == [
        "2026-09-25-full-b/claude-sonnet-4-6",
        "2026-09-24-full-a/gpt-5-mini",
        "2026-09-24-full-a/claude-sonnet-4-6",
    ]
    assert {r["path"] for r in runs} == {str(p) for p in logs.values()}
    assert [r["id"] for r in runs] == ["full-b", "full-a", "full-a"]


def test_runs_dir_skips_data_files_sidecars_and_dot_dirs(tmp_path: Path):
    amana_tree(tmp_path)
    paths = [r["path"] for r in scan_runs(tmp_path / "out", recursive=True)]
    assert not any(p.endswith(("input.jsonl", ".prompts.jsonl")) for p in paths)
    assert not any("/.cache/" in p for p in paths)


def test_folder_with_several_logs_names_each_by_stem(tmp_path: Path):
    root = tmp_path / "out"
    _log(root / "pass" / "a.jsonl", "a", "2026-09-24T09:00:00+00:00")
    _log(root / "pass" / "b.jsonl", "b", "2026-09-24T10:00:00+00:00")
    _log(root / "top.jsonl", "top", "2026-09-24T11:00:00+00:00")
    names = [r["name"] for r in scan_runs(root, recursive=True)]
    assert names == ["top", "pass/b", "pass/a"]


def test_runs_route_lists_the_tree_and_flags_the_served_log(tmp_path: Path):
    logs = amana_tree(tmp_path)
    served = logs["2026-09-24-full-a/gpt-5-mini"]
    with client_for(served, runs_dir=tmp_path / "out") as client:
        runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 3
    assert [r["name"] for r in runs if r["served"]] == ["2026-09-24-full-a/gpt-5-mini"]


def test_launch_log_outside_the_tree_is_still_listed(tmp_path: Path):
    amana_tree(tmp_path)
    elsewhere = _log(
        tmp_path / "scratch" / "probe.jsonl", "probe", "2026-01-01T00:00:00Z"
    )
    with client_for(elsewhere, runs_dir=tmp_path / "out") as client:
        runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 4
    assert [r["path"] for r in runs if r["served"]] == [str(elsewhere)]


def test_without_runs_dir_the_listing_is_siblings_only(tmp_path: Path):
    logs = amana_tree(tmp_path)
    served = logs["2026-09-25-full-b/claude-sonnet-4-6"]
    with client_for(served) as client:
        runs = client.get("/api/runs").json()["runs"]
    assert [r["name"] for r in runs] == ["run"]


# ---- switching: POST /api/runs/select -------------------------------------


def _select(client, path: Path | str):
    return client.post(
        "/api/runs/select", headers=POST, content=json.dumps({"path": str(path)})
    )


def _rows(client) -> list[str]:
    values = client.get("/api/values?start=0&count=10").json()["rows"]
    return [v["cells"]["classify"]["f"]["who"] for v in values]


def test_select_serves_the_other_log(tmp_path: Path):
    logs = amana_tree(tmp_path)
    launch = logs["2026-09-24-full-a/claude-sonnet-4-6"]
    other = logs["2026-09-24-full-a/gpt-5-mini"]
    with client_for(launch, runs_dir=tmp_path / "out") as client:
        before = client.get("/api/run").json()
        switched = _select(client, other)
        after = client.get("/api/run").json()
        values = _rows(client)
        runs = client.get("/api/runs").json()["runs"]

    assert switched.status_code == 200
    assert switched.json()["name"] == "2026-09-24-full-a/gpt-5-mini"
    # Same run id in both folders: only the log tells them apart.
    assert before["run"]["id"] == after["run"]["id"] == "full-a"
    assert before["log"]["path"] == str(launch)
    assert after["log"] == {"path": str(other), "name": "2026-09-24-full-a/gpt-5-mini"}
    assert after["run"]["name"] == "2026-09-24-full-a/gpt-5-mini"
    assert after["rows"]["total"] == 3
    assert values == ["full-a-0", "full-a-1", "full-a-2"]
    assert [r["name"] for r in runs if r["served"]] == ["2026-09-24-full-a/gpt-5-mini"]


def test_select_accepts_only_listed_logs(tmp_path: Path):
    logs = amana_tree(tmp_path)
    launch = logs["2026-09-24-full-a/claude-sonnet-4-6"]
    out = tmp_path / "out"
    with client_for(launch, runs_dir=out) as client:
        data_file = _select(client, out / "2026-09-24-full-a" / "input.jsonl")
        dot_dir = _select(client, out / ".cache" / "stale" / "run.jsonl")
        outside = _select(client, "/etc/hosts")
        traversal = _select(
            client, out / ".." / "out" / ".cache" / "stale" / "run.jsonl"
        )
        served = client.get("/api/run").json()["log"]["path"]

    for refused in (data_file, dot_dir, outside, traversal):
        assert refused.status_code == 404
    assert served == str(launch)


def test_select_rejects_a_bad_body(tmp_path: Path):
    logs = amana_tree(tmp_path)
    with client_for(logs["2026-09-24-full-a/gpt-5-mini"]) as client:
        not_json = client.post("/api/runs/select", headers=POST, content="nope")
        no_path = client.post("/api/runs/select", headers=POST, content="{}")
        wrong_type = client.post(
            "/api/runs/select", headers=POST, content=json.dumps({"path": 3})
        )
        plain = client.post(
            "/api/runs/select",
            headers={**POST, "Content-Type": "text/plain"},
            content=json.dumps({"path": "x"}),
        )
        get = client.get("/api/runs/select")
    assert [not_json.status_code, no_path.status_code, wrong_type.status_code] == [
        400,
        400,
        400,
    ]
    assert plain.status_code == 415
    assert get.status_code == 405


def test_retry_stays_on_the_launch_log(tmp_path: Path):
    logs = amana_tree(tmp_path)
    launch = logs["2026-09-24-full-a/claude-sonnet-4-6"]
    with client_for(launch, runs_dir=tmp_path / "out") as client:
        _select(client, logs["2026-09-25-full-b/claude-sonnet-4-6"])
        away = client.get("/api/run").json()["retry"]
        refused = client.post("/api/retry", headers=POST, content='{"all": true}')
        _select(client, launch)
        back = client.get("/api/run").json()["retry"]

    assert away["available"] is False
    assert str(launch) in away["reason"]
    assert refused.status_code == 409
    assert refused.json()["reason"] == away["reason"]
    # Home again: retry is back to the controller's own answer.
    assert back["reason"] == NO_PIPELINE


def _poll(fn, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while not fn():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_follower_tails_the_new_log_and_drops_the_old(tmp_path: Path):
    out = tmp_path / "out"
    launch = _log(out / "a" / "m" / "run.jsonl", "a", "2026-09-24T09:00:00Z")
    other = _log(
        out / "b" / "m" / "run.jsonl", "b", "2026-09-25T09:00:00Z", nrows=3, ended=False
    )
    # The other log is a run still in progress: row 2 not written yet.
    lines = other.read_text().splitlines()
    other.write_text("\n".join(lines[:-1]) + "\n")
    with client_for(launch, runs_dir=out) as client:
        assert _select(client, other).status_code == 200
        assert client.get("/api/run").json()["rows"]["done"] == 2
        with open(launch, "a", encoding="utf-8") as f:
            f.write(json.dumps(row("classify", 1, 3.0, status="error")) + "\n")
        with open(other, "a", encoding="utf-8") as f:
            f.write(lines[-1] + "\n")
        _poll(lambda: client.get("/api/run").json()["rows"]["done"] == 3)
        snap = client.get("/api/run").json()
    assert snap["run"]["id"] == "b"
    assert snap["stats"]["errors"] == 0


async def test_select_resets_every_client(tmp_path: Path):
    logs = amana_tree(tmp_path)
    app = create_app(
        logs["2026-09-24-full-a/claude-sonnet-4-6"],
        token=TOKEN,
        runs_dir=tmp_path / "out",
    )
    async with lifespan_ctx(app):
        latest = SSEStream(app)
        pinned = SSEStream(app, query="run=full-a")
        try:
            await latest.wait_for(lambda s: s.chunks)
            await pinned.wait_for(lambda s: s.chunks)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost", headers=AUTH_HEADERS
            ) as client:
                res = await client.post(
                    "/api/runs/select",
                    headers=POST,
                    content=json.dumps(
                        {"path": str(logs["2026-09-25-full-b/claude-sonnet-4-6"])}
                    ),
                )
            assert res.status_code == 200
            await latest.wait_for(lambda s: any(d.get("reset") for d in s.deltas))
            await pinned.wait_for(lambda s: any(d.get("reset") for d in s.deltas))
            # The backfill is the new snapshot, not a delta.
            await asyncio.sleep(0.3)
            assert not any(d["cells"] for d in latest.deltas)
        finally:
            await latest.close()
            await pinned.close()
