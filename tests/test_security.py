"""Security middleware: token, cookie flow, Origin/Host checks, POST-only."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from tests.conftest import TOKEN, client_for


def test_no_token_401(golden_log: Path):
    with client_for(golden_log, authed=False) as client:
        assert client.get("/api/run").status_code == 401


def test_bad_token_401(golden_log: Path):
    with client_for(golden_log, authed=False) as client:
        resp = client.get("/api/run", headers={"X-Accrue-Token": "wrong-token"})
        assert resp.status_code == 401


def test_good_token_via_header_200(golden_log: Path):
    with client_for(golden_log) as client:
        assert client.get("/api/run").status_code == 200


def test_token_query_param_sets_cookie_then_cookie_auths(golden_log: Path):
    with client_for(golden_log, authed=False) as client:
        # API-first without the cookie: rejected.
        assert client.get("/api/run").status_code == 401
        # First page hit with ?token= sets the HttpOnly cookie...
        client.get(f"/?token={TOKEN}")
        assert client.cookies.get("accrue_ui_token") == TOKEN
        # ...which then authenticates /api/* on its own.
        assert client.get("/api/run").status_code == 200


def test_wrong_query_token_sets_no_cookie(golden_log: Path):
    with client_for(golden_log, authed=False) as client:
        client.get("/?token=not-the-token")
        assert client.cookies.get("accrue_ui_token") is None
        assert client.get("/api/run").status_code == 401


def test_evil_origin_403_even_with_valid_token(golden_log: Path):
    with client_for(golden_log) as client:
        resp = client.get("/api/run", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 403


def test_evil_referer_403(golden_log: Path):
    with client_for(golden_log) as client:
        resp = client.get("/api/run", headers={"Referer": "https://evil.example/x"})
        assert resp.status_code == 403


def test_loopback_origin_on_our_own_port_allowed(golden_log: Path):
    """Every spelling of *our* origin passes — host names differ, port does not."""
    with client_for(golden_log, port=7607, base_url="http://127.0.0.1:7607") as client:
        for origin in ("http://localhost:7607", "http://127.0.0.1:7607"):
            resp = client.get("/api/run", headers={"Origin": origin})
            assert resp.status_code == 200


def test_loopback_origin_on_another_port_403(golden_log: Path):
    """Loopback is not one origin: another local port is another site.

    Regression for the hostname-only Origin check — a page served by any
    other process on 127.0.0.1 could forge a request that rode our cookie.
    """
    with client_for(golden_log, port=7607, base_url="http://127.0.0.1:7607") as client:
        for origin in (
            "http://127.0.0.1:31337",
            "http://localhost:31337",
            "https://127.0.0.1:7607",  # different scheme, different origin
        ):
            resp = client.get("/api/run", headers={"Origin": origin})
            assert resp.status_code == 403, origin


def test_forged_cross_port_post_is_rejected(golden_log: Path):
    """The live-verified forgery: another loopback port + a simple-request body.

    ``text/plain`` keeps the browser from preflighting, and the HttpOnly
    cookie rides along automatically. Both halves are now closed — the
    Origin fails first, and the media type would fail next.
    """
    with client_for(golden_log, port=7607, base_url="http://127.0.0.1:7607") as client:
        forged = client.post(
            "/api/retry",
            content=b'{"all": true}',
            headers={
                "Origin": "http://localhost:31337",
                "Content-Type": "text/plain;charset=UTF-8",
            },
        )
        assert forged.status_code == 403
        # Same body from our own origin still trips the media-type gate.
        same_origin = client.post(
            "/api/retry",
            content=b'{"all": true}',
            headers={
                "Origin": "http://127.0.0.1:7607",
                "Content-Type": "text/plain;charset=UTF-8",
            },
        )
        assert same_origin.status_code == 415


def test_legit_same_origin_json_post_reaches_the_route(tmp_path: Path):
    """The real frontend POST still works end to end: 202, not a guard."""
    from tests.test_retry import FAKE_PIPELINE, _failing_log

    module = tmp_path / "sec_retry_mod.py"
    module.write_text(FAKE_PIPELINE)
    sys.path.insert(0, str(tmp_path))
    try:
        log = _failing_log(tmp_path)
        with client_for(
            log,
            port=7607,
            base_url="http://127.0.0.1:7607",
            pipeline="sec_retry_mod:make",
        ) as client:
            resp = client.post(
                "/api/retry",
                json={"all": True},
                headers={"Origin": "http://127.0.0.1:7607"},
            )
            assert resp.status_code == 202, resp.text
            assert resp.json() == {"accepted": 3}
            deadline = time.time() + 10
            while client.get("/api/run").json()["retry"]["running"]:
                assert time.time() < deadline, "retry never finished"
                time.sleep(0.05)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("sec_retry_mod", None)


def test_post_without_json_content_type_415(golden_log: Path):
    """A form/text POST is a no-preflight simple request: refuse it outright."""
    with client_for(golden_log) as client:
        for content_type in (
            "text/plain",
            "application/x-www-form-urlencoded",
            "multipart/form-data; boundary=x",
        ):
            resp = client.post(
                "/api/retry",
                content=b"all=true",
                headers={"Origin": "http://localhost", "Content-Type": content_type},
            )
            assert resp.status_code == 415, content_type


def test_json_content_type_with_charset_is_accepted(golden_log: Path):
    with client_for(golden_log) as client:
        resp = client.post(
            "/api/retry",
            content=b'{"all": true}',
            headers={
                "Origin": "http://localhost",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        assert resp.status_code == 409  # the route's own answer, not a guard


def test_opaque_origin_rejected(golden_log: Path):
    """``Origin: null`` (sandboxed iframe, file://) is not our origin."""
    with client_for(golden_log) as client:
        assert client.get("/api/run", headers={"Origin": "null"}).status_code == 403


def test_allow_origin_whitelists_one_extra_origin(golden_log: Path):
    origin = "http://dev.frontend.test:5173"
    with client_for(golden_log, allow_origin=origin) as client:
        assert client.get("/api/run", headers={"Origin": origin}).status_code == 200
        # Whitelisting is per-origin, not per-host: another port is not it.
        other = "http://dev.frontend.test:5174"
        assert client.get("/api/run", headers={"Origin": other}).status_code == 403


def test_non_loopback_host_403(golden_log: Path):
    """DNS-rebinding defense: a non-loopback Host header is rejected."""
    with client_for(golden_log, base_url="http://testserver") as client:
        assert client.get("/api/run").status_code == 403


def test_mutations_are_post_only(golden_log: Path):
    with client_for(golden_log) as client:
        assert client.get("/api/retry").status_code == 405


def test_post_without_origin_403(golden_log: Path):
    """Non-GET requests must prove their origin (browsers always send it)."""
    with client_for(golden_log) as client:
        assert client.post("/api/retry").status_code == 403


def test_post_with_loopback_origin_reaches_route(golden_log: Path):
    with client_for(golden_log) as client:
        resp = client.post(
            "/api/retry", json={"all": True}, headers={"Origin": "http://localhost"}
        )
        assert resp.status_code == 409  # the route's own answer, not a 4xx guard


# ---- the retry mutation carries the same guards ---------------------------


def test_retry_without_token_401(golden_log: Path):
    with client_for(golden_log, authed=False) as client:
        resp = client.post(
            "/api/retry",
            json={"all": True},
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 401


def test_retry_with_evil_origin_403(golden_log: Path):
    with client_for(golden_log) as client:
        resp = client.post(
            "/api/retry",
            json={"all": True},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403


def test_retry_via_cookie_alone_reaches_the_route(golden_log: Path):
    """The frontend POSTs same-origin with no token header: the cookie auths."""
    with client_for(golden_log, authed=False) as client:
        client.get(f"/?token={TOKEN}")
        resp = client.post(
            "/api/retry",
            json={"all": True},
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 409  # unavailable (no --pipeline), not 401


def test_empty_token_rejected_at_construction(golden_log: Path):
    from accrue_ui.server.app import create_app

    with pytest.raises(ValueError):
        create_app(golden_log, token="")
