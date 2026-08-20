"""API routes. All read from the shared RunIndex wired into app state.

Response shapes follow the frontend lane's ``docs/api-shapes.md`` (v0.1
contract). ``GET /api/events`` streams live coalesced deltas (≤10Hz — the
coalescer in ``app.py`` drains the RunIndex at most every 100ms) with
keepalive comments between them. ``POST /api/retry`` selects failed rows and
hands them to the retry controller (``retry.py``), which re-runs exactly
those cells and appends the result to the log this server is already
following, so healed cells arrive by the same path as a live run.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .events import DeltaBroadcaster
from .index import RunIndex, scan_runs
from .retry import ALREADY_RUNNING, RetryController

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


def _selected_rows(body: Any, index: RunIndex) -> list[int]:
    """Resolve a retry request body to row indices, or raise HTTPException.

    Exactly one selector: ``{"rows": [...]}`` (explicit), ``{"group":
    {"step": s, "type": t}}`` (one error group, resolved against the index's
    current groups — so a group that healed since the page loaded is a 404,
    not a silent no-op), or ``{"all": true}`` (every errored row).
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    selectors = [key for key in ("rows", "group", "all") if key in body]
    if len(selectors) != 1:
        raise HTTPException(
            status_code=400,
            detail='exactly one of "rows", "group", "all" is required',
        )
    selector = selectors[0]

    if selector == "all":
        if body["all"] is not True:
            raise HTTPException(status_code=400, detail='"all" must be true')
        rows = index.error_rows()
        if not rows:
            raise HTTPException(status_code=400, detail="no failed rows to retry")
        return rows

    if selector == "rows":
        rows = body["rows"]
        valid = (
            isinstance(rows, list)
            and bool(rows)
            and all(isinstance(r, int) and not isinstance(r, bool) for r in rows)
        )
        if not valid:
            raise HTTPException(
                status_code=400, detail='"rows" must be a non-empty list of integers'
            )
        return sorted(set(rows))

    group = body["group"]
    if not isinstance(group, dict) or not isinstance(group.get("step"), str):
        raise HTTPException(
            status_code=400, detail='"group" must be {"step": ..., "type": ...}'
        )
    rows = index.group_rows(group["step"], str(group.get("type") or "Error"))
    if rows is None:
        raise HTTPException(
            status_code=404,
            detail=f"no error group {group['step']}/{group.get('type')}",
        )
    return rows


@router.post("/retry")
async def post_retry(request: Request) -> JSONResponse:
    """Re-run failed cells: 202 with the row count, or 409 with the reason.

    409 covers both ways retry can be off the table — the server was
    launched without a usable ``--pipeline``, or one retry is already in
    flight (only ever one at a time). Accepted retries run in the
    background; watch ``/api/run`` and the SSE stream for the cells healing.
    """
    index = _index(request)
    controller: RetryController = request.app.state.retry
    block = controller.block()
    if not block["available"]:
        return JSONResponse(block, status_code=409)

    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="body must be JSON") from None
    rows = _selected_rows(body, index)
    steps = None
    if isinstance(body, dict) and isinstance(body.get("group"), dict):
        steps = [body["group"]["step"]]

    if not controller.start(rows, steps=steps, display_key=index.display_key):
        return JSONResponse({**block, "reason": ALREADY_RUNNING}, status_code=409)
    return JSONResponse({"accepted": len(rows)}, status_code=202)


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
