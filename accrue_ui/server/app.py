"""FastAPI app factory.

``create_app(run_path, *, token, port=None, allow_origin=None, pipeline=None,
data=None)`` builds the whole read path: security middleware, the API routes,
a background follower that keeps the shared RunIndex current, a coalescer
that flushes SSE deltas at most every 100ms, and the static frontend mounted
at ``/`` (after the API routes, so ``/api/*`` always wins).

``port`` is the port the server will bind. It is part of the app's identity,
not just the runner's: the Origin check compares the *whole* origin
(``scheme://host:port``), because another local process listening on a
different loopback port is a different site — see ``security.py``.

``pipeline`` / ``data`` are the ``module:attr`` specs from the CLI. They are
imported once during startup (see ``retry.py``); whatever happens — success
or a precise failure reason — lands in ``/api/run``'s ``retry`` block, and
the resolved objects are what ``POST /api/retry`` runs.

Binding the server socket to 127.0.0.1 is the runner's job (``cli.py``);
everything here assumes loopback-only exposure — see ``security.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .events import FLUSH_INTERVAL_S, DeltaBroadcaster
from .index import RunIndex
from .retry import RetryController
from .routes import router
from .security import install_security
from .tail import Tail

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def _follow(app: FastAPI) -> None:
    """Feed appended records into the index; rebuild it on truncation."""
    tail: Tail = app.state.tail
    generation = tail.generation
    while True:
        records = await asyncio.to_thread(tail.read_available)
        if tail.generation != generation:
            # The log was truncated or replaced: offsets and derived state
            # are stale, so start a fresh index. read_available() already
            # returned records from the start of the new file. Re-applying
            # marks everything dirty again, so connected SSE clients converge
            # on the new file's state at the next flush.
            generation = tail.generation
            app.state.index = RunIndex(app.state.run_path, retry=app.state.retry)
        for record in records:
            app.state.index.apply(record)
        if not records:
            await asyncio.sleep(tail.poll_interval)


async def _flush_deltas(app: FastAPI) -> None:
    """Coalescer: drain accumulated changes at most every FLUSH_INTERVAL_S.

    The sleep-first loop bounds the delta rate at ≤10Hz no matter how fast
    the follower applies records; between flushes the RunIndex keeps
    coalescing (latest cell state wins), so nothing queues unboundedly.
    """
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_S)
        delta = app.state.index.drain_delta()
        if delta is not None:
            app.state.broadcaster.publish(delta)


def create_app(
    run_path: str | Path,
    *,
    token: str,
    port: int | None = None,
    allow_origin: str | None = None,
    pipeline: str | None = None,
    data: str | None = None,
) -> FastAPI:
    """Build the accrue-ui server app for one run log."""
    run_path = Path(run_path)
    retry = RetryController(run_path, pipeline, data)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tail = Tail(run_path)
        # Import --pipeline/--data now so the first snapshot already knows
        # whether retry is on the table, and why not when it isn't.
        retry.resolve()
        index = RunIndex(run_path, retry=retry)
        for record in tail.read_available():
            index.apply(record)
        # The initial backfill is the snapshot, not a delta: drain it away so
        # the first SSE flush only carries changes made after startup.
        index.drain_delta()
        app.state.run_path = run_path
        app.state.tail = tail
        app.state.index = index
        app.state.broadcaster = DeltaBroadcaster()
        tasks = [
            asyncio.create_task(_follow(app)),
            asyncio.create_task(_flush_deltas(app)),
        ]
        try:
            yield
        finally:
            await retry.aclose()
            for task in tasks:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)
            tail.close()

    app = FastAPI(title="accrue-ui", lifespan=lifespan, openapi_url=None)
    # Set before startup so a caller can inspect the retry specs (and the
    # reason they failed to import) without running the lifespan.
    app.state.retry = retry
    install_security(app, token=token, port=port, allow_origin=allow_origin)
    app.include_router(router)
    # Static frontend mounts last so /api/* is matched first.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
