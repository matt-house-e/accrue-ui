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

# USD per 1M tokens: {model: (input, output)}. Indicative, checked 2026-08-20.
#   - claude-sonnet-5:  $3.00 / $15.00  (Anthropic list; intro pricing of
#     $2.00 / $10.00 runs through 2026-08-31 — we price at list)
#   - claude-haiku-4-5: $1.00 / $5.00   (Anthropic list)
#   - gpt-5.x-mini class: $0.25 / $2.00 (OpenAI mini-tier, indicative)
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5.1-mini": (0.25, 2.00),
    "gpt-5.2-mini": (0.25, 2.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Both OpenAI and Anthropic batch APIs bill at 50% of list price.
BATCH_DISCOUNT = 0.5


def price_usd(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    *,
    batch: bool = False,
) -> float | None:
    """Dollar cost for a token count, or ``None`` when the model is unknown.

    ``batch=True`` applies the 50% batch-API discount.
    """
    if not model:
        return None
    prices = PRICES.get(model)
    if prices is None and "/" in model:
        # OpenAI-compatible gateways (OpenRouter etc.) prefix ids with the
        # vendor: "openai/gpt-5.2-mini" prices as "gpt-5.2-mini".
        prices = PRICES.get(model.rsplit("/", 1)[-1])
    if prices is None:
        return None
    per_in, per_out = prices
    usd = (tokens_in * per_in + tokens_out * per_out) / 1_000_000
    return usd * BATCH_DISCOUNT if batch else usd
