"""Add and remove a public hostname for a live seat.

Seats are created per visit, so their routes are too. Caddy's admin API takes
config edits at runtime, which is why the edge is Caddy rather than a static
nginx that would need a reload and a config file per session.

The admin endpoint is a **unix socket** on a volume shared only with the desk.
It cannot be a TCP port: seat containers sit on the same edge network, the
guest's Claude has a shell there, and anything that can reach the admin API can
point any hostname at anything.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

ROUTE_ID_PREFIX = "byoi-seat-"
PREVIEW_ROUTE_ID_PREFIX = "byoi-preview-"
TIMEOUT = 10.0


class CaddyError(RuntimeError):
    """A precondition the desk should report, not a 500."""


def admin_target() -> str:
    """``/path/to/admin.sock``, an ``http://host:port``, or empty when off."""
    return os.environ.get("BYOI_CADDY_ADMIN", "").strip()


def enabled() -> bool:
    return bool(admin_target())


def domain() -> str:
    return os.environ.get("BYOI_DOMAIN", "").strip()


def seat_hostname(session_id: str) -> str:
    """The address on the slip. Short, because it is typed as often as scanned."""
    base = domain()
    return f"s-{session_id}.{base}" if base else f"s-{session_id}"


def route_id(session_id: str) -> str:
    return f"{ROUTE_ID_PREFIX}{session_id}"


def preview_hostname(session_id: str) -> str:
    """Where the guest's own phone reaches the dev server they are building.

    A seat's Claude can look at ``127.0.0.1:3000`` from inside the container,
    but the guest cannot: on cellular their phone only ever reaches the edge.
    One more route per visit is what turns "does it look right?" into something
    the person who asked can answer for themselves.
    """
    base = domain()
    return f"p-{session_id}.{base}" if base else f"p-{session_id}"


def preview_route_id(session_id: str) -> str:
    return f"{PREVIEW_ROUTE_ID_PREFIX}{session_id}"


def preview_port() -> int | None:
    """The dev server port to publish, or None when the operator wants none.

    3000 is not a guess: it is the port ``infra.py`` already writes into
    ``AUTH_URL`` for every templated project. Empty or 0 turns the route off --
    unlike the seat, what it exposes has no OTP in front of it.
    """
    raw = os.environ.get("BYOI_PREVIEW_PORT", "3000").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if port > 0 else None


def _client() -> httpx.Client:
    target = admin_target()
    if not target:
        raise CaddyError("BYOI_CADDY_ADMIN is not set — no edge to publish a seat on")
    if target.startswith("http://") or target.startswith("https://"):
        return httpx.Client(base_url=target.rstrip("/"), timeout=TIMEOUT)
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=target),
        base_url="http://caddy-admin",
        timeout=TIMEOUT,
    )


def _raise_for(res: httpx.Response, what: str) -> None:
    if res.status_code >= 400:
        raise CaddyError(f"{what} failed ({res.status_code}): {res.text[:240]}")


def https_server(client: httpx.Client) -> str:
    """Name of the server holding :443.

    Caddy names servers itself when the config comes from a Caddyfile (`srv0`,
    `srv1`, …) and the numbering depends on the order sites were parsed, so it
    is looked up rather than assumed.
    """
    res = client.get("/config/apps/http/servers")
    _raise_for(res, "reading the Caddy config")
    servers: dict[str, Any] = res.json() or {}
    for name, server in servers.items():
        listen = server.get("listen") or []
        if any(str(addr).endswith(":443") for addr in listen):
            return name
    raise CaddyError("no Caddy server is listening on :443")


def _route(rid: str, hostname: str, upstream: str) -> dict[str, Any]:
    return {
        "@id": rid,
        "match": [{"host": [hostname]}],
        "handle": [
            {
                "handler": "subroute",
                "routes": [
                    {
                        "handle": [
                            {
                                "handler": "reverse_proxy",
                                "upstreams": [{"dial": upstream}],
                            }
                        ]
                    }
                ],
            }
        ],
        "terminal": True,
    }


def route_for(session_id: str, upstream: str) -> dict[str, Any]:
    return _route(route_id(session_id), seat_hostname(session_id), upstream)


def preview_route_for(session_id: str, upstream: str) -> dict[str, Any]:
    return _route(preview_route_id(session_id), preview_hostname(session_id), upstream)


def _insert(client: httpx.Client, route: dict[str, Any], what: str) -> None:
    """Put one route at index 0, ahead of the wildcard site's catch-all.

    A route appended after it would never be reached: ``*.domain`` matches the
    session hostname too, and answers 404.
    """
    server = https_server(client)
    _delete(client, str(route["@id"]), what)
    res = client.put(
        f"/config/apps/http/servers/{server}/routes/0",
        content=json.dumps(route),
        headers={"Content-Type": "application/json"},
    )
    _raise_for(res, what)


def _delete(client: httpx.Client, rid: str, what: str) -> bool:
    res = client.delete(f"/id/{rid}")
    if res.status_code == 404 or (res.status_code >= 400 and "unknown object" in res.text):
        return False
    _raise_for(res, what)
    return True


def publish(session_id: str, upstream: str) -> str:
    """Route this session's hostname at its seat container. Returns the hostname."""
    host = seat_hostname(session_id)
    if not enabled():
        return host
    with _client() as client:
        _insert(client, route_for(session_id, upstream), f"publishing {host}")
    return host


def publish_preview(session_id: str, container: str) -> str | None:
    """Route ``p-<session>`` at the dev server inside that seat.

    Returns the hostname, or None when previews are switched off. Nothing here
    checks that anything is listening: the guest starts and stops their own dev
    server all visit, and a 502 between runs is the honest answer.

    **What this publishes is public.** The seat next door has an OTP in front of
    it; a dev server does not, and cannot be given one without the salon sitting
    inside the guest's own app. The hostname is unlisted and dies at checkout --
    the same bargain ``docs/salon.md`` already strikes for a Vercel preview.
    """
    port = preview_port()
    if port is None:
        return None
    host = preview_hostname(session_id)
    if not enabled():
        return host
    with _client() as client:
        _insert(client, preview_route_for(session_id, f"{container}:{port}"), f"publishing {host}")
    return host


def unpublish(session_id: str, *, client: httpx.Client | None = None) -> bool:
    """Remove this session's routes. True if either was there; missing is not
    an error.

    Both go together. A preview route left behind is a public hostname pointed
    at a container name the next visit could be given.
    """
    if not enabled():
        return False
    owned = client is None
    client = client or _client()
    try:
        what = f"removing the routes for session {session_id}"
        seat = _delete(client, route_id(session_id), what)
        preview = _delete(client, preview_route_id(session_id), what)
        return seat or preview
    finally:
        if owned:
            client.close()


def published() -> list[str]:
    """Session ids the edge currently has a route for — for reconciling after a
    desk restart, when the database and Caddy can disagree."""
    if not enabled():
        return []
    with _client() as client:
        server = https_server(client)
        res = client.get(f"/config/apps/http/servers/{server}/routes")
        _raise_for(res, "reading routes")
        out = []
        for route in res.json() or []:
            rid = route.get("@id", "")
            if rid.startswith(ROUTE_ID_PREFIX):
                out.append(rid[len(ROUTE_ID_PREFIX) :])
        return out
