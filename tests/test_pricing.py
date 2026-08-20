"""Pricing table: known models price, unknown models stay null."""

from __future__ import annotations

import pytest

from accrue_ui.server.pricing import BATCH_DISCOUNT, price_usd


def test_known_models_price_per_mtok():
    # claude-sonnet-5: $3.00 in / $15.00 out per 1M tokens.
    assert price_usd("claude-sonnet-5", 1_000_000, 1_000_000) == pytest.approx(18.0)
    # claude-haiku-4-5: $1.00 / $5.00.
    assert price_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == pytest.approx(6.0)
    # gpt-5.x-mini class: $0.25 / $2.00.
    assert price_usd("gpt-5.2-mini", 1_000_000, 1_000_000) == pytest.approx(2.25)


def test_small_counts():
    assert price_usd("claude-sonnet-5", 1000, 100) == pytest.approx(0.0045)
    assert price_usd("claude-sonnet-5", 0, 0) == 0.0


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
