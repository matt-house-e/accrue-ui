"""Shared fixtures: app/client factories, run-log builders, SSE drivers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from accrue_ui.server.app import create_app

TOKEN = "test-token-123"
AUTH_HEADERS = {"X-Accrue-Token": TOKEN}
GOLDEN_FIXTURE = Path(__file__).parent / "fixtures" / "contract" / "run_small.jsonl"


def write_log(path: Path, records: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(record) + "\n" for record in records)


def age_file(path: Path, seconds: float = 120.0) -> None:
    """Backdate mtime so the 'recently written' half of live-detection is off."""
    past = time.time() - seconds
    os.utime(path, (past, past))


def row(
    step: str,
    row_i: int,
    t: float,
    *,
    status: str = "ok",
    from_cache: bool = False,
    values: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    elapsed_ms: float | None = 1.0,
) -> dict[str, Any]:
    return {
        "v": 1,
        "t": t,
        "type": "row_complete",
        "step": step,
        "row": row_i,
        "status": status,
        "from_cache": from_cache,
        "values": values,
        "error": error,
        "usage": usage,
        "elapsed_ms": elapsed_ms,
    }


def client_for(
    log_path: Path,
    *,
    authed: bool = True,
    base_url: str = "http://localhost",
    **kwargs: Any,
) -> TestClient:
    """TestClient over a fresh app for *log_path*; use as a context manager."""
    app = create_app(log_path, token=TOKEN, **kwargs)
    headers = dict(AUTH_HEADERS) if authed else {}
    return TestClient(app, base_url=base_url, headers=headers)


@pytest.fixture
def golden_log(tmp_path: Path) -> Path:
    """The pinned cross-repo fixture, copied to tmp with an aged mtime."""
    dst = tmp_path / "run_small.jsonl"
    shutil.copy(GOLDEN_FIXTURE, dst)
    age_file(dst)
    return dst


# ---- raw-ASGI drivers for the SSE endpoint --------------------------------
#
# TestClient cannot consume a never-ending response, so SSE tests drive the
# ASGI app directly: run its lifespan by hand, open /api/events as a raw
# http scope, and cancel the task to disconnect (which is exactly what a
# client going away looks like).


@contextlib.asynccontextmanager
async def lifespan_ctx(app: Any):
    """Run *app*'s lifespan protocol around the body of the ``async with``."""
    messages: asyncio.Queue = asyncio.Queue()
    startup_done = asyncio.Event()
    shutdown_done = asyncio.Event()

    async def receive() -> dict:
        return await messages.get()

    async def send(message: dict) -> None:
        if message["type"].startswith("lifespan.startup"):
            startup_done.set()
        elif message["type"].startswith("lifespan.shutdown"):
            shutdown_done.set()

    scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
    task = asyncio.create_task(app(scope, receive, send))
    await messages.put({"type": "lifespan.startup"})
    await asyncio.wait_for(startup_done.wait(), timeout=10)
    try:
        yield
    finally:
        await messages.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(shutdown_done.wait(), timeout=10)
        await task


class SSEStream:
    """One open GET /api/events connection; parses frames as they arrive."""

    def __init__(self, app: Any, token: str = TOKEN):
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.chunks: list[bytes] = []
        self.comments: list[str] = []
        self.deltas: list[dict] = []
        self._buffer = b""
        self._new_data = asyncio.Event()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": "/api/events",
            "raw_path": b"/api/events",
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "headers": [(b"host", b"localhost"), (b"x-accrue-token", token.encode())],
            "server": ("localhost", 80),
            "client": ("127.0.0.1", 1234),
        }
        self._task = asyncio.create_task(app(scope, self._receive, self._send))

    async def _receive(self) -> dict:
        await asyncio.sleep(3600)  # hold the connection open
        return {"type": "http.disconnect"}

    async def _send(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body" and message.get("body"):
            self.chunks.append(message["body"])
            self._buffer += message["body"]
            self._parse()
        self._new_data.set()

    def _parse(self) -> None:
        while b"\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split(b"\n\n", 1)
            text = frame.decode()
            if text.startswith(":"):
                self.comments.append(text[1:].strip())
                continue
            event = None
            data_lines = []
            for line in text.splitlines():
                if line.startswith("event:"):
                    event = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].strip())
            if event == "delta":
                self.deltas.append(json.loads("\n".join(data_lines)))

    async def wait_for(self, predicate: Any, timeout: float = 5.0) -> None:
        """Wait until ``predicate(self)`` is true or fail the test."""
        deadline = time.monotonic() + timeout
        while not predicate(self):
            remaining = deadline - time.monotonic()
            assert remaining > 0, (
                f"timed out waiting on SSE stream; chunks={self.chunks!r}"
            )
            self._new_data.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._new_data.wait(), timeout=remaining)

    async def close(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
