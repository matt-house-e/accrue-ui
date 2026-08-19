"""Command-line entry point for accrue-ui.

Argument surface is defined now so the interface is stable; the server
itself lands in issue #1.
"""

from __future__ import annotations

import argparse

from . import __version__


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
        default=7607,
        help="Port to serve on (default: 7607).",
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


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print(f"accrue-ui {__version__} — server lands in issue #1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
