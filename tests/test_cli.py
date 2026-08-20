"""The real CLI: log resolution, token URL, browser, loopback bind, security.

``make_server``/``webbrowser.open``/``create_app`` are monkeypatched at the
``cli`` module level so ``main()`` runs end-to-end without occupying a port;
one test drives a real uvicorn startup to prove the loopback-only bind.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from accrue_ui import cli
from tests.conftest import row, write_log

ROOT = Path(__file__).resolve().parents[1]
URL_RE = re.compile(r"^→ (http://127\.0\.0\.1:(\d+)/\?token=([A-Za-z0-9_-]{20,}))$")


def _write_small_log(path: Path) -> None:
    write_log(
        path,
        [
            {
                "v": 1,
                "t": 0.0,
                "type": "pipeline_start",
                "run_id": "cli-run",
                "started_at": "2026-08-20T10:00:00+00:00",
                "num_rows": 1,
                "display_key": None,
                "steps": [
                    {"name": "fetch", "level": 0, "mode": "realtime", "model": None}
                ],
                "plan": None,
            },
            row("fetch", 0, 1.0, values={"x": "y"}),
        ],
    )


@pytest.fixture
def run_log(tmp_path: Path) -> Path:
    log = tmp_path / "run.jsonl"
    _write_small_log(log)
    return log


class DummySocket:
    """Stand-in for the pre-bound listening socket (nothing is bound)."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Spy on main()'s collaborators; the dummy server never binds a port."""
    calls: dict = {"browser": [], "served": 0, "order": []}
    real_create_app = cli.create_app

    def spy_create_app(path, **kwargs):
        calls["log"] = Path(path)
        calls["app"] = real_create_app(path, **kwargs)
        calls["create_kwargs"] = kwargs
        return calls["app"]

    class DummyServer:
        def run(self, sockets=None) -> None:
            calls["served"] += 1
            calls["sockets"] = sockets

    def fake_make_server(app, port):
        calls["port"] = port
        return DummyServer()

    def fake_bind(port: int) -> DummySocket:
        calls["order"].append("bind")
        calls["bound"] = port
        sock = DummySocket(port)
        calls["socket"] = sock
        return sock

    def fake_open(url: str) -> None:
        calls["order"].append("browser")
        calls["browser"].append(url)

    monkeypatch.setattr(cli, "create_app", spy_create_app)
    monkeypatch.setattr(cli, "make_server", fake_make_server)
    monkeypatch.setattr(cli, "bind_loopback", fake_bind)
    monkeypatch.setattr(cli.webbrowser, "open", fake_open)
    return calls


def _printed_url(capsys: pytest.CaptureFixture) -> tuple[str, str, str]:
    """(url, port, token) from the arrow line main() prints."""
    out = capsys.readouterr().out
    for line in out.splitlines():
        match = URL_RE.match(line)
        if match:
            return match.group(1), match.group(2), match.group(3)
    raise AssertionError(f"no tokenized URL in output: {out!r}")


# ---- log resolution -------------------------------------------------------


def test_explicit_path_served(run_log: Path, captured: dict):
    assert cli.main([str(run_log), "--no-browser"]) == 0
    assert captured["log"] == run_log
    assert captured["served"] == 1


def test_missing_explicit_path_exits_1(tmp_path: Path, captured: dict, capsys):
    rc = cli.main([str(tmp_path / "nope.jsonl"), "--no-browser"])
    assert rc == 1
    assert "not found" in capsys.readouterr().err
    assert "app" not in captured  # never built an app, never served
    assert captured["served"] == 0


def test_newest_log_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured: dict
):
    monkeypatch.chdir(tmp_path)
    runs = tmp_path / ".accrue" / "runs"
    runs.mkdir(parents=True)
    older, newer = runs / "older.jsonl", runs / "newer.jsonl"
    _write_small_log(older)
    _write_small_log(newer)
    past = time.time() - 3600
    os.utime(older, (past, past))
    (runs / "not-a-log.txt").write_text("ignored")

    assert cli.main(["--no-browser"]) == 0
    assert captured["log"].name == "newer.jsonl"


def test_no_log_anywhere_exits_1_with_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured: dict, capsys
):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["--no-browser"])
    assert rc == 1
    err = capsys.readouterr().err
    assert ".accrue/runs" in err
    assert "accrue-ui" in err  # tells the user how to pass a path


def test_module_entrypoint_propagates_exit_code(tmp_path: Path):
    """`python -m accrue_ui` with nothing to serve exits 1, not 0.

    PYTHONPATH pins the subprocess to this checkout — without it, an
    editable install from a sibling checkout could win the import.
    """
    result = subprocess.run(
        [sys.executable, "-m", "accrue_ui"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert result.returncode == 1
    assert ".accrue/runs" in result.stderr


# ---- token, URL, browser --------------------------------------------------


def test_token_in_printed_url_and_fresh_per_launch(
    run_log: Path, captured: dict, capsys
):
    assert cli.main([str(run_log), "--no-browser", "--port", "7777"]) == 0
    _url1, port, token1 = _printed_url(capsys)
    assert port == "7777"
    assert cli.main([str(run_log), "--no-browser", "--port", "7777"]) == 0
    _url2, _port2, token2 = _printed_url(capsys)
    assert token1 != token2  # a fresh secret every launch


def test_browser_opens_by_default(run_log: Path, captured: dict, capsys):
    assert cli.main([str(run_log)]) == 0
    url, _port, _token = _printed_url(capsys)
    assert captured["browser"] == [url]
    # The port is ours before the token goes anywhere near a browser.
    assert captured["order"] == ["bind", "browser"]


def test_port_is_bound_before_anything_is_served(run_log: Path, captured: dict):
    assert cli.main([str(run_log), "--no-browser", "--port", "7777"]) == 0
    assert captured["bound"] == 7777
    # uvicorn serves the socket we already hold, it does not bind its own.
    assert captured["sockets"] == [captured["socket"]]
    assert captured["socket"].closed is True  # released on the way out


def test_port_reaches_the_app_for_the_origin_check(run_log: Path, captured: dict):
    """The Origin check needs the port, so create_app must be told it."""
    assert cli.main([str(run_log), "--no-browser", "--port", "7777"]) == 0
    assert captured["create_kwargs"]["port"] == 7777


def test_occupied_port_exits_1_without_leaking_the_token(
    run_log: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """A port squatter must not receive the launch URL.

    Regression: the URL used to be printed (and opened) before uvicorn ever
    tried to bind, so whoever already held the port got the token.
    """
    opened: list[str] = []
    served: list[int] = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(
        cli, "make_server", lambda app, port: served.append(port) or None
    )

    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    port = squatter.getsockname()[1]
    try:
        rc = cli.main([str(run_log), "--port", str(port)])
    finally:
        squatter.close()

    assert rc == 1
    captured_io = capsys.readouterr()
    assert "token" not in captured_io.out
    assert "→" not in captured_io.out
    assert "cannot bind" in captured_io.err
    assert opened == []
    assert served == []


def test_no_browser_suppresses_webbrowser(run_log: Path, captured: dict):
    assert cli.main([str(run_log), "--no-browser"]) == 0
    assert captured["browser"] == []


def test_pipeline_and_data_specs_reach_the_retry_controller(
    run_log: Path, captured: dict
):
    """--pipeline/--data build the retry controller; bad specs stay non-fatal."""
    argv = [str(run_log), "--no-browser", "--pipeline", "enrich:pipe"]
    assert cli.main([*argv, "--data", "enrich:rows"]) == 0
    controller = captured["app"].state.retry
    assert (controller.pipeline_spec, controller.data_spec) == (
        "enrich:pipe",
        "enrich:rows",
    )
    # No such module: the server still came up, retry just says why not.
    assert controller.available is False
    assert "cannot import module 'enrich'" in controller.reason
    assert controller.resume_command.endswith(
        "--pipeline enrich:pipe --data enrich:rows"
    )

    assert cli.main([str(run_log), "--no-browser"]) == 0
    controller = captured["app"].state.retry
    assert (controller.pipeline_spec, controller.data_spec) == (None, None)
    assert controller.reason == "launched without --pipeline"


# ---- the CLI-built app keeps the security posture -------------------------


def test_security_regression_with_cli_built_app(run_log: Path, captured: dict, capsys):
    assert cli.main([str(run_log), "--no-browser", "--port", "7607"]) == 0
    _url, _port, token = _printed_url(capsys)
    app = captured["app"]
    own = "http://127.0.0.1:7607"
    with TestClient(app, base_url=own) as client:
        assert client.get("/api/run").status_code == 401  # no token
        headers = {"X-Accrue-Token": token}
        assert client.get("/api/run", headers=headers).status_code == 200
        evil = {**headers, "Origin": "https://evil.example"}
        assert client.get("/api/run", headers=evil).status_code == 403
        # Another loopback port is another site, even with a valid token.
        neighbour = {**headers, "Origin": "http://127.0.0.1:31337"}
        assert client.get("/api/run", headers=neighbour).status_code == 403
        assert client.get("/api/retry", headers=headers).status_code == 405
        resp = client.post(
            "/api/retry",
            json={"all": True},
            headers={**headers, "Origin": own},
        )
        assert resp.status_code == 409  # retry still refuses: issue #4
        # ?token= page hit sets the cookie, which then auths /api/*.
        client.get(f"/?token={token}")
        assert client.get("/api/run").status_code == 200


# ---- real uvicorn startup -------------------------------------------------


def test_uvicorn_startup_binds_loopback_only(run_log: Path):
    app = cli.create_app(run_log, token="cli-test-token")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = cli.make_server(app, port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started:
            assert thread.is_alive(), "uvicorn exited before startup"
            assert time.time() < deadline, "uvicorn did not start in time"
            time.sleep(0.05)
        bound = server.servers[0].sockets[0].getsockname()
        assert bound[0] == "127.0.0.1"
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/run",
            headers={"X-Accrue-Token": "cli-test-token"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()
