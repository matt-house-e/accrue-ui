"""v0.2 capture-tier contract: real per-attempt timelines + prompt bodies.

Runs against the pinned `run_captured.jsonl` / `.prompts.jsonl` pair (see
tests/fixtures/contract/README.md): a 3-row classify run captured with
`capture="prompts"`, with a rate-limited api retry on row 1 and a parse_error
retry on row 2. These are the cross-repo handshake for accrue-ui #14 — if the
row_attempt / prompt_ref shapes change in accrue, they break here first.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from accrue_ui.server.index import RETRYING, RunIndex
from accrue_ui.server.tail import TailRecord
from tests.conftest import age_file, client_for, row, write_log

CONTRACT = Path(__file__).parent / "fixtures" / "contract"


@pytest.fixture
def captured_log(tmp_path: Path) -> Path:
    """The captured pair copied to tmp (log + sidecar together), mtime aged."""
    dst = tmp_path / "run_captured.jsonl"
    shutil.copy(CONTRACT / "run_captured.jsonl", dst)
    shutil.copy(
        CONTRACT / "run_captured.prompts.jsonl", tmp_path / "run_captured.prompts.jsonl"
    )
    age_file(dst)
    return dst


def _attempt(
    step: str,
    row_i: int,
    t: float,
    attempt_n: int,
    kind: str,
    status: str,
    *,
    backoff: float | None = None,
    error: dict[str, Any] | None = None,
    prompt_ref: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "v": 1,
        "t": t,
        "type": "row_attempt",
        "step": step,
        "row": row_i,
        "attempt": attempt_n,
        "kind": kind,
        "status": status,
        "latency_ms": 1.0,
        "backoff_s": backoff,
        "error": error,
        "prompt_ref": prompt_ref,
    }


# --- P21: attempt count reflects the real number of tries -------------------


def test_row_without_retry_has_one_attempt(captured_log: Path):
    with client_for(captured_log) as client:
        d = client.get("/api/cell/classify/0").json()
    assert d["status"] == "ok"
    assert len(d["attempts"]) == 1
    a = d["attempts"][0]
    assert (a["n"], a["attempt"], a["kind"], a["status"]) == (1, 1, "parse", "ok")
    assert a["backoff_s"] is None and a["error"] is None


def test_rate_limited_api_retry_yields_two_attempts(captured_log: Path):
    """Row 1: an api attempt is rate-limited (with backoff) then a parse
    attempt succeeds — two row_attempt records, not the hard-coded 1 (P21)."""
    with client_for(captured_log) as client:
        d = client.get("/api/cell/classify/1").json()
    assert d["status"] == "ok"
    assert [a["kind"] for a in d["attempts"]] == ["api", "parse"]
    assert [a["status"] for a in d["attempts"]] == ["rate_limited", "ok"]
    first, second = d["attempts"]
    assert first["backoff_s"] is not None and first["backoff_s"] > 0
    assert first["error"] == {"type": "LLMAPIError", "msg": "rate limited"}
    assert first["prompt_ref"] is None  # api error carries no captured body
    assert second["prompt_ref"] == {"off": 1276, "len": 1283}


def test_parse_error_retry_yields_two_attempts(captured_log: Path):
    """Row 2: a parse_error attempt then a successful parse — both captured."""
    with client_for(captured_log) as client:
        d = client.get("/api/cell/classify/2").json()
    assert [a["kind"] for a in d["attempts"]] == ["parse", "parse"]
    assert [a["status"] for a in d["attempts"]] == ["parse_error", "ok"]
    assert d["attempts"][0]["error"]["type"] == "JSONDecodeError"
    # Both parse attempts captured a body; each points at a distinct span.
    offs = [a["prompt_ref"]["off"] for a in d["attempts"]]
    assert offs == sorted(offs) and len(set(offs)) == 2


# --- prompt bodies decode from the correct byte range -----------------------


def test_prompt_body_decodes_from_the_exact_sidecar_offset(captured_log: Path):
    """The resolved body is exactly what a seek to the last attempt's
    prompt_ref off/len yields — proving offset resolution, not a whole read."""
    with client_for(captured_log) as client:
        d = client.get("/api/cell/classify/1").json()
    assert d["capture_available"] is True
    prompt = d["prompt"]
    assert prompt is not None
    assert prompt["response"] == '{"grade": "B"}'
    assert prompt["parsed"] == {"grade": "B"}
    assert [m["role"] for m in prompt["messages"]] == ["system", "user"]

    # Independently seek the sidecar at the attempt's recorded span and match.
    ref = d["attempts"][-1]["prompt_ref"]
    with open(captured_log.with_suffix(".prompts.jsonl"), "rb") as f:
        f.seek(ref["off"])
        raw = f.read(ref["len"])
    assert json.loads(raw) == prompt


def test_every_captured_row_resolves_its_own_body(captured_log: Path):
    with client_for(captured_log) as client:
        bodies = {
            r: client.get(f"/api/cell/classify/{r}").json()["prompt"]["parsed"]
            for r in (0, 1, 2)
        }
    assert bodies == {0: {"grade": "A"}, 1: {"grade": "B"}, 2: {"grade": "C"}}


def test_raw_events_carry_attempts_and_completion_in_log_order(captured_log: Path):
    with client_for(captured_log) as client:
        d = client.get("/api/cell/classify/1").json()
    assert [r["type"] for r in d["raw_events"]] == [
        "row_attempt",
        "row_attempt",
        "row_complete",
    ]


# --- metadata tier: attempts still work, but no bodies ----------------------


def _metadata_records() -> list[dict[str, Any]]:
    return [
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": "meta-run",
            "started_at": "2026-08-20T18:00:00Z",
            "num_rows": 2,
            "display_key": "company",
            "steps": [
                {
                    "name": "classify",
                    "level": 0,
                    "mode": "realtime",
                    "model": "gpt-4.1-mini",
                }
            ],
            "plan": None,
        },
        {
            "v": 1,
            "t": 0.001,
            "type": "step_start",
            "step": "classify",
            "level": 0,
            "mode": "realtime",
            "num_rows": 2,
        },
        _attempt("classify", 0, 0.002, 1, "parse", "ok"),
        row("classify", 0, 0.003, status="ok", values={"grade": "A"}),
        _attempt(
            "classify",
            1,
            0.004,
            1,
            "api",
            "rate_limited",
            backoff=0.5,
            error={"type": "LLMAPIError", "msg": "rate limited"},
        ),
        _attempt("classify", 1, 0.005, 2, "parse", "ok"),
        row("classify", 1, 0.006, status="ok", values={"grade": "B"}),
        {
            "v": 1,
            "t": 0.007,
            "type": "step_end",
            "step": "classify",
            "num_errors": 0,
            "usage": None,
            "elapsed_s": 0.007,
            "batch_id": None,
        },
        {
            "v": 1,
            "t": 0.008,
            "type": "pipeline_end",
            "num_rows": 2,
            "total_errors": 0,
            "cost": None,
            "elapsed_s": 0.008,
        },
    ]


def test_metadata_only_run_has_attempts_but_no_bodies(tmp_path: Path):
    """No sidecar written: the timeline still renders from row_attempt records,
    but prompt is null and capture_available is false — the inspector's
    empty-state-with-hint path."""
    log = tmp_path / "meta-run.jsonl"
    write_log(log, _metadata_records())
    age_file(log)
    assert not log.with_suffix(".prompts.jsonl").exists()
    with client_for(log) as client:
        d = client.get("/api/cell/classify/1").json()
    assert d["capture_available"] is False
    assert d["prompt"] is None
    assert [a["kind"] for a in d["attempts"]] == ["api", "parse"]
    assert all(a["prompt_ref"] is None for a in d["attempts"])


def test_missing_sidecar_never_crashes_cell_detail(tmp_path: Path):
    """A prompt_ref pointing at a sidecar that is not there resolves to null,
    not an error."""
    records = _metadata_records()
    # Give row 1's parse attempt a prompt_ref, but write no sidecar.
    records[5]["prompt_ref"] = {"off": 0, "len": 40}
    log = tmp_path / "meta-run.jsonl"
    write_log(log, records)
    age_file(log)
    with client_for(log) as client:
        resp = client.get("/api/cell/classify/1")
    assert resp.status_code == 200
    assert resp.json()["prompt"] is None


# --- P22: the retrying state is driven by a real failed attempt -------------


def _rec(data: dict[str, Any]) -> TailRecord:
    return TailRecord(offset=0, length=0, data=data)


def test_failed_attempt_marks_the_cell_retrying_until_it_completes():
    """Before row_complete, a non-ok row_attempt flips the cell to RETRYING;
    the completion then settles it. This is the real retry event P22 wants,
    not a state inferred from error-preview text."""
    idx = RunIndex("nonexistent.jsonl")
    for data in _metadata_records()[:4]:  # start, step_start, row0 attempt+complete
        idx.apply(_rec(data))
    # Row 1's first attempt fails — mid-flight, no row_complete yet.
    idx.apply(
        _rec(_attempt("classify", 1, 0.004, 1, "api", "rate_limited", backoff=0.5))
    )
    nsteps = len(idx.steps)
    assert idx._cells[1 * nsteps + 0] == RETRYING

    # Its row_complete lands: the cell leaves retrying for its terminal state.
    idx.apply(_rec(row("classify", 1, 0.006, status="ok", values={"grade": "B"})))
    assert idx._cells[1 * nsteps + 0] != RETRYING
    assert idx.snapshot()["steps"][0]["done"] == 2


def test_ok_attempt_does_not_force_retrying():
    """A first-try success never shows as retrying — only a failed try does."""
    idx = RunIndex("nonexistent.jsonl")
    for data in _metadata_records()[:2]:
        idx.apply(_rec(data))
    idx.apply(_rec(_attempt("classify", 0, 0.002, 1, "parse", "ok")))
    nsteps = len(idx.steps)
    assert idx._cells[0 * nsteps + 0] != RETRYING
