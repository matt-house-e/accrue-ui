"""Pricing table: known models price, unknown models stay null."""

from __future__ import annotations

import pytest

from accrue_ui.server.pricing import (
    BATCH_DISCOUNT,
    PRICES,
    ModelPrice,
    lookup,
    price_usd,
)

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
