"""FastAPI app factory.

``create_app(run_path, *, token, port=None, allow_origin=None, pipeline=None,
data=None, prices=None, runs_dir=None)`` builds the whole read path: security middleware,
the API routes, a background follower that keeps the shared RunLog (one
RunIndex per run in the file) current, a coalescer that flushes SSE deltas
at most every 100ms, and the static frontend mounted at ``/`` (after the API
routes, so ``/api/*`` always wins).

``port`` is the port the server will bind. It is part of the app's identity,
not just the runner's: the Origin check compares the *whole* origin
(``scheme://host:port``), because another local process listening on a
different loopback port is a different site — see ``security.py``.

``pipeline`` / ``data`` are the ``module:attr`` specs from the CLI. They are
imported once during startup (see ``retry.py``); whatever happens — success
or a precise failure reason — lands in ``/api/run``'s ``retry`` block, and
the resolved objects are what ``POST /api/retry`` runs.

``prices`` is the CLI's ``--prices`` TOML file. Unlike the retry specs it is
loaded here, eagerly: a bad file raises :class:`PricesError` before the
server exists, because a run silently priced from the wrong table is worse
than one that refuses to start.

``runs_dir`` is the CLI's ``--runs-dir``: ``/api/runs`` lists every run log
under it, recursively, instead of only the served log's siblings.

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
from .index import RunLog, open_log, scan_runs
from .pricing import PRICES, load_prices
from .retry import RetryController
from .routes import router
from .security import install_security
from .tail import Tail

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def _follow(app: FastAPI) -> None:
    """Feed appended records into the run log; rebuild it on truncation.

    ``POST /api/runs/select`` swaps in another log — ``tail`` and ``runs``
    together, already backfilled. The follower notices between
    reads, and is the only one to close the tail it was following: a read
    of it may have been in flight in the worker thread when the swap landed.
    """
    tail: Tail = app.state.tail
    generation = tail.generation
    while True:
        if app.state.tail is not tail:
            tail.close()
            tail = app.state.tail
            generation = tail.generation
        records = await asyncio.to_thread(tail.read_available)
        if app.state.tail is not tail:
            # Read from the log just switched away from: none of it belongs
            # to the run log now being served.
            continue
        if tail.generation != generation:
            # The log was truncated or replaced: offsets and derived state
            # are stale, so start a fresh run log. read_available() already
            # returned records from the start of the new file. Re-applying
            # marks everything dirty again, so connected SSE clients converge
            # on the new file's state at the next flush. The fresh log is
            # flagged so that flush carries reset:true — cells that existed
            # only in the old file are in-grid, so a client cannot gap-detect
            # them and must refetch the whole snapshot.
            generation = tail.generation
            app.state.runs = RunLog(
                tail.path,
                retry=app.state.retry,
                prices=app.state.prices,
                name=app.state.runs.name,
            )
            app.state.runs.mark_reset()
        for record in records:
            app.state.runs.apply(record)
        if not records:
            await asyncio.sleep(tail.poll_interval)


async def _flush_deltas(app: FastAPI) -> None:
    """Coalescer: drain accumulated changes at most every FLUSH_INTERVAL_S.

    The sleep-first loop bounds the delta rate at ≤10Hz no matter how fast
    the follower applies records; between flushes each RunIndex keeps
    coalescing (latest cell state wins), so nothing queues unboundedly.

    Each run's delta goes to the clients showing that run: those pinned to
    it by id, plus — for the latest run — the clients following the latest
    (``app.state.broadcaster``). A new run appearing, or the log being
    rebuilt, sends every client a ``reset`` first: the run list each of them
    holds is stale, and a latest-follower's latest run has changed.
    """
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_S)
        runs: RunLog = app.state.runs
        pinned: dict[str, DeltaBroadcaster] = app.state.run_broadcasters
        if runs.take_runs_changed():
            for broadcaster in (app.state.broadcaster, *pinned.values()):
                broadcaster.publish(_reset_delta())
        for index in runs.runs:
            delta = index.drain_delta()
            if delta is None:
                continue
            if index.id in pinned:
                pinned[index.id].publish(delta)
            if index is runs.latest:
                app.state.broadcaster.publish(delta)


def _log_name(app: FastAPI, path: Path) -> str:
    """*path*'s label in the run list: its folder under ``--runs-dir``."""
    if not app.state.runs_recursive:
        return path.stem
    listed = scan_runs(app.state.runs_dir, recursive=True, always=(path,))
    target = path.resolve()
    return next(
        (r["name"] for r in listed if Path(r["path"]).resolve() == target), path.stem
    )


def _reset_delta() -> dict:
    """An otherwise empty delta telling a client to refetch its snapshot."""
    return {"t": 0.0, "cells": [], "stats": {}, "steps": [], "reset": True}


def create_app(
    run_path: str | Path,
    *,
    token: str,
    port: int | None = None,
    allow_origin: str | None = None,
    pipeline: str | None = None,
    data: str | None = None,
    prices: str | Path | None = None,
    runs_dir: str | Path | None = None,
) -> FastAPI:
    """Build the accrue-ui server app for one run log."""
    run_path = Path(run_path)
    table = load_prices(prices) if prices is not None else PRICES
    retry = RetryController(
        run_path, pipeline, data, prices_path=prices, runs_dir=runs_dir
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Import --pipeline/--data now so the first snapshot already knows
        # whether retry is on the table, and why not when it isn't.
        retry.resolve()
        tail, runs = open_log(
            run_path, retry=retry, prices=table, name=_log_name(app, run_path)
        )
        app.state.tail = tail
        app.state.runs = runs
        # Clients following the file's latest run; ``run_broadcasters`` holds
        # one per run id a client has pinned with ``/api/events?run=``.
        app.state.broadcaster = DeltaBroadcaster()
        app.state.run_broadcasters = {}
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
            # The launch tail, and the one being served if a switch replaced
            # it (the follower closes the tails in between).
            tail.close()
            app.state.tail.close()

    app = FastAPI(title="accrue-ui", lifespan=lifespan, openapi_url=None)
    # Set before startup so a caller can inspect the retry specs (and the
    # reason they failed to import) without running the lifespan.
    app.state.retry = retry
    app.state.prices = table
    # What /api/runs lists: the --runs-dir tree, else the launch log's
    # siblings. Fixed at launch, so switching logs never moves the list.
    app.state.runs_dir = Path(runs_dir) if runs_dir is not None else run_path.parent
    app.state.runs_recursive = runs_dir is not None
    # Serializes POST /api/runs/select.
    app.state.select_lock = asyncio.Lock()
    install_security(app, token=token, port=port, allow_origin=allow_origin)
    app.include_router(router)
    # Static frontend mounts last so /api/* is matched first.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
