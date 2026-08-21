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
