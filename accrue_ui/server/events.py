"""SSE delta fan-out: drop-and-coalesce broadcasting to connected clients.

The RunIndex accumulates dirty state (cells, step counters, stats) as the
follower feeds it records; a single flush loop (see ``app.py``) drains it at
most every :data:`FLUSH_INTERVAL_S` — the ≤10Hz bound from docs/api-shapes.md
— and publishes the resulting delta to every connected client.

Each client holds exactly ONE pending payload. Publishing onto a client that
has not consumed its previous payload **merges** the two (latest cell state
wins; stats and step counters update by key) instead of queueing. A slow
client therefore receives snapshots of accumulated state, never an unbounded
journal — per-client memory is bounded by the grid size.
"""

from __future__ import annotations

import asyncio
from typing import Any

#: Minimum seconds between delta flushes (0.1s -> at most 10 events/second).
FLUSH_INTERVAL_S = 0.1


def merge_deltas(base: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Coalesce two delta payloads into one; neither input is mutated."""
    cells = {(r, s): state for r, s, state in base["cells"]}
    cells.update({(r, s): state for r, s, state in new["cells"]})
    steps = {s["name"]: s for s in base["steps"]}
    steps.update({s["name"]: s for s in new["steps"]})
    merged = {
        "t": new["t"],
        "cells": [[r, s, state] for (r, s), state in cells.items()],
        "stats": {**base["stats"], **new["stats"]},
        "steps": list(steps.values()),
    }
    # A reset must survive coalescing into a slow client's pending payload:
    # dropping it would leave that client on the pre-reindex grid. Absent
    # means false, so only carry it forward when either side set it.
    if base.get("reset") or new.get("reset"):
        merged["reset"] = True
    return merged


class SSEClient:
    """One connected /api/events stream: a single mergeable pending slot."""

    __slots__ = ("event", "pending")

    def __init__(self) -> None:
        self.pending: dict[str, Any] | None = None
        self.event = asyncio.Event()

    def take(self) -> dict[str, Any] | None:
        """Consume the pending delta (None when nothing arrived)."""
        delta, self.pending = self.pending, None
        self.event.clear()
        return delta


class DeltaBroadcaster:
    """Registry of SSE clients; publish() fans a delta out to all of them."""

    def __init__(self) -> None:
        self._clients: set[SSEClient] = set()

    def register(self) -> SSEClient:
        client = SSEClient()
        self._clients.add(client)
        return client

    def unregister(self, client: SSEClient) -> None:
        self._clients.discard(client)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def publish(self, delta: dict[str, Any]) -> None:
        """Hand *delta* to every client, merging into unconsumed payloads."""
        for client in self._clients:
            if client.pending is None:
                client.pending = delta
            else:
                client.pending = merge_deltas(client.pending, delta)
            client.event.set()
