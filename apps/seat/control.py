"""mTLS control plane: host admits/revokes OTP. Not exposed on the guest HTTP port."""

from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from apps.host_token import host_token, token_is_weak, token_matches
from apps.seat.gate import gate
from apps.seat.tmux_claude import SESSION

SEAT_ID = os.environ.get("BYOI_SEAT_ID", "seat-1")


class AdmitIn(BaseModel):
    otp: str
    session_id: str
    coder_name: str | None = None
    seat_id: str | None = None


app = FastAPI(title="BYOI seat control")


def _host_ip_allowed(request: Request) -> bool:
    """Optional extra lock. Empty BYOI_HOST_IPS means 'certificate is enough'."""
    allowed = os.environ.get("BYOI_HOST_IPS", "").strip()
    if not allowed:
        return True
    client = request.client.host if request.client else ""
    names = {p.strip() for p in allowed.split(",") if p.strip()}
    return client in names or client in {"127.0.0.1", "::1", "testclient"}


def _require_host(request: Request, authorization: str | None) -> None:
    if token_is_weak():
        raise HTTPException(503, "set a non-default BYOI_HOST_TOKEN (scripts/salon-tls.sh)")
    if not _host_ip_allowed(request):
        raise HTTPException(403, "host IP is not allowed (BYOI_HOST_IPS)")
    if not token_matches(authorization):
        raise HTTPException(401, "host token required")


@app.get("/local/control-health")
def health() -> dict:
    return {"ok": True, "seat_id": SEAT_ID, "mtls": True, "tmux": SESSION, **gate.snapshot()}


@app.post("/local/admit")
def admit(request: Request, body: AdmitIn, authorization: str | None = Header(default=None)) -> dict:
    _require_host(request, authorization)
    try:
        admitted = gate.admit(
            otp=body.otp,
            session_id=body.session_id,
            coder_name=body.coder_name,
            seat_id=body.seat_id or SEAT_ID,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**admitted, "seat_id": SEAT_ID}


@app.post("/local/revoke")
def revoke(request: Request, authorization: str | None = Header(default=None)) -> dict:
    _require_host(request, authorization)
    gate.revoke()
    return {"ok": True, "seat_id": SEAT_ID, "admitted": False}
