"""Seat agent: cafe Wi-Fi TTY. Phone and this PC share a LAN."""

from __future__ import annotations

import ipaddress
import os
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .pty_ws import attach_tmux
from .tmux_claude import SESSION, ensure_session

TRANSPORT = os.environ.get("BYOI_TRANSPORT", "wifi")
LAN_CIDR = os.environ.get("BYOI_LAN_CIDR", "")
SEAT_ID = os.environ.get("BYOI_SEAT_ID", "seat-1")
SEAT_NAME = os.environ.get("BYOI_SEAT_NAME", "Seat 1")
HOUSE = os.environ.get("BYOI_HOUSE_URL", "http://127.0.0.1:8080")
UNLOCK_OPEN = os.environ.get("BYOI_UNLOCK_OPEN", "1") == "1"
GUEST_USER = os.environ.get("BYOI_GUEST_USER", "guest")
ROOT = Path(__file__).resolve().parents[2]
CODER = ROOT / "apps" / "coder"


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
    if _want_rfcomm():
        from .rfcomm_tty import start_rfcomm

        start_rfcomm()
    yield


app = FastAPI(title=f"BYOI {SEAT_NAME}", lifespan=_lifespan)
if CODER.is_dir():
    app.mount("/coder/assets", StaticFiles(directory=CODER), name="coder-assets")
    app.mount("/assets", StaticFiles(directory=CODER), name="coder-assets-short")

_state = {
    "seat_id": SEAT_ID,
    "name": SEAT_NAME,
    "transport": TRANSPORT,
    "tmux": SESSION,
    "claude": "idle",
    "last_unlock": None,
}


def _request_host(request: Request) -> str:
    return request.client.host if request.client else ""


def _ssh_hint() -> str:
    host = os.environ.get("BYOI_SEAT_HOST") or lan_ip()
    return f"ssh {GUEST_USER}@{host}"


def _via() -> str:
    if TRANSPORT == "rfcomm":
        return "rfcomm"
    return "wifi"


@app.get("/")
def root(otp: str | None = None) -> RedirectResponse:
    q = f"?otp={otp}" if otp else ""
    return RedirectResponse(url=f"/coder{q}")


@app.get("/local/status")
def status() -> dict:
    tmux = ensure_session()
    _state["claude"] = "tmux" if tmux.get("tmux") else tmux.get("error")
    out = {
        **_state,
        **tmux,
        "ssh": _ssh_hint(),
        "lan": lan_ip(),
        "wifi": True,
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
    tmux = ensure_session()
    _state["last_unlock"] = time.time()
    _state["claude"] = "tmux" if tmux.get("tmux") else tmux.get("error")
    result = {
        "ok": True,
        "seat_id": SEAT_ID,
        "via": _via(),
        "term": "/term",
        "ssh": _ssh_hint(),
        "tmux": f"tmux attach -t {SESSION}",
        "tmux_status": tmux,
    }
    if TRANSPORT == "rfcomm":
        from .rfcomm_tty import rfcomm_status

        result["rfcomm"] = rfcomm_status()
    return result


@app.websocket("/term")
async def term(ws: WebSocket) -> None:
    client = ws.client.host if ws.client else ""
    if not client_allowed(client) and not UNLOCK_OPEN:
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        await attach_tmux(ws)
    except WebSocketDisconnect:
        return


@app.get("/join")
def join(otp: str | None = None) -> RedirectResponse:
    q = f"?otp={otp}" if otp else ""
    return RedirectResponse(url=f"/coder{q}")


@app.get("/coder", response_class=HTMLResponse)
def coder(otp: str | None = None) -> HTMLResponse:
    index = CODER / "index.html"
    html = index.read_text(encoding="utf-8") if index.is_file() else "<p>coder pwa missing</p>"
    return HTMLResponse(html)


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
