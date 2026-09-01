"""Overview data: pipeline_start.manifest parsing + graceful empty-state.

The manifest fixture (tests/fixtures/contract/run_manifest.jsonl) is a 12-row,
two-LLMStep run (classify -> assess) whose pipeline_start carries the v1
`manifest` — step defs, run config, and the enrichment-field schema (with an
`enum` field). run_small.jsonl carries no manifest and exercises the degrade
path. The overview rides the /api/run snapshot, so the existing launch-token +
Origin/Host middleware guards it exactly like every other /api/* payload.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.conftest import age_file, client_for, row, write_log

MANIFEST_FIXTURE = (
    Path(__file__).parent / "fixtures" / "contract" / "run_manifest.jsonl"
)
RETRIES_FIXTURE = Path(__file__).parent / "fixtures" / "contract" / "run_retries.jsonl"


@pytest.fixture
def manifest_log(tmp_path: Path) -> Path:
    """The manifest-bearing fixture, copied to tmp (with its sidecar), aged."""
    dst = tmp_path / "run_manifest.jsonl"
    shutil.copy(MANIFEST_FIXTURE, dst)
    shutil.copy(
        MANIFEST_FIXTURE.with_suffix(".prompts.jsonl"),
        dst.with_suffix(".prompts.jsonl"),
    )
    age_file(dst)
    return dst


@pytest.fixture
def retries_log(tmp_path: Path) -> Path:
    """A manifest run with injected retries (tests/fixtures/contract), aged.

    Metadata tier (no sidecar): two LLM steps with system prompts, per-step
    wall-clock, cached rows, and a handful of retried cells that all settle ok
    (see gen_run_retries.py for the exact injections)."""
    dst = tmp_path / "run_retries.jsonl"
    shutil.copy(RETRIES_FIXTURE, dst)
    age_file(dst)
    return dst


def test_overview_present_config_and_meta(manifest_log: Path):
    with client_for(manifest_log) as client:
        ov = client.get("/api/run").json()["overview"]
    assert ov["present"] is True
    assert ov["accrue_version"] == "1.3.0"
    assert ov["config"] == {
        "max_workers": 6,
        "caching": False,
        "checkpointing": True,
        "batch": False,
        "capture": "prompts",
    }
    assert ov["sample_size"] == 12
    assert ov["providers"] == ["openrouter"]


def test_overview_steps_definition_and_live_outcome(manifest_log: Path):
    with client_for(manifest_log) as client:
        body = client.get("/api/run").json()
    ov = body["overview"]
    assert [s["name"] for s in ov["steps"]] == ["classify", "assess"]
    classify, assess = ov["steps"]

    assert classify["type"] == "LLMStep"
    assert classify["model"] == {
        "id": "google/gemini-3.5-flash-lite",
        "provider": "openrouter",
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    assert classify["produces"] == ["category", "hq_country"]
    assert classify["depends_on"] == []
    assert classify["condition"] is None
    assert classify["level"] == 0
    assert classify["mode"] == "live"
    # depends_on / level carry the DAG shape.
    assert assess["depends_on"] == ["classify"]
    assert assess["level"] == 1

    # Each step's live outcome mirrors the live step counters and cost.by_step —
    # blueprint and running numbers never diverge.
    live_by_name = {s["name"]: s for s in body["steps"]}
    for s in ov["steps"]:
        live = live_by_name[s["name"]]
        assert s["outcome"]["done"] == live["done"] == 12
        assert s["outcome"]["total"] == live["total"] == 12
        assert s["outcome"]["errors"] == live["errors"] == 0
        assert s["outcome"]["cost"] == body["cost"]["by_step"][s["name"]]


def test_overview_field_schema_with_enum(manifest_log: Path):
    with client_for(manifest_log) as client:
        fields = client.get("/api/run").json()["overview"]["fields"]
    assert [f["name"] for f in fields] == [
        "category",
        "hq_country",
        "one_liner",
        "icp_fit",
    ]
    by_name = {f["name"]: f for f in fields}
    assert by_name["category"]["type"] == "str"
    assert by_name["category"]["enum"] is None
    assert by_name["category"]["step"] == "classify"
    assert by_name["category"]["internal"] is False
    icp = by_name["icp_fit"]
    assert icp["type"] == "enum"
    assert icp["enum"] == ["strong", "good", "weak"]
    assert icp["step"] == "assess"
    assert icp["description"]  # the schema carries the field description


def test_overview_absent_degrades_not_crashes(golden_log: Path):
    """A log with no manifest (older/metadata runs) reports present:false."""
    with client_for(golden_log) as client:
        ov = client.get("/api/run").json()["overview"]
    assert ov == {"present": False}


def test_overview_behind_launch_token(manifest_log: Path):
    """The overview rides /api/run: the same launch-token guards it (401 without)."""
    with client_for(manifest_log, authed=False) as client:
        assert client.get("/api/run").status_code == 401


def _synthetic_manifest_records() -> list[dict]:
    """A function step (null model) + a conditional LLM step + an internal field."""
    return [
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": "synth-manifest",
            "started_at": "2026-08-21T10:00:00Z",
            "num_rows": 2,
            "display_key": "domain",
            "steps": [
                {"name": "normalize", "level": 0, "mode": "realtime", "model": None},
                {
                    "name": "classify",
                    "level": 1,
                    "mode": "realtime",
                    "model": "anthropic/claude-haiku",
                },
            ],
            "manifest": {
                "accrue_version": "1.3.0",
                "config": {
                    "max_workers": 4,
                    "caching": True,
                    "checkpointing": False,
                    "batch": False,
                    "capture": "metadata",
                },
                "steps": [
                    {
                        "name": "normalize",
                        "type": "FunctionStep",
                        "model": None,
                        "produces": ["__norm"],
                        "depends_on": [],
                        "condition": None,
                    },
                    {
                        "name": "classify",
                        "type": "LLMStep",
                        "model": {
                            "id": "anthropic/claude-haiku",
                            "provider": "anthropic",
                            "temperature": 0.0,
                            "max_tokens": 256,
                        },
                        "produces": ["category"],
                        "depends_on": ["normalize"],
                        "condition": "category is None",
                    },
                ],
                "fields": [
                    {
                        "name": "__norm",
                        "type": "unknown",
                        "enum": None,
                        "description": None,
                        "step": "normalize",
                        "internal": True,
                    },
                    {
                        "name": "category",
                        "type": "str",
                        "enum": None,
                        "description": "One short industry category.",
                        "step": "classify",
                        "internal": False,
                    },
                ],
            },
            "plan": None,
        },
        row("normalize", 0, 1.0, values={"__norm": "x"}),
    ]


def test_overview_function_step_and_internal_field(tmp_path: Path):
    log = tmp_path / "synth.jsonl"
    write_log(log, _synthetic_manifest_records())
    age_file(log)
    with client_for(log) as client:
        ov = client.get("/api/run").json()["overview"]

    # A function step's null model is neither shown nor counted as a provider.
    assert ov["providers"] == ["anthropic"]
    normalize, classify = ov["steps"]
    assert normalize["type"] == "FunctionStep"
    assert normalize["model"] is None
    assert normalize["outcome"]["cost"] is None  # no priceable model -> null
    assert classify["condition"] == "category is None"

    by_name = {f["name"]: f for f in ov["fields"]}
    assert by_name["__norm"]["internal"] is True
    # A type accrue could not introspect stays "unknown" (rendered plainly),
    # never hidden.
    assert by_name["__norm"]["type"] == "unknown"

    # A manifest that predates #140 (no system_prompt key) yields null, not a
    # crash — the panel is simply omitted.
    assert normalize["system_prompt"] is None
    assert classify["system_prompt"] is None


# --- P21: per-step timing (wall-clock + per-row p50/p95, cached excluded) ----


def test_overview_timing_wall_clock_and_percentiles(retries_log: Path):
    with client_for(retries_log) as client:
        ov = client.get("/api/run").json()["overview"]
    # Total pipeline wall-clock (pipeline_end.elapsed_s) surfaced on the block.
    assert ov["pipeline_wall_s"] == 19.0
    classify, assess = ov["steps"]

    c = classify["outcome"]
    assert c["ended"] is True
    assert c["wall_s"] == 12.53  # step_end.elapsed_s, the headline duration
    # p50/p95 from row_complete.elapsed_ms with the 2 cached rows EXCLUDED:
    # the 10 non-cached rows are 700..1150ms (700 + 50*row).
    assert c["latency_ms"] == {"p50": 925.0, "p95": 1127.5, "n": 10}

    a = assess["outcome"]
    assert a["wall_s"] == 6.0
    assert a["latency_ms"]["n"] == 10  # assess also has 2 cached rows dropped


def test_overview_cached_rows_excluded_from_percentiles(retries_log: Path):
    """The cached-exclusion is load-bearing: including the ~0ms cached rows
    would drag both percentiles down and understate real call latency."""
    with client_for(retries_log) as client:
        ov = client.get("/api/run").json()["overview"]
    classify = ov["steps"][0]["outcome"]
    # 12 rows, 2 cached -> n is 10, and p50 sits at the middle of 700..1150,
    # not near the cached 0.4ms floor.
    assert classify["latency_ms"]["n"] == 10
    assert classify["latency_ms"]["p50"] > 800


def test_overview_batch_step_has_wall_clock_but_no_percentiles(tmp_path: Path):
    """Batch steps: wall-clock only. Their per-row elapsed includes provider
    queue time, so a per-row latency there would mislead (latency_ms is null)."""
    records = [
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": "batch-run",
            "started_at": "2026-08-21T10:00:00Z",
            "num_rows": 2,
            "steps": [{"name": "summarize", "level": 0, "mode": "batch", "model": "m"}],
            "manifest": {
                "accrue_version": "1.3.0",
                "config": {"batch": True, "capture": "metadata"},
                "steps": [
                    {
                        "name": "summarize",
                        "type": "LLMStep",
                        "model": {"id": "m", "provider": "p"},
                        "produces": ["summary"],
                        "depends_on": [],
                        "condition": None,
                        "system_prompt": "Summarize the row.",
                    }
                ],
                "fields": [],
            },
            "plan": None,
        },
        {
            "v": 1,
            "t": 0.1,
            "type": "step_start",
            "step": "summarize",
            "level": 0,
            "mode": "batch",
            "num_rows": 2,
        },
        row("summarize", 0, 1.0, values={"summary": "a"}, elapsed_ms=30000.0),
        row("summarize", 1, 2.0, values={"summary": "b"}, elapsed_ms=42000.0),
        {
            "v": 1,
            "t": 3.0,
            "type": "step_end",
            "step": "summarize",
            "num_errors": 0,
            "elapsed_s": 50.0,
            "batch_id": "b1",
        },
        {"v": 1, "t": 3.0, "type": "pipeline_end", "elapsed_s": 50.0},
    ]
    log = tmp_path / "batch.jsonl"
    write_log(log, records)
    age_file(log)
    with client_for(log) as client:
        step = client.get("/api/run").json()["overview"]["steps"][0]
    assert step["mode"] == "batch"
    assert step["outcome"]["wall_s"] == 50.0  # wall-clock still shown
    assert step["outcome"]["latency_ms"] is None  # no misleading per-row latency


def test_overview_in_progress_step_has_no_final_duration(tmp_path: Path):
    """A step with no step_end yet reports ended:false and wall_s:null, so the
    card shows a running state rather than a final duration."""
    records = [
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": "live-run",
            "started_at": "2026-08-21T10:00:00Z",
            "num_rows": 4,
            "steps": [
                {"name": "classify", "level": 0, "mode": "realtime", "model": "m"}
            ],
            "manifest": {
                "accrue_version": "1.3.0",
                "config": {"capture": "metadata"},
                "steps": [
                    {
                        "name": "classify",
                        "type": "LLMStep",
                        "model": {"id": "m", "provider": "p"},
                        "produces": ["c"],
                        "depends_on": [],
                        "condition": None,
                        "system_prompt": "Classify.",
                    }
                ],
                "fields": [],
            },
            "plan": None,
        },
        {
            "v": 1,
            "t": 0.1,
            "type": "step_start",
            "step": "classify",
            "level": 0,
            "mode": "realtime",
            "num_rows": 4,
        },
        row("classify", 0, 1.0, values={"c": "x"}, elapsed_ms=500.0),
        row("classify", 1, 2.0, values={"c": "y"}, elapsed_ms=700.0),
        # no step_end, no pipeline_end: the step is still in progress.
    ]
    log = tmp_path / "live.jsonl"
    write_log(log, records)
    age_file(log)
    with client_for(log) as client:
        ov = client.get("/api/run").json()["overview"]
    step = ov["steps"][0]["outcome"]
    assert step["ended"] is False
    assert step["wall_s"] is None
    # The percentiles still compute mid-run from the rows that did finish.
    assert step["latency_ms"]["n"] == 2
    # No step_end anywhere -> no pipeline_end either.
    assert ov["pipeline_wall_s"] is None


# --- P22: per-step retry / rate-limit volume --------------------------------


def test_overview_retry_aggregation_and_dominant_bucket(retries_log: Path):
    with client_for(retries_log) as client:
        ov = client.get("/api/run").json()["overview"]
    classify, assess = ov["steps"]

    cr = classify["outcome"]["retry"]
    # retries = attempts with attempt > 1: rows 0,1,2,4 retried once, row 3
    # twice -> 6. Every retried cell settled ok, so failed attempts == retries.
    assert cr["count"] == 6
    assert cr["by_status"] == {"rate_limited": 4, "parse_error": 1, "timeout": 1}
    assert cr["by_kind"] == {"api": 5, "parse": 1}
    assert cr["dominant"] == {"status": "rate_limited", "count": 4}

    ar = assess["outcome"]["retry"]
    assert ar["count"] == 2
    assert ar["dominant"] == {"status": "timeout", "count": 2}
    assert ar["by_kind"] == {"api": 2}


def test_overview_retry_omitted_when_zero(manifest_log: Path):
    """A step that never retried reports retry:null so the card omits the chip
    (the manifest fixture runs clean — 24 attempts, all attempt 1)."""
    with client_for(manifest_log) as client:
        ov = client.get("/api/run").json()["overview"]
    for step in ov["steps"]:
        assert step["outcome"]["retry"] is None


def test_overview_retry_works_at_metadata_tier(retries_log: Path):
    """row_attempt is emitted even at capture=metadata; the aggregation must
    NOT be gated on capture=prompts (there is no sidecar here)."""
    with client_for(retries_log) as client:
        ov = client.get("/api/run").json()["overview"]
    assert ov["config"]["capture"] == "metadata"
    assert ov["steps"][0]["outcome"]["retry"]["count"] == 6


def test_overview_retry_reconciles_with_inspector_timelines(retries_log: Path):
    """The card's retry count must equal the attempts-beyond-first the per-cell
    inspector timelines show — same source, no divergence."""
    with client_for(retries_log) as client:
        ov = client.get("/api/run").json()["overview"]
        beyond_first = 0
        for r in range(12):
            cell = client.get(f"/api/cell/classify/{r}").json()
            attempts = cell["attempts"] or []
            beyond_first += sum(1 for a in attempts if a["attempt"] > 1)
    assert beyond_first == ov["steps"][0]["outcome"]["retry"]["count"] == 6


# --- accrue #140: row-independent system-prompt panel (display side) ---------


def test_overview_system_prompt_carried_per_step(retries_log: Path):
    with client_for(retries_log) as client:
        steps = client.get("/api/run").json()["overview"]["steps"]
    classify, assess = steps
    assert isinstance(classify["system_prompt"], str)
    assert "structured data enrichment engine" in classify["system_prompt"]
    assert classify["system_prompt"] != assess["system_prompt"]  # per-step
    assert "B2B account analyst" in assess["system_prompt"]
