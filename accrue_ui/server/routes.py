"""API routes. All read from the shared RunIndex wired into app state.

Response shapes follow the frontend lane's ``docs/api-shapes.md`` (v0.1
contract). ``GET /api/events`` streams live coalesced deltas (≤10Hz — the
coalescer in ``app.py`` drains the RunIndex at most every 100ms) with
keepalive comments between them. ``POST /api/retry`` always answers 409 for
now; retry lands in issue #4.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .events import DeltaBroadcaster
from .index import RunIndex, scan_runs

SSE_KEEPALIVE_INTERVAL_S = 15.0
VALUES_MAX_COUNT = 1000

router = APIRouter(prefix="/api")


def _index(request: Request) -> RunIndex:
    return request.app.state.index


@router.get("/run")
async def get_run(request: Request) -> dict:
    """Full snapshot of the run, including the complete cell-state array."""
    return _index(request).snapshot()


@router.get("/values")
async def get_values(request: Request, start: int = 0, count: int = 50) -> dict:
    """Windowed row values for the data render mode; clamped to log bounds."""
    return _index(request).values_window(start, min(count, VALUES_MAX_COUNT))


@router.get("/cell/{step}/{row}")
async def get_cell(request: Request, step: str, row: int) -> dict:
    """One cell's full detail for the inspector, raw log records included."""
    detail = _index(request).cell_detail(step, row)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no cell {step}/{row}")
    return detail


@router.get("/runs")
async def get_runs(request: Request) -> dict:
    """Known run logs (siblings of the served log), newest first."""
    return {"runs": scan_runs(request.app.state.run_path.parent)}


@router.get("/events")
async def get_events(request: Request) -> StreamingResponse:
    """SSE stream of live coalesced deltas (shape: docs/api-shapes.md).

    The coalescer in ``app.py`` publishes at most every 100ms; between
    deltas, keepalive comments flow so proxies and clients see a live
    connection. A slow client's unread payload is merged, not queued —
    see ``events.py`` for the drop-and-coalesce semantics.
    """
    broadcaster: DeltaBroadcaster = request.app.state.broadcaster

    async def stream() -> AsyncIterator[bytes]:
        client = broadcaster.register()
        try:
            # Open with a comment so the client sees bytes immediately.
            yield b": connected\n\n"
            while True:
                try:
                    await asyncio.wait_for(
                        client.event.wait(), timeout=SSE_KEEPALIVE_INTERVAL_S
                    )
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                delta = client.take()
                if delta is not None:
                    payload = json.dumps(delta, separators=(",", ":"))
                    yield f"event: delta\ndata: {payload}\n\n".encode()
        finally:
            broadcaster.unregister(client)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/retry")
async def post_retry(request: Request) -> JSONResponse:
    """Retry is not available in the read path; orchestration is issue #4."""
    retry = _index(request).retry_block()
    return JSONResponse(
        {"available": False, "reason": retry["reason"]},
        status_code=409,
    )


@router.api_route(
    "/retry",
    methods=["GET", "HEAD", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def retry_method_not_allowed() -> JSONResponse:
    """Mutations are POST-only. Explicit 405 so the wrong method can't fall
    through to the static mount (whose full match would win over the POST
    route's partial one) and read as a 404."""
    return JSONResponse(
        {"detail": "Method Not Allowed"},
        status_code=405,
        headers={"Allow": "POST"},
    )
