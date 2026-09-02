"""mTLS control plane: host admits/revokes OTP. Not exposed on the guest HTTP port."""

from __future__ import annotations

import os
from pathlib import Path

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
    kind: str = "submission"


class InfraIn(BaseModel):
    session_id: str
    cwd: str | None = None


class InfraEnvIn(BaseModel):
    session_id: str
    env: dict[str, str]
    cwd: str | None = None


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
        "byo": chat_session.byo,
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
    # Grab the session before the gate forgets it — the local stack is keyed on it.
    session_id = gate.snapshot().get("session_id")
    gate.revoke()
    from . import guest_auth
    from .claude_chat import session as chat_session
    from .infra import down

    chat_session.reset()
    chat_session.assign_account(None)
    infra = {"removed": False}
    # Revoke the guest's own Claude token and erase the ephemeral account before
    # anything slower runs. Deleting the file alone would leave a live refresh
    # token; teardown logs out first, then unlinks.
    byo = guest_auth.teardown_sync(session_id)
    if session_id:
        try:
            infra = down(str(session_id))
        except Exception as exc:  # teardown must never block freeing the seat
            infra = {"ok": False, "removed": False, "detail": str(exc)}
    return {"ok": True, "seat_id": SEAT_ID, "admitted": False, "infra": infra, "byo": byo}


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
        "byo": chat_session.byo,
        "quota": chat_session.quota,
        "accounts": chat_session.pool.snapshot(current=chat_session.account_label),
        "handoff": bool(chat_session.handoff_text),
    }


@app.get("/local/usage")
def usage(
    request: Request,
    label: str | None = None,
    since: float | None = None,
    until: float | None = None,
    authorization: str | None = Header(default=None),
) -> dict:
    """Rate-limit snapshot plus day/hour token breakdowns, for the floor's usage panel.

    `label` picks which Claude account to report on — the seat this chair is
    assigned, not necessarily whoever is live right now (one seat process can
    serve several named chairs in `static` mode). `stats` is that account's
    full history ("Seat"); `guest_stats`, present only when `since` is given,
    is the same account restricted to one visit's window ("Guest").
    """
    _require_host(request, authorization)
    from .claude_chat import session as chat_session
    from .usage_stats import usage_report

    chat_session.refresh_quota()
    if label:
        account = chat_session.pool.get(label)
        is_live = account is not None and account.label == chat_session.account_label
        config_dir = account.config_dir if account else None
        resolved_label = account.label if account else label
    else:
        is_live = True
        config_dir = chat_session.config_dir
        resolved_label = chat_session.account_label
    return {
        "seat_id": SEAT_ID,
        "account": resolved_label,
        "guest_name": gate.snapshot().get("coder_name") if is_live else None,
        "quota": chat_session.quota if is_live else None,
        "stats": usage_report(config_dir),
        "guest_stats": usage_report(config_dir, since=since, until=until) if since is not None else None,
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
        captured = capture(
            cwd=cwd, session_id=body.session_id, push=body.push, kind=body.kind
        )
    except SubmissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "seat_id": SEAT_ID,
        "hooked": hook is not None,
        "transcript_path": (hook or {}).get("transcript_path"),
        **captured,
    }


@app.post("/local/infra/up")
def infra_up(request: Request, body: InfraIn, authorization: str | None = Header(default=None)) -> dict:
    """Start this session's Postgres/Redis/auth and write them into .env.local."""
    _require_host(request, authorization)
    from .claude_chat import session as chat_session
    from .infra import InfraError, public_env, up

    cwd = body.cwd or (str(chat_session.workspace_path) if chat_session.workspace_path else None)
    if not cwd:
        raise HTTPException(409, "seat has no workspace for this session")
    try:
        env = up(session_id=body.session_id, cwd=cwd)
    except InfraError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "seat_id": SEAT_ID, "cwd": cwd, "env": public_env(env)}


@app.post("/local/infra/env")
def infra_env(request: Request, body: InfraEnvIn, authorization: str | None = Header(default=None)) -> dict:
    """Write URLs for a stack the *desk* raised into the project's .env.local.

    The seat has no Docker and cannot start these containers — see
    apps/api/infra.py for why. What it still owns is the file: only the salon's
    managed block is rewritten, so a guest's own variables survive.
    """
    _require_host(request, authorization)
    from .claude_chat import session as chat_session
    from .infra import public_env, write_env_file

    cwd = body.cwd or (str(chat_session.workspace_path) if chat_session.workspace_path else None)
    if not cwd:
        raise HTTPException(409, "seat has no workspace for this session")
    project_dir = Path(cwd).expanduser()
    if not project_dir.is_dir():
        raise HTTPException(409, f"not a directory: {project_dir}")
    written = write_env_file(project_dir, body.env)
    return {
        "ok": True,
        "seat_id": SEAT_ID,
        "cwd": str(project_dir),
        "env_file": str(written),
        "env": public_env(body.env),
    }


@app.post("/local/infra/down")
def infra_down(request: Request, body: InfraIn, authorization: str | None = Header(default=None)) -> dict:
    _require_host(request, authorization)
    from .infra import down

    return {"seat_id": SEAT_ID, **down(body.session_id)}


@app.get("/local/infra")
def infra_status(
    request: Request, session_id: str, authorization: str | None = Header(default=None)
) -> dict:
    _require_host(request, authorization)
    from .infra import status

    return {"seat_id": SEAT_ID, "session_id": session_id, **status(session_id)}


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
