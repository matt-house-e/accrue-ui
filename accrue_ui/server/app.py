"""FastAPI app factory.

``create_app(run_path, *, token, allow_origin=None)`` builds the whole read
path: security middleware, the API routes, a background follower that keeps
the shared RunIndex current, and the static frontend mounted at ``/`` (after
the API routes, so ``/api/*`` always wins).

Binding the server socket to 127.0.0.1 is the runner's job (issue #3);
everything here assumes loopback-only exposure — see ``security.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .index import RunIndex
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
            # returned records from the start of the new file.
            generation = tail.generation
            app.state.index = RunIndex(app.state.run_path)
        for record in records:
            app.state.index.apply(record)
        if not records:
            await asyncio.sleep(tail.poll_interval)


def create_app(
    run_path: str | Path,
    *,
    token: str,
    allow_origin: str | None = None,
) -> FastAPI:
    """Build the accrue-ui server app for one run log."""
    run_path = Path(run_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tail = Tail(run_path)
        index = RunIndex(run_path)
        for record in tail.read_available():
            index.apply(record)
        app.state.run_path = run_path
        app.state.tail = tail
        app.state.index = index
        follower = asyncio.create_task(_follow(app))
        try:
            yield
        finally:
            follower.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await follower
            tail.close()

    app = FastAPI(title="accrue-ui", lifespan=lifespan, openapi_url=None)
    install_security(app, token=token, allow_origin=allow_origin)
    app.include_router(router)
    # Static frontend mounts last so /api/* is matched first.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
