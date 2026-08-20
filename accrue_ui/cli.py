"""Command-line entry point for accrue-ui.

``accrue-ui [run_log] [--pipeline mod:attr] [--port 7607] [--no-browser]``
resolves the run log (explicit path, else the newest ``*.jsonl`` under
``./.accrue/runs``), generates a fresh URL-safe launch token, builds the
server app, prints the tokenized URL, opens the browser, and serves with
uvicorn bound to **127.0.0.1 only** — hardcoded, no flag to widen it (see
``server/security.py`` for the threat model).
"""

from __future__ import annotations

import argparse
import secrets
import sys
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from . import __version__
from .server.app import create_app

#: Loopback only, on purpose. There is deliberately no --host flag.
HOST = "127.0.0.1"
DEFAULT_PORT = 7607
RUNS_DIR = Path(".accrue") / "runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="accrue-ui",
        description="Run-observability dashboard for accrue pipelines.",
    )
    parser.add_argument(
        "run_log",
        nargs="?",
        default=None,
        help="Path to a JSONL run log. Defaults to the latest log under .accrue/runs/.",
    )
    parser.add_argument(
        "--pipeline",
        default=None,
        help="Import path to the pipeline object (module:attr), required for retry.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to serve on (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser tab on startup.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def newest_run_log(runs_dir: Path = RUNS_DIR) -> Path | None:
    """The most recently modified ``*.jsonl`` under *runs_dir*, if any."""
    if not runs_dir.is_dir():
        return None
    logs = [p for p in runs_dir.glob("*.jsonl") if p.is_file()]
    return max(logs, key=lambda p: p.stat().st_mtime, default=None)


def make_server(app: FastAPI, port: int) -> uvicorn.Server:
    """A uvicorn server bound to loopback; programmatic so tests can drive it."""
    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
    return uvicorn.Server(config)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.run_log is not None:
        run_log = Path(args.run_log)
        if not run_log.is_file():
            print(f"accrue-ui: run log not found: {run_log}", file=sys.stderr)
            return 1
    else:
        found = newest_run_log()
        if found is None:
            print(
                f"accrue-ui: no run log given and no *.jsonl under {RUNS_DIR}/ "
                "— pass a path: accrue-ui path/to/run.jsonl",
                file=sys.stderr,
            )
            return 1
        run_log = found

    token = secrets.token_urlsafe(32)
    app = create_app(run_log, token=token)
    # Accepted now, wired up in issue #4 (retry); until then POST /api/retry
    # keeps answering 409.
    app.state.pipeline = args.pipeline

    url = f"http://{HOST}:{args.port}/?token={token}"
    print(f"→ {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)

    make_server(app, args.port).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
