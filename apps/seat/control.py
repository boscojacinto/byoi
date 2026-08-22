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


class WorkspaceIn(BaseModel):
    path: str


class VerifyIn(BaseModel):
    spec: str
    title: str = ""
    cwd: str | None = None


class AccountIn(BaseModel):
    label: str


class SubmitIn(BaseModel):
    session_id: str
    cwd: str | None = None
    push: bool = False


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
    from .claude_chat import session as chat_session

    return {
        "ok": True,
        "seat_id": SEAT_ID,
        "mtls": True,
        "tmux": SESSION,
        "account": chat_session.account_label,
        "accounts": chat_session.pool.snapshot(current=chat_session.account_label),
        **gate.snapshot(),
    }


@app.get("/local/live")
def live(request: Request, authorization: str | None = Header(default=None)) -> dict:
    _require_host(request, authorization)
    from .claude_chat import session as chat_session

    snap = chat_session.snapshot()
    return {"seat_id": SEAT_ID, "gate": gate.snapshot(), **snap}


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
    from .claude_chat import session as chat_session

    chat_session.reset()
    account = chat_session.assign_preferred(body.seat_id or SEAT_ID)
    return {**admitted, "seat_id": SEAT_ID, "account": account.label if account else None}


@app.post("/local/revoke")
def revoke(request: Request, authorization: str | None = Header(default=None)) -> dict:
    _require_host(request, authorization)
    gate.revoke()
    from .claude_chat import session as chat_session

    chat_session.reset()
    chat_session.assign_account(None)
    return {"ok": True, "seat_id": SEAT_ID, "admitted": False}


@app.post("/local/workspace")
def set_workspace(request: Request, body: WorkspaceIn, authorization: str | None = Header(default=None)) -> dict:
    _require_host(request, authorization)
    from .claude_chat import session as chat_session

    try:
        path = chat_session.set_workspace(body.path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {"ok": True, "cwd": str(path), "seat_id": SEAT_ID}


@app.get("/local/accounts")
def list_accounts(request: Request, authorization: str | None = Header(default=None)) -> dict:
    _require_host(request, authorization)
    from .claude_chat import session as chat_session

    return {
        "seat_id": SEAT_ID,
        "account": chat_session.account_label,
        "quota": chat_session.quota,
        "accounts": chat_session.pool.snapshot(current=chat_session.account_label),
        "handoff": bool(chat_session.handoff_text),
    }


@app.post("/local/account")
async def switch_account(
    request: Request, body: AccountIn, authorization: str | None = Header(default=None)
) -> dict:
    _require_host(request, authorization)
    from .claude_chat import session as chat_session

    account = chat_session.pool.get(body.label)
    if account is None or not account.credentialed:
        raise HTTPException(404, "unknown Claude account")
    await chat_session.switch_account(account, reason="host")
    return {"ok": True, "seat_id": SEAT_ID, **chat_session.snapshot()}


@app.post("/local/submit")
async def submit(
    request: Request, body: SubmitIn, authorization: str | None = Header(default=None)
) -> dict:
    """Fire the seat's UserPromptSubmit hook, then pin the tree to a fetchable ref."""
    _require_host(request, authorization)
    from .claude_chat import session as chat_session
    from .submission import SubmissionError, capture

    hook = await chat_session.signal_submit(body.session_id)
    cwd = (
        body.cwd
        or (hook or {}).get("cwd")
        or (str(chat_session.workspace_path) if chat_session.workspace_path else None)
    )
    if not cwd:
        raise HTTPException(409, "seat has no workspace for this session")
    try:
        captured = capture(cwd=cwd, session_id=body.session_id, push=body.push)
    except SubmissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "seat_id": SEAT_ID,
        "hooked": hook is not None,
        "transcript_path": (hook or {}).get("transcript_path"),
        **captured,
    }


@app.post("/local/verify")
def verify(request: Request, body: VerifyIn, authorization: str | None = Header(default=None)) -> dict:
    _require_host(request, authorization)
    from .claude_chat import session as chat_session
    from .verify import run_verify

    cwd = body.cwd or (str(chat_session.workspace_path) if chat_session.workspace_path else None)
    try:
        return run_verify(spec=body.spec, title=body.title, cwd=cwd)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
