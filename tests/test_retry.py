"""Retry: spec import, row selection, one-at-a-time, and healing the index.

The unit half drives ``RetryController`` against throwaway modules written
into ``tmp_path`` (no accrue needed — a "pipeline" is anything with
``retry_failed_async``). The integration half installs a real accrue
pipeline whose failures are curable, serves its log, and POSTs
``/api/retry`` to watch the cells heal end to end.
"""

from __future__ import annotations

import base64
import importlib
import itertools
import json
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from accrue_ui.server.index import RunIndex
from accrue_ui.server.retry import RetryController
from accrue_ui.server.tail import TailRecord
from tests.conftest import age_file, client_for, row, write_log

PENDING, RUNNING, OK, CACHED, RETRYING, ERROR, SKIPPED = range(7)

ORIGIN = {"Origin": "http://localhost"}

_counter = itertools.count()

# A stand-in pipeline: records what retry_failed_async was called with and
# holds the call open while HOLD["on"] is true (the one-at-a-time test).
FAKE_PIPELINE = textwrap.dedent("""
    CALLS = []
    HOLD = {"on": False}
    BOOM = {"on": False}


    class FakePipeline:
        async def retry_failed_async(self, data, **kwargs):
            CALLS.append({"data": data, **kwargs})
            while HOLD["on"]:
                import asyncio

                await asyncio.sleep(0.01)
            if BOOM["on"]:
                raise RuntimeError("pipeline exploded")
            return "result"


    pipeline = FakePipeline()
    rows = [{"company": "a"}, {"company": "b"}]


    def make():
        return (pipeline, rows)


    def rows_fn():
        return rows
""")


@pytest.fixture
def write_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write an importable throwaway module; returns its module name."""
    monkeypatch.syspath_prepend(str(tmp_path))

    def _write(source: str, name: str | None = None) -> str:
        name = name or f"retry_mod_{next(_counter)}"
        (tmp_path / f"{name}.py").write_text(source)
        importlib.invalidate_caches()
        sys.modules.pop(name, None)
        monkeypatch.delitem(sys.modules, name, raising=False)
        return name

    return _write


def controller_for(pipeline: str | None, data: str | None = None) -> RetryController:
    return RetryController(Path("run.jsonl"), pipeline, data)


# ---- --pipeline / --data argument forms -----------------------------------


def test_no_pipeline_is_unavailable():
    controller = controller_for(None)
    assert controller.available is False
    assert controller.reason == "launched without --pipeline"
    assert controller.block()["running"] is False
    assert controller.block()["last_error"] is None


def test_data_without_pipeline():
    assert controller_for(None, "mod:rows").reason == "--data requires --pipeline"


@pytest.mark.parametrize("spec", ["nocolon", ":attr", "mod:", ""])
def test_malformed_pipeline_spec(spec: str):
    assert controller_for(spec).reason == f"--pipeline {spec!r} is not module:attr"


def test_malformed_data_spec(write_module):
    mod = write_module(FAKE_PIPELINE)
    controller = controller_for(f"{mod}:pipeline", "nocolon")
    assert controller.reason == "--data 'nocolon' is not module:attr"


def test_missing_module():
    reason = controller_for("no_such_module_xyz:pipeline").reason
    assert "cannot import module 'no_such_module_xyz'" in reason
    assert "ModuleNotFoundError" in reason


def test_module_raising_on_import(write_module):
    mod = write_module("raise RuntimeError('boom at import')")
    reason = controller_for(f"{mod}:pipeline").reason
    assert f"cannot import module '{mod}'" in reason
    assert "RuntimeError: boom at import" in reason


def test_missing_attribute(write_module):
    mod = write_module(FAKE_PIPELINE)
    assert controller_for(f"{mod}:nope").reason == (
        f"--pipeline '{mod}:nope': '{mod}' has no attribute 'nope'"
    )


def test_pipeline_instance_without_data(write_module):
    mod = write_module(FAKE_PIPELINE)
    assert controller_for(f"{mod}:pipeline").reason == (
        f"--pipeline '{mod}:pipeline' is a FakePipeline; "
        "retry also needs --data module:attr"
    )


def test_pipeline_instance_with_list_data(write_module):
    mod = write_module(FAKE_PIPELINE)
    controller = controller_for(f"{mod}:pipeline", f"{mod}:rows")
    assert controller.available is True
    assert controller.reason is None


def test_pipeline_instance_with_callable_data(write_module):
    mod = write_module(FAKE_PIPELINE)
    assert controller_for(f"{mod}:pipeline", f"{mod}:rows_fn").available is True


def test_pipeline_instance_with_dataframe_data(write_module):
    mod = write_module(
        FAKE_PIPELINE + "\nimport pandas as pd\n\nframe = pd.DataFrame(rows)\n"
    )
    assert controller_for(f"{mod}:pipeline", f"{mod}:frame").available is True


def test_data_of_the_wrong_type(write_module):
    mod = write_module(FAKE_PIPELINE + "\nnot_data = 'oops'\n")
    assert controller_for(f"{mod}:pipeline", f"{mod}:not_data").reason == (
        f"--data '{mod}:not_data' is a str; expected a DataFrame, a list of "
        "dicts, or a zero-arg callable returning one"
    )


def test_data_callable_returning_the_wrong_type(write_module):
    mod = write_module(FAKE_PIPELINE + "\ndef bad_rows():\n    return 'oops'\n")
    assert controller_for(f"{mod}:pipeline", f"{mod}:bad_rows").reason == (
        f"--data '{mod}:bad_rows': bad_rows() returned str; expected a "
        "DataFrame or a list of dicts"
    )


def test_data_callable_raising(write_module):
    mod = write_module(
        FAKE_PIPELINE + "\ndef bad_rows():\n    raise ValueError('no csv')\n"
    )
    assert controller_for(f"{mod}:pipeline", f"{mod}:bad_rows").reason == (
        f"--data '{mod}:bad_rows': calling bad_rows() raised ValueError: no csv"
    )


def test_factory_returning_pipeline_and_data(write_module):
    mod = write_module(FAKE_PIPELINE)
    controller = controller_for(f"{mod}:make")
    assert controller.available is True
    assert controller.reason is None


def test_factory_rejects_a_redundant_data_flag(write_module):
    mod = write_module(FAKE_PIPELINE)
    assert controller_for(f"{mod}:make", f"{mod}:rows").reason == (
        f"--pipeline '{mod}:make' already supplies data; drop --data"
    )


def test_factory_returning_a_non_tuple(write_module):
    mod = write_module(FAKE_PIPELINE + "\ndef bad():\n    return {'p': pipeline}\n")
    assert controller_for(f"{mod}:bad").reason == (
        f"--pipeline '{mod}:bad': bad() returned dict; expected (pipeline, data)"
    )


def test_factory_returning_something_that_is_not_a_pipeline(write_module):
    mod = write_module(FAKE_PIPELINE + "\ndef bad():\n    return ('nope', rows)\n")
    assert controller_for(f"{mod}:bad").reason == (
        f"--pipeline '{mod}:bad': bad() returned str as its pipeline; "
        "expected a Pipeline"
    )


def test_factory_returning_bad_data(write_module):
    mod = write_module(FAKE_PIPELINE + "\ndef bad():\n    return (pipeline, 'nope')\n")
    assert controller_for(f"{mod}:bad").reason == (
        f"--pipeline '{mod}:bad': bad() returned str as its data; "
        "expected a DataFrame or a list of dicts"
    )


def test_factory_raising(write_module):
    mod = write_module(FAKE_PIPELINE + "\ndef bad():\n    raise RuntimeError('boom')\n")
    assert controller_for(f"{mod}:bad").reason == (
        f"--pipeline '{mod}:bad': calling bad() raised RuntimeError: boom"
    )


def test_attribute_that_is_neither_pipeline_nor_callable(write_module):
    mod = write_module(FAKE_PIPELINE + "\nsettings = {'a': 1}\n")
    assert controller_for(f"{mod}:settings").reason == (
        f"--pipeline '{mod}:settings' is a dict; expected a Pipeline or a "
        "zero-arg callable returning (pipeline, data)"
    )


# ---- config ---------------------------------------------------------------


def test_default_config_enables_checkpointing(write_module):
    """Retry reads the checkpoint, so checkpointing is forced on."""
    mod = write_module(FAKE_PIPELINE)
    controller = controller_for(f"{mod}:make")
    controller.resolve()
    config = controller._target.config
    assert config.enable_checkpointing is True
    assert config.enable_progress_bar is False


def test_factory_may_supply_the_runs_config(write_module):
    """A run with a custom checkpoint_dir can only be retried with its config."""
    mod = write_module(
        FAKE_PIPELINE
        + textwrap.dedent("""
            from accrue import EnrichmentConfig


            def with_config():
                config = EnrichmentConfig(checkpoint_dir="/tmp/ckpt", max_workers=3)
                return (pipeline, rows, config)
        """)
    )
    controller = controller_for(f"{mod}:with_config")
    controller.resolve()
    config = controller._target.config
    assert config.checkpoint_dir == "/tmp/ckpt"
    assert config.max_workers == 3
    assert config.enable_checkpointing is True  # forced on even when off


# ---- resume_command -------------------------------------------------------


def test_resume_command_forms():
    assert RetryController("run.jsonl").resume_command == (
        "accrue-ui run.jsonl --pipeline <module:attr>"
    )
    assert RetryController("run.jsonl", "enrich:pipe").resume_command == (
        "accrue-ui run.jsonl --pipeline enrich:pipe"
    )
    assert RetryController(
        "run.jsonl", "enrich:pipe", "enrich:rows"
    ).resume_command == (
        "accrue-ui run.jsonl --pipeline enrich:pipe --data enrich:rows"
    )


def test_resume_command_quotes_awkward_paths():
    command = RetryController("my runs/a.jsonl", "enrich:pipe").resume_command
    assert command == "accrue-ui 'my runs/a.jsonl' --pipeline enrich:pipe"


# ---- index: error groups, healing, unknown records ------------------------


def _records(num_rows: int = 6) -> list[dict]:
    return [
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": "heal-run",
            "started_at": "2026-08-20T10:00:00+00:00",
            "num_rows": num_rows,
            "display_key": "company",
            "steps": [
                {"name": "fetch", "level": 0, "mode": "realtime", "model": None},
                {"name": "score", "level": 1, "mode": "realtime", "model": None},
            ],
            "plan": None,
        },
        {
            "v": 1,
            "t": 0.1,
            "type": "step_start",
            "step": "fetch",
            "level": 0,
            "mode": "realtime",
            "num_rows": num_rows,
        },
    ]


def _failing_log(tmp_path: Path, num_rows: int = 6) -> Path:
    """fetch: rows 1 and 4 fail with Boom, row 2 with Other; the rest are ok."""
    records = _records(num_rows)
    for r in range(num_rows):
        if r in (1, 4):
            records.append(
                row(
                    "fetch",
                    r,
                    1.0 + r,
                    status="error",
                    error={"type": "Boom", "msg": "kaboom"},
                )
            )
        elif r == 2:
            records.append(
                row(
                    "fetch",
                    r,
                    1.0 + r,
                    status="error",
                    error={"type": "Other", "msg": "different"},
                )
            )
        else:
            records.append(row("fetch", r, 1.0 + r, values={"company": f"c{r}"}))
    log = tmp_path / "heal.jsonl"
    write_log(log, records)
    age_file(log)
    return log


def _index_from(log: Path) -> RunIndex:
    index = RunIndex(log)
    with open(log, encoding="utf-8") as f:
        for line in f:
            index.apply(TailRecord(0, len(line), json.loads(line)))
    return index


def test_group_rows_resolution(tmp_path: Path):
    index = _index_from(_failing_log(tmp_path))
    assert index.group_rows("fetch", "Boom") == [1, 4]
    assert index.group_rows("fetch", "Other") == [2]
    assert index.group_rows("fetch", "Nope") is None
    assert index.group_rows("nosuchstep", "Boom") is None
    assert index.error_rows() == [1, 2, 4]


def test_ok_row_complete_heals_cell_group_and_stats(tmp_path: Path):
    index = _index_from(_failing_log(tmp_path))
    assert index.snapshot()["stats"]["errors"] == 3
    index.drain_delta()  # prime, so the next delta carries only the healing

    index.apply(TailRecord(0, 1, row("fetch", 1, 20.0, values={"company": "c1"})))

    delta = index.drain_delta()
    assert delta["cells"] == [[1, 0, OK]]  # the UI watches error -> ok live
    assert delta["steps"] == [{"name": "fetch", "done": 6, "errors": 2}]
    assert delta["stats"]["errors"] == 2

    snap = index.snapshot()
    groups = {(g["step"], g["type"]): g for g in snap["error_groups"]}
    boom = groups[("fetch", "Boom")]
    assert boom["count"] == 1
    assert boom["rows"] == [[4, 4]]  # row 1 left the group
    assert sum(boom["histogram"]) == 1
    assert snap["stats"]["errors"] == 2
    assert sum(s["errors"] for s in snap["steps"]) == 2
    assert sum(g["count"] for g in snap["error_groups"]) == 2
    # The healed cell reads as a normal success everywhere.
    assert index.cell_detail("fetch", 1)["status"] == "ok"
    assert index.values_window(1, 1)["rows"][0]["cells"]["fetch"]["s"] == OK


def test_empty_group_disappears(tmp_path: Path):
    index = _index_from(_failing_log(tmp_path))
    for r in (1, 4):
        index.apply(
            TailRecord(0, 1, row("fetch", r, 20.0 + r, values={"company": "x"}))
        )
    types = {g["type"] for g in index.snapshot()["error_groups"]}
    assert types == {"Other"}  # Boom lost its last row and is gone

    index.apply(TailRecord(0, 1, row("fetch", 2, 30.0, values={"company": "x"})))
    snap = index.snapshot()
    assert snap["error_groups"] == []
    assert snap["stats"]["errors"] == 0
    assert snap["steps"][0]["done"] == 6  # re-delivery never double-counts


def test_retry_that_fails_again_stays_counted_once(tmp_path: Path):
    index = _index_from(_failing_log(tmp_path))
    index.apply(
        TailRecord(
            0,
            1,
            row(
                "fetch",
                1,
                20.0,
                status="error",
                error={"type": "Boom", "msg": "kaboom"},
            ),
        )
    )
    snap = index.snapshot()
    assert snap["stats"]["errors"] == 3
    groups = {g["type"]: g for g in snap["error_groups"]}
    assert groups["Boom"]["count"] == 2
    assert groups["Boom"]["rows"] == [[1, 1], [4, 4]]


def test_retry_failing_with_a_different_type_switches_group(tmp_path: Path):
    index = _index_from(_failing_log(tmp_path))
    index.apply(
        TailRecord(
            0,
            1,
            row(
                "fetch",
                1,
                20.0,
                status="error",
                error={"type": "Other", "msg": "different"},
            ),
        )
    )
    snap = index.snapshot()
    groups = {g["type"]: g for g in snap["error_groups"]}
    assert groups["Boom"]["rows"] == [[4, 4]]
    assert groups["Other"]["rows"] == [[1, 2]]  # rows 1 and 2, one range
    assert snap["stats"]["errors"] == 3
    assert sum(g["count"] for g in snap["error_groups"]) == 3


def test_unknown_record_types_are_ignored(tmp_path: Path):
    """retry_start/retry_end frame a retry segment; v1 consumers skip them."""
    index = _index_from(_failing_log(tmp_path))
    before = index.snapshot()

    index.apply(
        TailRecord(
            0,
            1,
            {
                "v": 1,
                "t": 19.0,
                "type": "retry_start",
                "run_id": "heal-run",
                "started_at": "2026-08-20T10:05:00+00:00",
                "num_rows": 6,
                "num_cells": 2,
                "cells": [{"step": "fetch", "row": 1}, {"step": "fetch", "row": 4}],
            },
        )
    )
    index.apply(TailRecord(0, 1, row("fetch", 1, 20.0, values={"company": "c1"})))
    index.apply(
        TailRecord(
            0,
            1,
            {
                "v": 1,
                "t": 21.0,
                "type": "retry_end",
                "num_rows": 6,
                "total_errors": 0,
                "cost": {"in": 0, "out": 0, "cost": None},
                "elapsed_s": 2.0,
                "num_cells": 2,
            },
        )
    )
    index.apply(TailRecord(0, 1, {"v": 1, "t": 22.0, "type": "some_future_record"}))

    after = index.snapshot()
    assert after["stats"]["errors"] == before["stats"]["errors"] - 1
    assert after["run"]["id"] == before["run"]["id"]
    assert [s["total"] for s in after["steps"]] == [s["total"] for s in before["steps"]]


def test_retry_segment_step_start_does_not_shrink_the_total(tmp_path: Path):
    index = _index_from(_failing_log(tmp_path))
    index.apply(
        TailRecord(
            0,
            1,
            {
                "v": 1,
                "t": 19.5,
                "type": "step_start",
                "step": "fetch",
                "level": 0,
                "mode": "realtime",
                "num_rows": 2,  # only the retried cells
            },
        )
    )
    step = index.snapshot()["steps"][0]
    assert (step["total"], step["done"]) == (6, 6)


# ---- POST /api/retry ------------------------------------------------------


def _retry_client(log: Path, write_module, **kwargs: Any):
    mod = write_module(FAKE_PIPELINE)
    return mod, client_for(log, pipeline=f"{mod}:make", **kwargs)


def test_post_retry_is_409_without_a_pipeline(tmp_path: Path):
    log = _failing_log(tmp_path)
    with client_for(log) as client:
        resp = client.post("/api/retry", json={"all": True}, headers=ORIGIN)
    assert resp.status_code == 409
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "launched without --pipeline"
    assert body["running"] is False


def test_run_snapshot_reports_retry_available(tmp_path: Path, write_module):
    log = _failing_log(tmp_path)
    mod, client = _retry_client(log, write_module)
    with client:
        block = client.get("/api/run").json()["retry"]
    assert block == {
        "available": True,
        "reason": None,
        "resume_command": f"accrue-ui {log} --pipeline {mod}:make",
        "running": False,
        "last_error": None,
    }


def test_post_retry_all_accepts_every_failed_row(tmp_path: Path, write_module):
    log = _failing_log(tmp_path)
    mod, client = _retry_client(log, write_module)
    with client:
        resp = client.post("/api/retry", json={"all": True}, headers=ORIGIN)
        assert resp.status_code == 202
        assert resp.json() == {"accepted": 3}
        _wait_until(lambda: not client.get("/api/run").json()["retry"]["running"])
    call = sys.modules[mod].CALLS[0]
    assert call["rows"] == [1, 2, 4]
    assert call["steps"] is None
    assert call["run_log"] == str(log)
    assert call["display_key"] == "company"  # from the log header
    assert call["data"] == sys.modules[mod].rows


def test_post_retry_group_resolves_rows_and_pins_the_step(tmp_path: Path, write_module):
    log = _failing_log(tmp_path)
    mod, client = _retry_client(log, write_module)
    with client:
        resp = client.post(
            "/api/retry",
            json={"group": {"step": "fetch", "type": "Boom"}},
            headers=ORIGIN,
        )
        assert resp.status_code == 202
        assert resp.json() == {"accepted": 2}
        _wait_until(lambda: not client.get("/api/run").json()["retry"]["running"])
    call = sys.modules[mod].CALLS[0]
    assert call["rows"] == [1, 4]
    assert call["steps"] == ["fetch"]


def test_post_retry_unknown_group_404(tmp_path: Path, write_module):
    log = _failing_log(tmp_path)
    _mod, client = _retry_client(log, write_module)
    with client:
        resp = client.post(
            "/api/retry",
            json={"group": {"step": "fetch", "type": "Ghost"}},
            headers=ORIGIN,
        )
    assert resp.status_code == 404
    assert "no error group" in resp.json()["detail"]


def test_post_retry_explicit_rows(tmp_path: Path, write_module):
    log = _failing_log(tmp_path)
    mod, client = _retry_client(log, write_module)
    with client:
        resp = client.post("/api/retry", json={"rows": [4, 1, 4]}, headers=ORIGIN)
        assert resp.status_code == 202
        assert resp.json() == {"accepted": 2}  # deduped and sorted
        _wait_until(lambda: not client.get("/api/run").json()["retry"]["running"])
    assert sys.modules[mod].CALLS[0]["rows"] == [1, 4]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"rows": [1], "all": True},
        {"rows": []},
        {"rows": "1,2"},
        {"rows": [1.5]},
        {"all": False},
        {"group": {"type": "Boom"}},
        [1, 2],
    ],
)
def test_post_retry_rejects_bad_selectors(tmp_path: Path, write_module, body: Any):
    log = _failing_log(tmp_path)
    _mod, client = _retry_client(log, write_module)
    with client:
        resp = client.post("/api/retry", json=body, headers=ORIGIN)
    assert resp.status_code == 400


def test_post_retry_rejects_a_non_json_body(tmp_path: Path, write_module):
    log = _failing_log(tmp_path)
    _mod, client = _retry_client(log, write_module)
    with client:
        resp = client.post(
            "/api/retry",
            content=b"not json",
            headers={**ORIGIN, "Content-Type": "application/json"},
        )
    assert resp.status_code == 400


def test_post_retry_with_nothing_failed_is_400(tmp_path: Path, write_module):
    log = tmp_path / "clean.jsonl"
    write_log(log, [*_records(2), row("fetch", 0, 1.0), row("fetch", 1, 1.1)])
    age_file(log)
    _mod, client = _retry_client(log, write_module)
    with client:
        resp = client.post("/api/retry", json={"all": True}, headers=ORIGIN)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "no failed rows to retry"


def test_only_one_retry_runs_at_a_time(tmp_path: Path, write_module):
    log = _failing_log(tmp_path)
    mod, client = _retry_client(log, write_module)
    module = None
    with client:
        module = sys.modules[mod]
        module.HOLD["on"] = True  # first retry blocks until we release it
        try:
            first = client.post("/api/retry", json={"all": True}, headers=ORIGIN)
            assert first.status_code == 202
            _wait_until(lambda: client.get("/api/run").json()["retry"]["running"])

            second = client.post("/api/retry", json={"rows": [1]}, headers=ORIGIN)
            assert second.status_code == 409
            body = second.json()
            assert body["reason"] == "retry already running"
            assert body["running"] is True
            assert body["available"] is True
        finally:
            module.HOLD["on"] = False
        _wait_until(lambda: not client.get("/api/run").json()["retry"]["running"])

        # ...and once it finishes, the next one is accepted.
        third = client.post("/api/retry", json={"rows": [1]}, headers=ORIGIN)
        assert third.status_code == 202
        _wait_until(lambda: not client.get("/api/run").json()["retry"]["running"])
    assert len(module.CALLS) == 2  # the 409 never reached the pipeline


def test_retry_task_failure_surfaces_as_last_error(tmp_path: Path, write_module):
    log = _failing_log(tmp_path)
    mod, client = _retry_client(log, write_module)
    with client:
        sys.modules[mod].BOOM["on"] = True
        resp = client.post("/api/retry", json={"all": True}, headers=ORIGIN)
        assert resp.status_code == 202
        _wait_until(lambda: not client.get("/api/run").json()["retry"]["running"])
        block = client.get("/api/run").json()["retry"]
    assert block["last_error"] == "RuntimeError: pipeline exploded"
    assert block["available"] is True  # still usable; the run just failed


def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting on the retry")


# ---- integration: a real accrue pipeline heals a real run -----------------

REAL_PIPELINE = textwrap.dedent("""
    import os
    from pathlib import Path

    from accrue import EnrichmentConfig, FunctionStep, Pipeline

    FAIL_ROWS = {2, 5}
    NUM_ROWS = 8


    def _index_of(ctx):
        return int(ctx.row["company"].split("-")[1])


    def _normalize(ctx):
        return {"name_upper": ctx.row["company"].upper()}


    def _score(ctx):
        i = _index_of(ctx)
        if i in FAIL_ROWS and not Path(os.environ["HEAL_FLAG"]).exists():
            raise ValueError(f"cannot score {ctx.row['company']}")
        return {"score": i * 10}


    PIPELINE = Pipeline([
        FunctionStep("normalize", _normalize, fields=["name_upper"]),
        FunctionStep("score", _score, fields=["score"], depends_on=["normalize"]),
    ])


    def build_rows():
        return [{"company": f"company-{i:02d}"} for i in range(NUM_ROWS)]


    def build_config():
        return EnrichmentConfig(
            enable_checkpointing=True,
            checkpoint_dir=os.environ["CKPT_DIR"],
            enable_caching=False,
            enable_progress_bar=False,
            max_workers=1,
        )


    def target():
        return (PIPELINE, build_rows(), build_config())
""")


def test_real_pipeline_retry_heals_the_run(
    tmp_path: Path, write_module, monkeypatch: pytest.MonkeyPatch
):
    """The whole loop: a run fails 2 rows, POST /api/retry, the cells heal."""
    heal_flag = tmp_path / "healed.flag"
    monkeypatch.setenv("HEAL_FLAG", str(heal_flag))
    monkeypatch.setenv("CKPT_DIR", str(tmp_path / "ckpt"))
    mod = write_module(REAL_PIPELINE)
    module = importlib.import_module(mod)

    log = tmp_path / "real.jsonl"
    result = module.PIPELINE.run(
        module.build_rows(), config=module.build_config(), run_log=str(log)
    )
    assert len(result.errors) == 2

    with client_for(log, pipeline=f"{mod}:target") as client:
        snap = client.get("/api/run").json()
        assert snap["retry"]["available"] is True
        assert snap["stats"]["errors"] == 2
        assert [g["rows"] for g in snap["error_groups"]] == [[[2, 2], [5, 5]]]

        heal_flag.write_text("go")  # the step stops raising
        resp = client.post("/api/retry", json={"all": True}, headers=ORIGIN)
        assert resp.status_code == 202
        assert resp.json() == {"accepted": 2}

        def healed() -> bool:
            body = client.get("/api/run").json()
            return not body["retry"]["running"] and body["stats"]["errors"] == 0

        _wait_until(healed, timeout=30.0)

        final = client.get("/api/run").json()

    assert final["retry"]["last_error"] is None
    assert final["error_groups"] == []
    assert [s["errors"] for s in final["steps"]] == [0, 0]
    assert final["rows"] == {"total": 8, "done": 8}
    cells = base64.b64decode(final["cells"]["data"])
    nsteps = final["cells"]["steps"]
    score = [s["name"] for s in final["steps"]].index("score")
    assert [cells[r * nsteps + score] for r in (2, 5)] == [OK, OK]
    assert set(cells) == {OK}

    # The healed values are readable, and the retry framed its own segment.
    detail = client_detail = None
    with client_for(log, pipeline=f"{mod}:target") as client:
        detail = client.get("/api/cell/score/2").json()
        client_detail = client.get("/api/values?start=2&count=1").json()
    assert detail["status"] == "ok"
    assert detail["values"] == {"score": 20}
    assert len(detail["raw_events"]) == 2  # the failure and the heal
    assert client_detail["rows"][0]["cells"]["score"]["s"] == OK

    types = [json.loads(line)["type"] for line in log.read_text().splitlines()]
    assert "retry_start" in types
    assert "retry_end" in types
    assert types.index("retry_start") > types.index("pipeline_end")
