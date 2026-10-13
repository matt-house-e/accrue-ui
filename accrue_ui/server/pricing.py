"""Model pricing for the cost views.

The run log's ``usage.cost`` is always null in schema v1 — accrue core
deliberately ships no pricing data — so accrue-ui prices tokens itself from
the small table below. The values are **indicative list prices**, easy to
edit, and will drift; a future accrue emitter may start writing real dollar
amounts into ``usage.cost``, which the index prefers over this table.

Unknown models (and function steps, where ``model`` is null) price to
``None``: their dollars are omitted upstream, and when nothing prices, the
UI's spend figures are null rather than a misleading zero.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import NamedTuple


class ModelPrice(NamedTuple):
    """USD per 1M tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float  # 5-minute cache write


# Anthropic's standard prompt-cache multipliers on the input price: cache
# hits at 0.1x, 5-minute writes at 1.25x. Models that differ say so below.
CACHE_READ_X = 0.1
CACHE_WRITE_X = 1.25


def _claude(input: float, output: float, cache_read: float | None = None) -> ModelPrice:
    if cache_read is None:
        cache_read = input * CACHE_READ_X
    return ModelPrice(input, output, cache_read, input * CACHE_WRITE_X)


def _other(input: float, output: float) -> ModelPrice:
    # OpenAI/Google: cached input at ~0.1x, no write premium. Indicative.
    return ModelPrice(input, output, input * CACHE_READ_X, input)


# Claude: Anthropic first-party list prices from
# https://platform.claude.com/docs/en/about-claude/pricing, checked 2026-10-13.
#   - Fable 5.1 cache hits are 0.025x input; Opus 5.5 and Sonnet 5.5 are 0.05x.
#   - Sonnet 5's $2/$10 launch price became its standard price; the planned
#     move to $3/$15 on 2026-09-01 was cancelled.
#   - Haiku 5.5 is priced at its <=100K-token-prompt tier ($0.50/$2.50 above
#     that); per-request prompt length is not in the run log.
#   - Bedrock and Vertex bill Claude separately; these are Anthropic API rates.
# Others: indicative, checked 2026-08-20 (gpt-5.x-mini class at OpenAI's
# mini tier; gemini at their gateway rates).
PRICES: dict[str, ModelPrice] = {
    "claude-fable-5-1": _claude(10.00, 50.00, cache_read=0.25),
    "claude-fable-5": _claude(10.00, 50.00),
    "claude-opus-5-5": _claude(4.00, 20.00, cache_read=0.20),
    "claude-opus-5": _claude(5.00, 25.00),
    "claude-opus-4-8": _claude(5.00, 25.00),
    "claude-opus-4-7": _claude(5.00, 25.00),
    "claude-opus-4-6": _claude(5.00, 25.00),
    "claude-opus-4-5": _claude(5.00, 25.00),
    "claude-opus-4-1": _claude(15.00, 75.00),
    "claude-opus-4-0": _claude(15.00, 75.00),
    "claude-sonnet-5-5": _claude(2.00, 10.00, cache_read=0.10),
    "claude-sonnet-5": _claude(2.00, 10.00),
    "claude-sonnet-4-6": _claude(3.00, 15.00),
    "claude-sonnet-4-5": _claude(3.00, 15.00),
    "claude-sonnet-4-0": _claude(3.00, 15.00),
    "claude-haiku-5-5": _claude(0.10, 0.50),
    "claude-haiku-4-5": _claude(1.00, 5.00),
    "gpt-5-mini": _other(0.25, 2.00),
    "gpt-5.1-mini": _other(0.25, 2.00),
    "gpt-5.2-mini": _other(0.25, 2.00),
    "gemini-3.5-flash-lite": _other(0.30, 2.50),
    "gemini-3.7-flash": _other(0.375, 1.875),
}

# Both OpenAI and Anthropic batch APIs bill at 50% of list price.
BATCH_DISCOUNT = 0.5

# Dated snapshot ids ("claude-sonnet-4-5-20250929") price as their alias.
_SNAPSHOT_SUFFIX = re.compile(r"-\d{8}$")


def lookup(
    model: str | None, prices: Mapping[str, ModelPrice] = PRICES
) -> ModelPrice | None:
    """The price entry for *model*, or ``None`` when it has none."""
    if not model:
        return None
    names = [model]
    if "/" in model:
        # OpenAI-compatible gateways (OpenRouter etc.) prefix ids with the
        # vendor: "openai/gpt-5.2-mini" prices as "gpt-5.2-mini".
        names.append(model.rsplit("/", 1)[-1])
    for name in names:
        bare = _SNAPSHOT_SUFFIX.sub("", name)
        # OpenRouter spells Claude versions with dots: "claude-sonnet-4.6".
        dashed = bare.replace(".", "-") if bare.startswith("claude-") else bare
        for key in (name, bare, dashed):
            if key in prices:
                return prices[key]
    return None


def price_usd(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    *,
    cache_read: int = 0,
    cache_write: int = 0,
    batch: bool = False,
    prices: Mapping[str, ModelPrice] = PRICES,
) -> float | None:
    """Dollar cost for a token count, or ``None`` when the model is unknown.

    ``cache_read``/``cache_write`` are prompt-cache token counts, each at its
    own rate; schema v1 usage carries none, so the index never passes them.
    ``batch=True`` applies the 50% batch-API discount.
    """
    price = lookup(model, prices)
    if price is None:
        return None
    usd = (
        tokens_in * price.input
        + tokens_out * price.output
        + cache_read * price.cache_read
        + cache_write * price.cache_write
    ) / 1_000_000
    return usd * BATCH_DISCOUNT if batch else usd
