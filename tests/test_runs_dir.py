"""``--runs-dir``: every run log under a folder, and switching between them
(accrue-ui#38).

amana keeps one run per folder — ``out/<run>/<model>/run.jsonl`` — so every
log has the same stem and the sibling listing offered one entry, "run".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from accrue_ui.server.index import scan_runs
from tests.conftest import age_file, client_for, row, write_log


def _log(path: Path, run_id: str, started_at: str, *, nrows: int = 2) -> Path:
    """A finished one-step run log at *path* (folders created)."""
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
        {"v": 1, "t": 9.0, "type": "pipeline_end", "elapsed_s": 9.0},
    ]
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
    served = logs["2026-09-24-full-a/gpt-5-mini"]
    with client_for(served) as client:
        runs = client.get("/api/runs").json()["runs"]
    assert [r["name"] for r in runs] == ["run"]
