"""Command-line entry point for accrue-ui.

``accrue-ui [run_log] [--pipeline mod:attr] [--data mod:attr] [--port 7607]
[--no-browser]`` resolves the run log (explicit path, else the newest
``*.jsonl`` under ``./.accrue/runs``), generates a fresh URL-safe launch
token, builds the server app, **binds the loopback port**, and only then
prints the tokenized URL and opens the browser — uvicorn serves on the
socket already in hand, bound to **127.0.0.1 only**, hardcoded, no flag to
widen it (see ``server/security.py`` for the threat model).

Binding first is a security requirement, not tidiness: the URL carries the
launch token, so printing it before the port is ours would hand that token
to whatever process got there first (see :func:`bind_loopback`).

``--pipeline`` (plus ``--data`` when it names a Pipeline rather than a
factory) is what makes retry possible; see ``server/retry.py`` for the
accepted shapes. Run the command from the directory the pipeline itself ran
in — the one holding ``.accrue/`` — so the retry finds the same checkpoint.
"""

from __future__ import annotations

import argparse
import secrets
import socket
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
        help=(
            "Import path (module:attr) to the Pipeline, or to a zero-arg "
            "callable returning (pipeline, data). Required for retry."
        ),
    )
    parser.add_argument(
        "--data",
        default=None,
        help=(
            "Import path (module:attr) to the run's input rows — a DataFrame, "
            "a list of dicts, or a zero-arg callable returning one. Required "
            "when --pipeline names a Pipeline instance."
        ),
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


def bind_loopback(port: int) -> socket.socket:
    """Listen on ``127.0.0.1:port`` and return the socket, or raise OSError.

    Called **before** the tokenized URL is printed or opened. Uvicorn would
    otherwise bind inside ``server.run()``, long after the token is on the
    user's screen and in their browser — and if the port were already taken
    by another local process, that URL would name *its* server, handing the
    launch token to a squatter. Binding first turns that into a clean
    "port in use" exit with nothing leaked.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # TIME_WAIT reuse only — this never lets a live listener share the
        # port, so a squatter still fails the bind (which is the point).
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, port))
        sock.listen(2048)
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


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
    # The specs are imported during app startup, not here: a bad --pipeline
    # is reported in the UI (retry.reason) rather than killing the server.
    app = create_app(
        run_log,
        token=token,
        port=args.port,
        pipeline=args.pipeline,
        data=args.data,
    )

    # Bind BEFORE the token is printed or opened — see bind_loopback().
    try:
        sock = bind_loopback(args.port)
    except OSError as exc:
        print(
            f"accrue-ui: cannot bind {HOST}:{args.port} ({exc}) — "
            "another server already has it. Try --port.",
            file=sys.stderr,
        )
        return 1

    url = f"http://{HOST}:{args.port}/?token={token}"
    print(f"→ {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)

    try:
        make_server(app, args.port).run(sockets=[sock])
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
