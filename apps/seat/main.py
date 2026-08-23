"""Seat agent: cafe Wi-Fi guest PWA + OTP-gated Claude Code chat."""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .claude_chat import default_workspace, list_workspace, session as chat_session
from .gate import gate
from .pty_ws import attach_tmux
from .tmux_claude import SESSION, ensure_session

TRANSPORT = os.environ.get("BYOI_TRANSPORT", "wifi")
LAN_CIDR = os.environ.get("BYOI_LAN_CIDR", "")
SEAT_ID = os.environ.get("BYOI_SEAT_ID", "seat-1")
SEAT_NAME = os.environ.get("BYOI_SEAT_NAME", "Seat 1")
HOUSE = os.environ.get("BYOI_HOUSE_URL", "http://127.0.0.1:8080")
UNLOCK_OPEN = os.environ.get("BYOI_UNLOCK_OPEN", "0") == "1"
GUEST_USER = os.environ.get("BYOI_GUEST_USER", "guest")
ROOT = Path(__file__).resolve().parents[2]
CODER = ROOT / "apps" / "coder"
GUEST_WEB = ROOT / "apps" / "guest-web"
SEAT_WEB = ROOT / "apps" / "seat-web"


def lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def client_allowed(host: str | None, *, lan_cidr: str = LAN_CIDR) -> bool:
    """True for the seat itself and phones on the same private Wi-Fi."""
    if not host:
        return False
    if host in {"testclient", "localhost", "localhost.localdomain"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    cidr = (lan_cidr or "").strip()
    if cidr:
        try:
            return ip in ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            return False
    return bool(ip.is_private or ip.is_link_local)


def _want_rfcomm() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return TRANSPORT == "rfcomm"


@asynccontextmanager
async def _lifespan(application: FastAPI):
    from apps.tls import paths as tls_paths

    from .control_server import spawn_control_task

    if _want_rfcomm():
        from .rfcomm_tty import start_rfcomm

        start_rfcomm()
    control = spawn_control_task()
    _state["control_tls"] = bool(control) or tls_paths().seat_ready()
    logging.getLogger("uvicorn.error").info(
        "BYOI seat ready · guest PWA :8787 · control mTLS :8788"
    )
    try:
        yield
    finally:
        if control:
            server, task = control
            server.should_exit = True
            await task


_state = {
    "seat_id": SEAT_ID,
    "name": SEAT_NAME,
    "transport": TRANSPORT,
    "tmux": SESSION,
    "claude": "idle",
    "chat": "stream-json",
    "last_unlock": None,
    "control_tls": False,
}

app = FastAPI(title=f"BYOI {SEAT_NAME}", lifespan=_lifespan)
if CODER.is_dir():
    app.mount("/coder/assets", StaticFiles(directory=CODER), name="coder-assets")
    app.mount("/assets", StaticFiles(directory=CODER), name="coder-assets-short")


def _request_host(request: Request) -> str:
    return request.client.host if request.client else ""


def _ssh_hint() -> str:
    host = os.environ.get("BYOI_SEAT_HOST") or lan_ip()
    return f"ssh {GUEST_USER}@{host}"


def _via() -> str:
    if TRANSPORT == "rfcomm":
        return "rfcomm"
    return "wifi"


def _guest_url(otp: str | None = None) -> str:
    q = f"?otp={otp}" if otp else ""
    return f"/guest/{q}"


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    index = SEAT_WEB / "index.html"
    if index.is_file():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse("<p>seat UI missing — try <a href='/guest/'>/guest/</a></p>")


@app.get("/ca.pem")
def ca_pem() -> FileResponse:
    from apps.tls import paths as tls_paths

    ca = tls_paths().ca
    if not ca.is_file():
        raise HTTPException(404, "run scripts/salon-tls.sh")
    return FileResponse(ca, media_type="application/x-pem-file", filename="byoi-ca.pem")


@app.get("/local/status")
def status() -> dict:
    tmux = ensure_session()
    _state["claude"] = "chat"
    out = {
        **_state,
        **tmux,
        **gate.snapshot(),
        "ssh": _ssh_hint(),
        "lan": lan_ip(),
        "wifi": True,
        "otp_gate": True,
        "guest": "/guest/",
        "workspace": str(chat_session.workspace_path or default_workspace()),
        "control_port": int(os.environ.get("BYOI_CONTROL_PORT", "8788")),
    }
    if TRANSPORT == "rfcomm":
        from .rfcomm_tty import rfcomm_status

        out["rfcomm"] = rfcomm_status()
    return out


class UnlockIn(BaseModel):
    otp: str | None = None
    session_id: str | None = None


@app.post("/local/unlock")
def unlock(request: Request, body: UnlockIn | None = None) -> dict:
    host = _request_host(request)
    if not client_allowed(host) and not UNLOCK_OPEN:
        raise HTTPException(403, "join the same Wi-Fi as this seat first")
    presented = body.otp if body else None
    try:
        ticket = gate.unlock(presented, open_gate=UNLOCK_OPEN)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    tmux = ensure_session()
    _state["last_unlock"] = time.time()
    _state["claude"] = "tmux" if tmux.get("tmux") else tmux.get("error")
    result = {
        "ok": True,
        "seat_id": SEAT_ID,
        "via": _via(),
        "ticket": ticket,
        "chat": f"/chat?ticket={ticket}",
        "guest": f"/guest/?ticket={ticket}&view=chat",
        "term": f"/term?ticket={ticket}",
        "tty": f"/tty?ticket={ticket}",
        "ssh": _ssh_hint(),
        "tmux": f"tmux attach -t {SESSION}",
        "tmux_status": tmux,
    }
    if TRANSPORT == "rfcomm":
        from .rfcomm_tty import rfcomm_status

        result["rfcomm"] = rfcomm_status()
    return result


def _accept_guest_socket(ws: WebSocket) -> bool:
    client = ws.client.host if ws.client else ""
    if not client_allowed(client) and not UNLOCK_OPEN:
        return False
    return gate.check_ticket(ws.query_params.get("ticket"))


def _require_ticket(ticket: str | None) -> None:
    if not gate.check_ticket(ticket):
        raise HTTPException(403, "unlock this seat first")


@app.get("/local/handoff")
def guest_handoff(request: Request, ticket: str) -> Response:
    if not client_allowed(_request_host(request)) and not UNLOCK_OPEN:
        raise HTTPException(403, "join the same Wi-Fi as this seat first")
    _require_ticket(ticket)
    from .accounts import read_handoff

    text = chat_session.handoff_text or read_handoff(gate.snapshot().get("session_id"))
    if not text:
        raise HTTPException(404, "no compact handoff yet")
    return Response(text, media_type="text/markdown; charset=utf-8")


class ByoIn(BaseModel):
    ticket: str | None = None
    code: str | None = None


def _guest_session_id() -> str | None:
    return gate.snapshot().get("session_id")


def _byo_guard(request: Request, body: ByoIn | None) -> str | None:
    if not client_allowed(_request_host(request)) and not UNLOCK_OPEN:
        raise HTTPException(403, "join the same Wi-Fi as this seat first")
    _require_ticket(body.ticket if body else None)
    return _guest_session_id()


@app.post("/local/byo/start")
async def byo_start(request: Request, body: ByoIn | None = None) -> dict:
    """Begin a sign-in the guest completes on their own phone."""
    session_id = _byo_guard(request, body)
    from . import guest_auth

    try:
        started = await guest_auth.begin_login(session_id)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    if started.get("done"):
        # A callback finished the flow without a code; adopt it straight away.
        chat_session.reset()
        chat_session.assign_account(guest_auth.guest_account(session_id))
    return {
        **started,
        "seat_id": SEAT_ID,
        "byo": chat_session.byo,
        "warnings": guest_auth.storage_warnings(),
    }


@app.post("/local/byo/code")
async def byo_code(request: Request, body: ByoIn | None = None) -> dict:
    """Hand the guest's authorization code to the waiting sign-in."""
    session_id = _byo_guard(request, body)
    from . import guest_auth

    try:
        done = await guest_auth.submit_code(session_id, (body.code if body else "") or "")
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    # A fresh account means a fresh session: never carry the salon account's
    # conversation into the guest's own.
    chat_session.reset()
    chat_session.assign_account(guest_auth.guest_account(session_id))
    return {**done, "seat_id": SEAT_ID, "byo": chat_session.byo}


@app.post("/local/byo/cancel")
async def byo_cancel(request: Request, body: ByoIn | None = None) -> dict:
    """Drop the guest's account and go back to a salon one."""
    session_id = _byo_guard(request, body)
    from . import guest_auth

    result = await guest_auth.teardown(session_id)
    chat_session.reset()
    account = chat_session.assign_preferred(SEAT_ID)
    return {
        "ok": True,
        "seat_id": SEAT_ID,
        "byo": chat_session.byo,
        "account": account.label if account else None,
        **result,
    }


@app.get("/local/workspace")
def workspace_listing(request: Request, ticket: str, path: str = "") -> dict:
    if not client_allowed(_request_host(request)) and not UNLOCK_OPEN:
        raise HTTPException(403, "join the same Wi-Fi as this seat first")
    _require_ticket(ticket)
    try:
        return list_workspace(path)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.websocket("/chat")
async def chat(ws: WebSocket) -> None:
    if not _accept_guest_socket(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        await chat_session.attach_client(ws)
    except WebSocketDisconnect:
        return


@app.websocket("/term")
async def term(ws: WebSocket) -> None:
    if not _accept_guest_socket(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        await attach_tmux(ws)
    except WebSocketDisconnect:
        return


@app.get("/join")
def join(otp: str | None = None) -> RedirectResponse:
    return RedirectResponse(url=_guest_url(otp))


@app.get("/coder")
def coder(otp: str | None = None) -> RedirectResponse:
    return RedirectResponse(url=_guest_url(otp))


@app.get("/tty", response_class=HTMLResponse)
def tty() -> HTMLResponse:
    page = CODER / "tty.html"
    if page.is_file():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<p>tty missing</p>")


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def house_proxy(path: str, request: Request) -> Response:
    url = urljoin(HOUSE.rstrip("/") + "/", f"api/{path}")
    if request.url.query:
        url = f"{url}?{request.url.query}"
    async with httpx.AsyncClient() as client:
        proxied = await client.request(
            request.method,
            url,
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            content=await request.body(),
            timeout=20.0,
        )
    return Response(
        content=proxied.content,
        status_code=proxied.status_code,
        media_type=proxied.headers.get("content-type"),
    )


if SEAT_WEB.is_dir():
    app.mount("/seat/assets", StaticFiles(directory=SEAT_WEB), name="seat-web")
if GUEST_WEB.is_dir():
    app.mount("/guest", StaticFiles(directory=GUEST_WEB, html=True), name="guest-pwa")
