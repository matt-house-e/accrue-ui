"""Generate ``run_retries.jsonl`` — a manifest-bearing run WITH injected retries.

The live/pinned runs have zero errors, so nothing exercises the Overview's
per-step retry chip (accrue-ui #22) or its dominant-bucket / breakdown-tooltip
rendering. This builds a small but *realistic* run for that: two LLM steps
(classify -> assess), each carrying a row-independent ``system_prompt`` (accrue
#140) and a ``step_end.elapsed_s`` wall-clock (#21), with a handful of cells
retried — every retried cell still ends ``ok``, so the failed-attempt buckets
reconcile 1:1 with ``attempt > 1`` (the inspector's per-cell timelines).

Deliberately **metadata tier** (``config.capture = "metadata"``, no prompt
sidecar, every ``prompt_ref`` null): it proves the retry aggregation renders
without captured bodies — ``row_attempt`` is emitted at every capture tier.

Deterministic: no clock, no randomness. Re-run to regenerate:

    python tests/fixtures/contract/gen_run_retries.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).with_name("run_retries.jsonl")

CLASSIFY_SYS = (
    "# Role\n"
    "You are a structured data enrichment engine. Given one input row and a set\n"
    "of field specifications, produce a JSON object with exactly the requested\n"
    "fields as keys.\n\n"
    "# Output Rules\n"
    "- Return ONLY a single valid JSON object. No prose, no code fences.\n"
    "- Top-level keys MUST be exactly: category, hq_country\n"
    "- Keep outputs concise and information-dense.\n\n"
    "<field_specifications>\n"
    '<field name="category"><prompt>One short industry category.</prompt></field>\n'
    '<field name="hq_country"><prompt>Headquarters country, best guess.</prompt></field>\n'
    "</field_specifications>"
)
ASSESS_SYS = (
    "# Role\n"
    "You are a B2B account analyst. Given the row and the classification from the\n"
    "prior step, score the account and write a one-line rationale.\n\n"
    "# Output Rules\n"
    "- Return ONLY a single valid JSON object. Top-level keys: one_liner, icp_fit.\n"
    "- icp_fit is one of: strong, good, weak.\n"
)

N_ROWS = 12
KEYS = [
    "stripe.com",
    "figma.com",
    "vercel.com",
    "linear.app",
    "notion.so",
    "ramp.com",
    "retool.com",
    "airtable.com",
    "amplitude.com",
    "segment.com",
    "datadog.com",
    "snowflake.com",
]

# Injected retry plans, per step -> {row: [(kind, status), ...]} for the failing
# attempts that precede the successful one. Each row here ends ``ok`` after the
# listed failures, so retries == failed attempts for the step.
RETRY_PLANS = {
    "classify": {
        0: [("api", "rate_limited")],
        1: [("parse", "parse_error")],
        2: [("api", "rate_limited")],
        3: [("api", "rate_limited"), ("api", "timeout")],
        4: [("api", "rate_limited")],
    },
    "assess": {
        0: [("api", "timeout")],
        1: [("api", "timeout")],
    },
}
# Rows served from cache (settle in ~0ms; MUST be excluded from p50/p95).
CACHED = {"classify": {10, 11}, "assess": {10, 11}}

STEPS = [
    {
        "name": "classify",
        "level": 0,
        "model": {
            "id": "google/gemini-3.5-flash-lite",
            "provider": "openrouter",
            "temperature": 0.2,
            "max_tokens": 4000,
        },
        "produces": ["category", "hq_country"],
        "depends_on": [],
        "system_prompt": CLASSIFY_SYS,
        "wall_s": 12.53,
        "base_ms": 700.0,
    },
    {
        "name": "assess",
        "level": 1,
        "model": {
            "id": "anthropic/claude-haiku-4",
            "provider": "anthropic",
            "temperature": 0.0,
            "max_tokens": 512,
        },
        "produces": ["one_liner", "icp_fit"],
        "depends_on": ["classify"],
        "system_prompt": ASSESS_SYS,
        "wall_s": 6.0,
        "base_ms": 480.0,
    },
]


def _manifest() -> dict:
    return {
        "accrue_version": "1.3.0",
        "config": {
            "max_workers": 6,
            "caching": True,
            "checkpointing": True,
            "batch": False,
            "capture": "metadata",
        },
        "steps": [
            {
                "name": s["name"],
                "type": "LLMStep",
                "model": s["model"],
                "produces": s["produces"],
                "depends_on": s["depends_on"],
                "condition": None,
                "system_prompt": s["system_prompt"],
            }
            for s in STEPS
        ],
        "fields": [
            {
                "name": "category",
                "type": "str",
                "enum": None,
                "description": "One short industry category.",
                "step": "classify",
                "internal": False,
            },
            {
                "name": "hq_country",
                "type": "str",
                "enum": None,
                "description": "Headquarters country.",
                "step": "classify",
                "internal": False,
            },
            {
                "name": "one_liner",
                "type": "str",
                "enum": None,
                "description": "One-line rationale.",
                "step": "assess",
                "internal": False,
            },
            {
                "name": "icp_fit",
                "type": "enum",
                "enum": ["strong", "good", "weak"],
                "description": "Ideal-customer-profile fit.",
                "step": "assess",
                "internal": False,
            },
        ],
    }


def build() -> list[dict]:
    recs: list[dict] = []
    t = 0.0
    recs.append(
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": "run-retries",
            "started_at": "2026-08-21T12:00:00Z",
            "num_rows": N_ROWS,
            "display_key": "domain",
            "steps": [
                {
                    "name": s["name"],
                    "level": s["level"],
                    "mode": "realtime",
                    "model": s["model"]["id"],
                }
                for s in STEPS
            ],
            "manifest": _manifest(),
            "plan": None,
        }
    )

    for s in STEPS:
        name = s["name"]
        t += 0.01
        recs.append(
            {
                "v": 1,
                "t": round(t, 3),
                "type": "step_start",
                "step": name,
                "level": s["level"],
                "mode": "realtime",
                "num_rows": N_ROWS,
            }
        )
        plans = RETRY_PLANS.get(name, {})
        cached = CACHED.get(name, set())
        for r in range(N_ROWS):
            cached_row = r in cached
            fails = plans.get(r, [])
            attempt = 0
            for kind, status in fails:
                attempt += 1
                t += 0.02
                recs.append(
                    {
                        "v": 1,
                        "t": round(t, 3),
                        "type": "row_attempt",
                        "step": name,
                        "row": r,
                        "attempt": attempt,
                        "kind": kind,
                        "status": status,
                        "latency_ms": 120.0,
                        "backoff_s": 2.0 if kind == "api" else None,
                        "error": {
                            "type": "LLMAPIError"
                            if kind == "api"
                            else "JSONDecodeError",
                            "msg": status.replace("_", " "),
                        },
                        "prompt_ref": None,  # metadata tier: no captured body
                    }
                )
            # the settling (ok) attempt
            attempt += 1
            t += 0.02
            recs.append(
                {
                    "v": 1,
                    "t": round(t, 3),
                    "type": "row_attempt",
                    "step": name,
                    "row": r,
                    "attempt": attempt,
                    "kind": "parse",
                    "status": "ok",
                    "latency_ms": 90.0,
                    "backoff_s": None,
                    "error": None,
                    "prompt_ref": None,
                }
            )
            # row_complete: cached rows settle at ~0ms (excluded from p50/p95).
            elapsed = 0.4 if cached_row else s["base_ms"] + r * 50.0
            vals = (
                {"category": "software", "hq_country": "United States"}
                if name == "classify"
                else {"one_liner": "Fast-growing dev tool.", "icp_fit": "strong"}
            )
            recs.append(
                {
                    "v": 1,
                    "t": round(t, 3),
                    "type": "row_complete",
                    "step": name,
                    "row": r,
                    "key": KEYS[r],
                    "status": "ok",
                    "from_cache": cached_row,
                    "values": vals,
                    "error": None,
                    "usage": {"in": 300, "out": 30, "cost": None},
                    "elapsed_ms": round(elapsed, 3),
                }
            )
        t = max(t, s["wall_s"])
        recs.append(
            {
                "v": 1,
                "t": round(t, 3),
                "type": "step_end",
                "step": name,
                "num_errors": 0,
                "usage": {"in": 3600, "out": 360, "cost": None},
                "elapsed_s": s["wall_s"],
                "batch_id": None,
            }
        )

    recs.append(
        {
            "v": 1,
            "t": round(max(t, 19.0), 3),
            "type": "pipeline_end",
            "num_rows": N_ROWS,
            "total_errors": 0,
            "cost": {"in": 7200, "out": 720, "cost": None},
            "elapsed_s": 19.0,
        }
    )
    return recs


def main() -> None:
    recs = build()
    with open(OUT, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in recs)
    print(f"wrote {len(recs)} records to {OUT}")


if __name__ == "__main__":
    main()
