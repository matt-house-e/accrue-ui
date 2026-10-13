"""Pricing table: known models price, unknown models stay null."""

from __future__ import annotations

from pathlib import Path

import pytest

from accrue_ui.server.index import RunIndex
from accrue_ui.server.pricing import (
    BATCH_DISCOUNT,
    PRICES,
    ModelPrice,
    lookup,
    price_usd,
)
from accrue_ui.server.tail import TailRecord
from tests.conftest import row

CURRENT_CLAUDE = [
    "claude-fable-5-1",
    "claude-fable-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-opus-4-1",
    "claude-opus-4-0",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-sonnet-4-0",
    "claude-haiku-5-5",
    "claude-haiku-4-5",
]


def test_known_models_price_per_mtok():
    # claude-sonnet-4-6: $3.00 in / $15.00 out per 1M tokens.
    assert price_usd("claude-sonnet-4-6", 1_000_000, 1_000_000) == pytest.approx(18.0)
    # claude-sonnet-5: $2.00 / $10.00 (launch price made standard).
    assert price_usd("claude-sonnet-5", 1_000_000, 1_000_000) == pytest.approx(12.0)
    # claude-haiku-4-5: $1.00 / $5.00.
    assert price_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == pytest.approx(6.0)
    # gpt-5.x-mini class: $0.25 / $2.00.
    assert price_usd("gpt-5.2-mini", 1_000_000, 1_000_000) == pytest.approx(2.25)


@pytest.mark.parametrize("model", CURRENT_CLAUDE)
def test_current_claude_models_carry_all_four_prices(model):
    price = PRICES[model]
    assert all(v > 0 for v in price)
    # Cache hits are always cheaper than input; 5-minute writes cost more.
    assert price.cache_read < price.input < price.cache_write


def test_claude_cache_prices_follow_the_published_multipliers():
    # Standard: hits at 0.1x input, 5-minute writes at 1.25x.
    assert PRICES["claude-sonnet-4-6"] == pytest.approx((3.00, 15.00, 0.30, 3.75))
    # The models whose cache hits are cheaper than 0.1x.
    assert PRICES["claude-fable-5-1"].cache_read == pytest.approx(0.25)
    assert PRICES["claude-opus-5-5"].cache_read == pytest.approx(0.20)
    assert PRICES["claude-sonnet-5-5"].cache_read == pytest.approx(0.10)


def test_cache_tokens_price_at_their_own_rates():
    # 1M cache reads + 1M cache writes on claude-sonnet-4-6: $0.30 + $3.75.
    usd = price_usd(
        "claude-sonnet-4-6", 0, 0, cache_read=1_000_000, cache_write=1_000_000
    )
    assert usd == pytest.approx(4.05)


def test_small_counts():
    assert price_usd("claude-sonnet-4-6", 1000, 100) == pytest.approx(0.0045)
    assert price_usd("claude-sonnet-4-6", 0, 0) == 0.0


def test_unknown_or_missing_model_is_none():
    assert price_usd("mystery-9", 1_000_000, 1_000_000) is None
    assert price_usd(None, 1_000_000, 1_000_000) is None
    assert price_usd("", 1_000_000, 1_000_000) is None


def test_batch_discount_halves():
    assert BATCH_DISCOUNT == 0.5
    full = price_usd("claude-haiku-4-5", 100_000, 10_000)
    batched = price_usd("claude-haiku-4-5", 100_000, 10_000, batch=True)
    assert full == pytest.approx(0.15)
    assert batched == pytest.approx(0.075)


def test_gateway_prefixed_and_gemini_models_are_priced():
    # vendor-prefixed id (OpenRouter etc.) resolves via the suffix
    assert price_usd("openai/gpt-5.2-mini", 1_000_000, 0) == pytest.approx(0.25)
    # gemini flash-lite priced from its real gateway rate
    assert price_usd(
        "google/gemini-3.5-flash-lite", 1_000_000, 1_000_000
    ) == pytest.approx(2.80)
    # unknown vendor/model still None
    assert price_usd("acme/unknown-9b", 1_000, 1_000) is None


def test_claude_id_spellings_resolve_to_the_alias():
    alias = PRICES["claude-sonnet-4-5"]
    # dated snapshot id
    assert lookup("claude-sonnet-4-5-20250929") == alias
    # OpenRouter's dotted spelling, with and without the vendor prefix
    assert lookup("anthropic/claude-sonnet-4.5") == alias
    assert lookup("claude-sonnet-4.5") == alias
    # dots are rewritten for Claude ids only: gpt-5.2-mini keeps its dot
    assert lookup("gpt-5-2-mini") is None


def test_an_explicit_table_replaces_the_built_in_one():
    table = {"house-model": ModelPrice(1.0, 2.0, 0.1, 1.25)}
    usd = price_usd("house-model", 1_000_000, 1_000_000, prices=table)
    assert usd == pytest.approx(3.0)
    assert price_usd("claude-sonnet-4-6", 1_000, 1_000, prices=table) is None


# ---- the run index: an amana-style log prices, unknown models are named ----


def _amana_log(model: str, *, mode: str = "realtime") -> list[dict]:
    """An LLM ``extract`` step on *model*, then a function ``calculate`` step.

    The shape of the amana contract-analysis runs that showed Spend "—"
    (accrue-ui#31): live mode reports usage per row, batch mode only on
    step_end.
    """
    batch = mode == "batch"
    usage = {"in": 10_000, "out": 1_000, "cost": None}
    records: list[dict] = [
        {
            "v": 1,
            "t": 0.0,
            "type": "pipeline_start",
            "run_id": "amana",
            "started_at": "2026-09-25T10:00:00+00:00",
            "num_rows": 2,
            "display_key": None,
            "steps": [
                {"name": "extract", "level": 0, "mode": mode, "model": model},
                {"name": "calculate", "level": 1, "mode": "realtime", "model": None},
            ],
            "plan": None,
        },
        {
            "v": 1,
            "t": 0.1,
            "type": "step_start",
            "step": "extract",
            "level": 0,
            "mode": mode,
            "num_rows": 2,
        },
    ]
    for r in range(2):
        records.append(row("extract", r, 1.0 + r, usage=None if batch else usage))
    records.append(
        {
            "v": 1,
            "t": 3.0,
            "type": "step_end",
            "step": "extract",
            "num_errors": 0,
            "usage": {"in": 20_000, "out": 2_000, "cost": None},
            "elapsed_s": 2.9,
            "batch_id": "msgbatch_1" if batch else None,
        }
    )
    records.append(
        {
            "v": 1,
            "t": 3.1,
            "type": "step_end",
            "step": "calculate",
            "num_errors": 0,
            "usage": {"in": 0, "out": 0, "cost": None},
            "elapsed_s": 0.1,
            "batch_id": None,
        }
    )
    return records


def _index(records: list[dict]) -> RunIndex:
    index = RunIndex(Path("unused.jsonl"))
    for record in records:
        index.apply(TailRecord(0, 1, record))
    return index


def test_amana_log_on_claude_sonnet_4_6_shows_a_dollar_cost():
    snap = _index(_amana_log("claude-sonnet-4-6")).snapshot()
    # 20k in at $3 + 2k out at $15 per 1M tokens.
    assert snap["stats"]["spend"] == pytest.approx(0.09)
    assert snap["cost"]["by_model"] == {"claude-sonnet-4-6": pytest.approx(0.09)}
    assert snap["stats"]["unpriced"] == []


def test_amana_batch_log_prices_from_step_end_at_the_batch_discount():
    snap = _index(_amana_log("claude-sonnet-4-6", mode="batch")).snapshot()
    assert snap["stats"]["spend"] == pytest.approx(0.045)
    assert snap["cost"]["batch_saved"] == pytest.approx(0.045)


def test_unknown_model_is_named_not_dashed():
    snap = _index(_amana_log("acme-contracts-9")).snapshot()
    assert snap["stats"]["spend"] is None
    assert snap["stats"]["unpriced"] == ["acme-contracts-9"]
    # The function step spent no tokens and has no model: never "unpriced".
    assert "calculate" not in snap["stats"]["unpriced"]


def test_partly_priced_run_still_names_the_unpriced_model():
    records = _amana_log("claude-sonnet-4-6")
    records[0]["steps"].append(
        {"name": "review", "level": 1, "mode": "realtime", "model": "acme-9"}
    )
    records.append(row("review", 0, 4.0, usage={"in": 500, "out": 50, "cost": None}))
    snap = _index(records).snapshot()
    assert snap["stats"]["spend"] == pytest.approx(0.09)
    assert snap["stats"]["unpriced"] == ["acme-9"]


def test_emitter_supplied_cost_counts_as_priced():
    records = _amana_log("acme-9")
    for rec in records:
        if rec["type"] == "row_complete":
            rec["usage"] = {"in": 10_000, "out": 1_000, "cost": 0.01}
    snap = _index(records).snapshot()
    assert snap["stats"]["spend"] == pytest.approx(0.02)
    assert snap["stats"]["unpriced"] == []


def test_unpriced_rides_the_sse_delta():
    records = _amana_log("acme-9")
    index = _index(records[:2])  # pipeline_start + step_start: no usage yet
    assert index.drain_delta()["stats"]["unpriced"] == []
    index.apply(TailRecord(0, 1, records[2]))  # first row with usage
    assert index.drain_delta()["stats"]["unpriced"] == ["acme-9"]
