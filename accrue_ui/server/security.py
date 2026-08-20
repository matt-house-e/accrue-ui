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
- **Origin/Host checks.** ``/api/*`` requests are matched against the
  server's **own full origin** — ``scheme://host:port``, not just the
  hostname. Loopback is not one origin: ``http://127.0.0.1:31337`` is a
  different site that a hostile local process can serve, and the browser
  attaches our cookie to a request it forges at us. Comparing hostnames
  alone let that request through; comparing the whole origin does not. A
  missing Origin is allowed only for GET/HEAD (same-origin GETs omit it);
  anything else must prove its origin. The Host header must be a loopback
  name too (DNS-rebinding defense).
- **Mutations are POST-only** — enforced by the route table (GET on
  ``/api/retry`` is 405), so a simple cross-site ``<img>``/link can never
  mutate.
- **Mutations must be JSON.** A cross-origin ``fetch`` with a
  ``text/plain``/form body is a *simple request*: the browser sends it
  without a preflight, so the Origin check is the only thing standing in
  front of it. Requiring ``Content-Type: application/json`` (415 otherwise)
  forces a preflight, which a hostile origin cannot pass.
"""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

TOKEN_COOKIE = "accrue_ui_token"
TOKEN_HEADER = "X-Accrue-Token"
TOKEN_QUERY_PARAM = "token"

#: Host-header spellings of the loopback interface (DNS-rebinding defense).
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
#: How a browser on this machine spells our own origin's host.
LOOPBACK_ORIGIN_HOSTS = ("localhost", "127.0.0.1", "[::1]")
DEFAULT_PORTS = {"http": 80, "https": 443}
#: Methods that cannot mutate; everything else must prove origin AND intent.
SAFE_METHODS = frozenset({"GET", "HEAD"})
JSON_MEDIA_TYPE = "application/json"


def _host_of(origin_or_url: str) -> str | None:
    """Hostname (no port, lowercased) of an Origin/Referer value, else None."""
    try:
        host = urlsplit(origin_or_url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _normalize_origin(origin_or_url: str) -> str | None:
    """``scheme://host:port`` with the default port made explicit, else None.

    ``http://localhost`` and ``http://localhost:80`` are the same origin and
    normalize identically; ``http://localhost:31337`` does not, which is the
    whole point (see the module docstring). ``null`` and other opaque
    origins have no scheme and are rejected.
    """
    try:
        parts = urlsplit(origin_or_url)
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        return None
    if port is None:
        port = DEFAULT_PORTS.get(scheme)
        if port is None:
            return None
    if ":" in host:  # IPv6 literal: origins spell it bracketed
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


def _self_origins(port: int) -> set[str]:
    """Every origin string a browser may use for *our* server on *port*."""
    return {f"http://{host}:{port}" for host in LOOPBACK_ORIGIN_HOSTS}


def _media_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _token_matches(supplied: str | None, expected: str) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def install_security(
    app: FastAPI,
    *,
    token: str,
    port: int | None = None,
    allow_origin: str | None = None,
) -> None:
    """Wire the token + Origin/Host middleware onto *app*.

    ``port`` is the port the server will bind — known at app creation, and
    what the Origin check compares against. When it is omitted (tests,
    embedders) the port of the request's own Host header stands in: a
    browser sets Host from the URL it is calling, so a forged cross-origin
    request still carries *our* authority there and *their* Origin.

    ``allow_origin`` optionally whitelists one extra origin (e.g. a dev
    frontend on another port); it is matched in full, port included.
    """
    if not token:
        raise ValueError("a non-empty launch token is required")

    extra_origins: set[str] = set()
    extra_hosts: set[str] = set()
    if allow_origin:
        normalized = _normalize_origin(allow_origin)
        if normalized:
            extra_origins.add(normalized)
        host = _host_of(allow_origin)
        if host:
            extra_hosts.add(host)

    allowed_hosts = LOOPBACK_HOSTS | extra_hosts
    fixed_origins = _self_origins(port) | extra_origins if port is not None else None

    @app.middleware("http")
    async def security_middleware(request: Request, call_next) -> Response:
        path = request.url.path

        if path == "/api" or path.startswith("/api/"):
            # 1. Host header must be loopback (DNS-rebinding defense).
            host = (request.url.hostname or "").lower()
            if host not in allowed_hosts:
                return JSONResponse({"detail": "forbidden host"}, status_code=403)

            # 2. Origin (or Referer) must be THIS origin — scheme, host and
            #    port. Absent is fine for GET/HEAD only: browsers omit Origin
            #    on same-origin GETs but always send it on cross-site
            #    requests.
            if fixed_origins is not None:
                allowed_origins = fixed_origins
            else:
                own_port = request.url.port or DEFAULT_PORTS.get(request.url.scheme, 80)
                allowed_origins = _self_origins(own_port) | extra_origins
            source = request.headers.get("origin") or request.headers.get("referer")
            if source is not None:
                if _normalize_origin(source) not in allowed_origins:
                    return JSONResponse({"detail": "forbidden origin"}, status_code=403)
            elif request.method not in SAFE_METHODS:
                return JSONResponse({"detail": "missing origin"}, status_code=403)

            # 3. A mutation must declare a JSON body, so it can never be a
            #    no-preflight "simple request" (see the module docstring).
            declared = _media_type(request.headers.get("content-type"))
            if request.method not in SAFE_METHODS and declared != JSON_MEDIA_TYPE:
                return JSONResponse(
                    {"detail": "expected Content-Type: application/json"},
                    status_code=415,
                )

            # 4. Launch token via header or cookie, constant-time compare.
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
