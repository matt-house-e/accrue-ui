"""Security middleware: launch token + Origin/Host checks.

Threat model: a local, single-user dashboard. The dangers are other local
processes and hostile web pages the user has open (CSRF, DNS rebinding) —
not the network at large. **Binding the server to 127.0.0.1 only is the
CLI's job (``cli.py``, hardcoded — no flag to widen it)**; this module
assumes loopback binding and adds the browser-facing layers:

- **Launch token.** ``?token=<t>`` on the first page hit sets an HttpOnly
  cookie; after that every ``/api/*`` request must present the token via the
  cookie or the ``X-Accrue-Token`` header, else 401. Comparison is
  constant-time.
- **Origin/Host checks.** ``/api/*`` requests whose Origin (or Referer)
  names a non-loopback host are rejected with 403 — a hostile page cannot
  ride the cookie cross-site. A missing Origin is allowed only for GET/HEAD
  (same-origin GETs omit it); anything else must prove its origin. The Host
  header must be a loopback name too (DNS-rebinding defense).
- **Mutations are POST-only** — enforced by the route table (GET on
  ``/api/retry`` is 405), so a simple cross-site ``<img>``/link can never
  mutate.
"""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

TOKEN_COOKIE = "accrue_ui_token"
TOKEN_HEADER = "X-Accrue-Token"
TOKEN_QUERY_PARAM = "token"

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _host_of(origin_or_url: str) -> str | None:
    """Hostname (no port, lowercased) of an Origin/Referer value, else None."""
    try:
        host = urlsplit(origin_or_url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _token_matches(supplied: str | None, expected: str) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def install_security(
    app: FastAPI, *, token: str, allow_origin: str | None = None
) -> None:
    """Wire the token + Origin/Host middleware onto *app*.

    ``allow_origin`` optionally whitelists one extra non-loopback origin
    (e.g. a dev frontend on another port); its host is then also accepted.
    """
    if not token:
        raise ValueError("a non-empty launch token is required")

    extra_origin_hosts: set[str] = set()
    if allow_origin:
        host = _host_of(allow_origin)
        if host:
            extra_origin_hosts.add(host)

    allowed_hosts = LOOPBACK_HOSTS | extra_origin_hosts

    @app.middleware("http")
    async def security_middleware(request: Request, call_next) -> Response:
        path = request.url.path

        if path == "/api" or path.startswith("/api/"):
            # 1. Host header must be loopback (DNS-rebinding defense).
            host = (request.url.hostname or "").lower()
            if host not in allowed_hosts:
                return JSONResponse({"detail": "forbidden host"}, status_code=403)

            # 2. Origin (or Referer) must be loopback when present. Absent is
            #    fine for GET/HEAD only — browsers omit Origin on same-origin
            #    GETs but always send it on cross-site requests.
            source = request.headers.get("origin") or request.headers.get("referer")
            if source is not None:
                if _host_of(source) not in allowed_hosts:
                    return JSONResponse({"detail": "forbidden origin"}, status_code=403)
            elif request.method not in ("GET", "HEAD"):
                return JSONResponse({"detail": "missing origin"}, status_code=403)

            # 3. Launch token via header or cookie, constant-time compare.
            supplied = request.headers.get(TOKEN_HEADER) or request.cookies.get(
                TOKEN_COOKIE
            )
            if not _token_matches(supplied, token):
                return JSONResponse(
                    {"detail": "missing or invalid token"}, status_code=401
                )

            return await call_next(request)

        # Non-API (page/static) request: a valid ?token= query parameter
        # sets the HttpOnly session cookie for subsequent /api calls.
        response = await call_next(request)
        supplied = request.query_params.get(TOKEN_QUERY_PARAM)
        if supplied is not None and _token_matches(supplied, token):
            response.set_cookie(
                TOKEN_COOKIE,
                token,
                httponly=True,
                samesite="strict",
                path="/",
            )
        return response
