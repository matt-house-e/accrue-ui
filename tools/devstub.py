"""DEV ONLY stub server — never ship or expose this.

Serves ``accrue_ui/static/`` at ``/`` and canned fixtures from
``tests/fixtures/api/`` at the ``/api/*`` routes documented in
``docs/api-shapes.md``, so the frontend renders end-to-end before the real
FastAPI server (issues #1/#3) lands. ``/api/values`` returns its fixture
regardless of query params; ``/api/events`` replays the sample deltas as SSE
then holds the connection open. Stdlib only; requires a repo checkout (the
fixtures are not packaged).

**It lives outside ``accrue_ui/`` on purpose.** There is no launch token,
no Origin check and no Host check in here — every route answers anyone who
asks. Keeping it in ``tools/`` keeps it out of the wheel, so an installed
accrue-ui cannot be talked into starting it.

Usage: python tools/devstub.py [--port 7607]
"""

from __future__ import annotations

import argparse
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "accrue_ui" / "static"
FIXTURES = ROOT / "tests" / "fixtures" / "api"


class StubHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def do_GET(self):  # http.server API name
        path = self.path.split("?", 1)[0]
        if path == "/api/run":
            return self._json("run.json")
        if path == "/api/values":
            return self._json("values_1281.json")
        if path.startswith("/api/cell/"):
            return self._json("cell_web_search_1284.json")
        if path == "/api/runs":
            return self._json("runs.json")
        if path == "/api/events":
            return self._sse()
        return super().do_GET()

    def _json(self, fixture_name: str) -> None:
        body = (FIXTURES / fixture_name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            lines = (FIXTURES / "events_sample.ndjson").read_text().splitlines()
            for line in lines:
                if not line.strip():
                    continue
                self.wfile.write(f"event: delta\ndata: {line}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.4)
            while True:  # hold the stream open like a live server would
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(15)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):  # keep dev output quiet
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=7607)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), StubHandler)
    print(f"accrue-ui devstub (DEV ONLY) on http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
