"""Security middleware: token, cookie flow, Origin/Host checks, POST-only."""

from __future__ import annotations

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


def test_loopback_origin_allowed(golden_log: Path):
    with client_for(golden_log) as client:
        for origin in ("http://localhost:7607", "http://127.0.0.1:7607"):
            resp = client.get("/api/run", headers={"Origin": origin})
            assert resp.status_code == 200


def test_allow_origin_whitelists_extra_host(golden_log: Path):
    origin = "http://dev.frontend.test:5173"
    with client_for(golden_log, allow_origin=origin) as client:
        resp = client.get("/api/run", headers={"Origin": origin})
        assert resp.status_code == 200


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
        resp = client.post("/api/retry", headers={"Origin": "http://localhost"})
        assert resp.status_code == 409  # the route's own answer, not a 4xx guard


def test_empty_token_rejected_at_construction(golden_log: Path):
    from accrue_ui.server.app import create_app

    with pytest.raises(ValueError):
        create_app(golden_log, token="")
