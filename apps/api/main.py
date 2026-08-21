"""House API + static host/coder apps."""

from __future__ import annotations

import os
import ssl
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from apps.host_token import token_is_weak, token_matches

from . import seat_sync
from .printing import print_slip
from .seat_sync import SeatSyncError
from .slips import WIFI_SSID, compose_checkin_slip, public_base, public_host, seat_join_url
from .store import Store

ROOT = Path(__file__).resolve().parents[2]
SALON_NAME = os.environ.get("BYOI_SALON_NAME", "BYOI salon · cafe")
HOST_WEB = ROOT / "apps" / "host-web"
CODER_WEB = ROOT / "apps" / "coder"


class BoardIn(BaseModel):
    title: str
    brief: str
    wellness_minutes: int = Field(90, ge=15, le=240)
    break_after: int = Field(50, ge=10, le=180)


class CheckInIn(BaseModel):
    seat_id: str
    coder_name: str


class ClaimIn(BaseModel):
    board_id: str


def create_app(data_dir: Path | None = None) -> FastAPI:
    data = Path(data_dir or os.environ.get("BYOI_DATA", ROOT / "data"))
    store = Store(data / "salon.db")
    app = FastAPI(title="BYOI salon", version="0.1.0")
    if HOST_WEB.is_dir():
        app.mount("/host/assets", StaticFiles(directory=HOST_WEB), name="host-assets")
    if CODER_WEB.is_dir():
        app.mount("/coder/assets", StaticFiles(directory=CODER_WEB), name="coder-assets")

    def require_host(authorization: str | None) -> None:
        if token_is_weak():
            raise HTTPException(503, "set a non-default BYOI_HOST_TOKEN (scripts/salon-tls.sh)")
        if not token_matches(authorization):
            raise HTTPException(401, "host token required")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "salon": SALON_NAME}

    @app.get("/api/board")
    def get_board() -> dict:
        return {"items": store.board()}

    @app.post("/api/board")
    def post_board(body: BoardIn, authorization: str | None = Header(default=None)) -> dict:
        require_host(authorization)
        return store.add_board(body.title, body.brief, body.wellness_minutes, body.break_after)

    @app.get("/api/seats")
    def get_seats() -> dict:
        return {"seats": store.seats()}

    @app.post("/api/sessions/check-in")
    def check_in(body: CheckInIn, authorization: str | None = Header(default=None)) -> dict:
        require_host(authorization)
        try:
            sess = store.check_in(body.seat_id, body.coder_name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        seat = store.seat(body.seat_id)
        assert seat
        try:
            seat_sync.admit_session(seat, sess)
        except SeatSyncError as exc:
            store.free_seat(body.seat_id)
            raise HTTPException(
                502,
                "seat did not accept the OTP — is the seat agent up on this PC?",
            ) from exc
        join = seat_join_url(seat, sess["unlock_otp"])
        image = compose_checkin_slip(
            salon=SALON_NAME,
            seat_name=seat["name"],
            coder_name=sess["coder_name"],
            board_title=None,
            otp=sess["unlock_otp"],
            wellness_minutes=90,
            break_after=50,
            wifi_ssid=WIFI_SSID,
            join=join,
        )
        printed = print_slip(image, data)
        store.log_print("check-in", {"session_id": sess["id"], "otp": sess["unlock_otp"]}, printed.get("png"))
        host = public_host(seat["agent_url"])
        return {
            "session": sess,
            "join": join,
            "ssh": f"ssh guest@{host}",
            "tmux": "tmux attach -t claude-guest",
            "print": printed,
            "otp": sess["unlock_otp"],
            "seat_admitted": True,
        }

    @app.post("/api/sessions/{session_id}/claim")
    def claim(session_id: str, body: ClaimIn) -> dict:
        try:
            sess = store.claim(session_id, body.board_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"session": sess, "item": store.board_item(body.board_id)}

    def _revoke_seat(seat: dict | None) -> None:
        try:
            seat_sync.revoke_session(seat)
        except SeatSyncError as exc:
            raise HTTPException(502, "seat did not drop the OTP") from exc

    @app.post("/api/seats/free-all")
    def free_all(authorization: str | None = Header(default=None)) -> dict:
        require_host(authorization)
        seen: set[str] = set()
        for seat in store.seats():
            url = seat.get("agent_url") or ""
            if url in seen:
                continue
            seen.add(url)
            _revoke_seat(seat)
        return {"seats": store.free_all()}

    @app.post("/api/seats/{seat_id}/free")
    def free_seat(seat_id: str, authorization: str | None = Header(default=None)) -> dict:
        require_host(authorization)
        seat = store.seat(seat_id)
        if not seat:
            raise HTTPException(404, "unknown seat")
        _revoke_seat(seat)
        return {"seat": store.free_seat(seat_id)}

    @app.post("/api/sessions/{session_id}/complete")
    def complete(session_id: str) -> dict:
        sess = store.session(session_id)
        if not sess:
            raise HTTPException(404, "unknown session")
        _revoke_seat(store.seat(sess["seat_id"]))
        return {"session": store.complete(session_id)}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        sess = store.session(session_id)
        if not sess:
            raise HTTPException(404, "unknown session")
        item = store.board_item(sess["board_id"]) if sess.get("board_id") else None
        return {"session": sess, "item": item, "seat": store.seat(sess["seat_id"])}

    @app.get("/api/join")
    def join(otp: str) -> dict:
        sess = store.session_by_otp(otp)
        if not sess:
            raise HTTPException(404, "unknown slip")
        if sess["status"] == "done":
            raise HTTPException(410, "session finished")
        seat = store.seat(sess["seat_id"])
        agent = public_base(seat["agent_url"]) if seat else None
        return {
            "session": sess,
            "seat": seat,
            "board": store.board(),
            "seat_agent": agent,
            "wifi_ssid": WIFI_SSID,
        }

    @app.get("/join")
    def join_page(otp: str | None = None) -> RedirectResponse:
        q = f"?otp={otp}" if otp else ""
        return RedirectResponse(url=f"/coder{q}")

    @app.post("/local/unlock")
    async def unlock_via_seat(request: Request):
        """Forward unlock to the seat agent so the PWA can stay same-origin on :8080."""
        seat_url = os.environ.get("BYOI_SEAT_URL", "https://127.0.0.1:8787")
        verify: bool | ssl.SSLContext = True
        try:
            from apps.tls import guest_verify_context, paths as tls_paths

            if tls_paths().ca.is_file():
                verify = guest_verify_context()
        except (OSError, ssl.SSLError):
            verify = True
        async with httpx.AsyncClient(verify=verify) as client:
            proxied = await client.post(
                f"{seat_url.rstrip('/')}/local/unlock",
                content=await request.body(),
                headers={"content-type": request.headers.get("content-type", "application/json")},
                timeout=20.0,
            )
        return Response(
            content=proxied.content,
            status_code=proxied.status_code,
            media_type=proxied.headers.get("content-type"),
        )

    @app.get("/", response_class=HTMLResponse)
    def root() -> HTMLResponse:
        index = HOST_WEB / "index.html"
        if index.is_file():
            return HTMLResponse(index.read_text(encoding="utf-8"))
        return HTMLResponse("<p>host web missing</p>")

    @app.get("/coder", response_class=HTMLResponse)
    def coder_index() -> HTMLResponse:
        index = CODER_WEB / "index.html"
        if index.is_file():
            return HTMLResponse(index.read_text(encoding="utf-8"))
        return HTMLResponse("<p>coder pwa missing</p>")

    @app.get("/ca.pem")
    def ca_pem() -> FileResponse:
        from apps.tls import paths as tls_paths

        ca = tls_paths().ca
        if not ca.is_file():
            raise HTTPException(404, "run scripts/salon-tls.sh")
        return FileResponse(ca, media_type="application/x-pem-file", filename="byoi-ca.pem")

    @app.get("/last-slip.png")
    def last_slip() -> FileResponse:
        path = data / "last-slip.png"
        if not path.is_file():
            raise HTTPException(404, "no slip yet")
        return FileResponse(path, media_type="image/png")

    return app


app = create_app()
