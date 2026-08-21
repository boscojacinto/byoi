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
    return httpx.Client(verify=host_ssl_context(), timeout=8.0)


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
