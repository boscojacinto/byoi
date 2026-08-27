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


def route_for(session_id: str, upstream: str) -> dict[str, Any]:
    return {
        "@id": route_id(session_id),
        "match": [{"host": [seat_hostname(session_id)]}],
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


def publish(session_id: str, upstream: str) -> str:
    """Route this session's hostname at its seat container. Returns the hostname.

    The route is inserted at index 0, ahead of the wildcard site's catch-all. A
    route appended after it would never be reached: ``*.domain`` matches the
    session hostname too, and answers 404.
    """
    host = seat_hostname(session_id)
    if not enabled():
        return host
    with _client() as client:
        server = https_server(client)
        unpublish(session_id, client=client)
        res = client.put(
            f"/config/apps/http/servers/{server}/routes/0",
            content=json.dumps(route_for(session_id, upstream)),
            headers={"Content-Type": "application/json"},
        )
        _raise_for(res, f"publishing {host}")
    return host


def unpublish(session_id: str, *, client: httpx.Client | None = None) -> bool:
    """Remove the route. True if one was there; a missing route is not an error."""
    if not enabled():
        return False
    owned = client is None
    client = client or _client()
    try:
        res = client.delete(f"/id/{route_id(session_id)}")
        if res.status_code == 404 or (res.status_code >= 400 and "unknown object" in res.text):
            return False
        _raise_for(res, f"removing the route for session {session_id}")
        return True
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
