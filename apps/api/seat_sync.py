"""Host → seat OTP push over mTLS. Guest HTTP on :8787 cannot admit."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from apps.host_token import host_token, token_is_weak
from apps.tls import host_ssl_context, paths


class SeatSyncError(Exception):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"seat sync failed ({status}): {body[:240]}")


def control_base(seat: dict[str, Any] | None = None) -> str:
    import os

    override = os.environ.get("BYOI_SEAT_CONTROL_URL") or os.environ.get("BYOI_SEAT_URL", "")
    override = override.rstrip("/")
    if override:
        parsed = urlparse(override if "://" in override else f"https://{override}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8788
        return f"https://{host}:{port}"
    guest = (seat or {}).get("agent_url") or "http://127.0.0.1:8787"
    parsed = urlparse(guest if "://" in guest else f"http://{guest}")
    host = parsed.hostname or "127.0.0.1"
    port = int(os.environ.get("BYOI_CONTROL_PORT", "8788"))
    return f"https://{host}:{port}"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {host_token()}", "Content-Type": "application/json"}


def _client() -> httpx.Client:
    if token_is_weak():
        raise SeatSyncError(503, "set a non-default BYOI_HOST_TOKEN (scripts/salon-tls.sh)")
    p = paths()
    if not p.host_ready():
        raise SeatSyncError(503, "missing salon TLS files — run scripts/salon-tls.sh")
    timeout = 8.0
    return httpx.Client(verify=host_ssl_context(), timeout=timeout)


def seat_status(seat: dict[str, Any] | None, *, timeout: float = 3.0) -> dict[str, Any]:
    """Readiness probe for a freshly raised seat.

    Deliberately over the mTLS port rather than the guest one: an answer here
    proves the app is up *and* that it loaded the certificate the desk just
    minted for it, which is everything that has to be true before an OTP is
    pushed.
    """
    url = control_base(seat) + "/local/control-health"
    try:
        with httpx.Client(verify=host_ssl_context(), timeout=timeout) as client:
            res = client.get(url)
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {"ok": True}


def admit_session(seat: dict[str, Any], sess: dict[str, Any]) -> dict[str, Any]:
    url = control_base(seat) + "/local/admit"
    payload = {
        "otp": sess["unlock_otp"],
        "session_id": sess["id"],
        "coder_name": sess.get("coder_name"),
        "seat_id": sess.get("seat_id"),
    }
    try:
        with _client() as client:
            res = client.post(url, json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {"ok": True}


def set_workspace(seat: dict[str, Any] | None, path: str) -> dict[str, Any]:
    url = control_base(seat) + "/local/workspace"
    try:
        with _client() as client:
            res = client.post(url, json={"path": path}, headers=_headers())
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {"ok": True}


def submit_solution(
    seat: dict[str, Any] | None,
    *,
    session_id: str,
    cwd: str | None = None,
    push: bool = False,
    kind: str = "submission",
) -> dict[str, Any]:
    """Fire the seat's submit hook and pin the guest's tree to a fetchable git ref."""
    url = control_base(seat) + "/local/submit"
    payload = {"session_id": session_id, "cwd": cwd, "push": push, "kind": kind}
    try:
        with httpx.Client(verify=host_ssl_context(), timeout=60.0) as client:
            res = client.post(url, json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {}


def infra_up(
    seat: dict[str, Any] | None, *, session_id: str, cwd: str | None = None
) -> dict[str, Any]:
    """Start the seat's Postgres/Redis/auth for this session."""
    url = control_base(seat) + "/local/infra/up"
    try:
        with httpx.Client(verify=host_ssl_context(), timeout=300.0) as client:
            res = client.post(url, json={"session_id": session_id, "cwd": cwd}, headers=_headers())
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {}


def push_infra_env(
    seat: dict[str, Any] | None,
    *,
    session_id: str,
    env: dict[str, str],
    cwd: str | None = None,
) -> dict[str, Any]:
    """Hand a cloud seat the URLs for the stack the desk raised for it."""
    url = control_base(seat) + "/local/infra/env"
    payload = {"session_id": session_id, "env": env, "cwd": cwd}
    try:
        with _client() as client:
            res = client.post(url, json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {}


def infra_down(seat: dict[str, Any] | None, *, session_id: str) -> dict[str, Any]:
    url = control_base(seat) + "/local/infra/down"
    try:
        with _client() as client:
            res = client.post(url, json={"session_id": session_id}, headers=_headers())
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {}


def infra_status(seat: dict[str, Any] | None, *, session_id: str) -> dict[str, Any]:
    url = control_base(seat) + "/local/infra"
    try:
        with _client() as client:
            res = client.get(url, params={"session_id": session_id}, headers=_headers())
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {}


def verify_solution(
    seat: dict[str, Any] | None,
    *,
    spec: str,
    title: str = "",
    cwd: str | None = None,
) -> dict[str, Any]:
    url = control_base(seat) + "/local/verify"
    payload = {"spec": spec, "title": title, "cwd": cwd}
    try:
        with httpx.Client(verify=host_ssl_context(), timeout=200.0) as client:
            res = client.post(url, json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {"cases": []}


def live_snapshot(seat: dict[str, Any] | None) -> dict[str, Any]:
    """Guest chat history on this seat — used by the desk Live pane."""
    url = control_base(seat) + "/local/live"
    try:
        with _client() as client:
            res = client.get(url, headers=_headers())
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {"history": []}


def list_accounts(seat: dict[str, Any] | None = None) -> dict[str, Any]:
    url = control_base(seat) + "/local/accounts"
    try:
        with _client() as client:
            res = client.get(url, headers=_headers())
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {"accounts": []}


def usage_stats(seat: dict[str, Any] | None = None) -> dict[str, Any]:
    url = control_base(seat) + "/local/usage"
    try:
        with _client() as client:
            res = client.get(url, headers=_headers())
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {"quota": None, "stats": None}


def revoke_session(seat: dict[str, Any] | None) -> dict[str, Any]:
    url = control_base(seat) + "/local/revoke"
    try:
        with _client() as client:
            res = client.post(url, json={}, headers=_headers())
    except httpx.HTTPError as exc:
        raise SeatSyncError(0, str(exc)) from exc
    if res.status_code >= 400:
        raise SeatSyncError(res.status_code, res.text)
    return res.json() if res.content else {"ok": True}
