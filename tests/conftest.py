"""Shared fixtures: app/client factories and run-log builders."""

from __future__ import annotations

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
