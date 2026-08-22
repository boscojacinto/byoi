"""House API + static host/guest apps."""

from __future__ import annotations

import os
import ssl
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from apps.host_token import token_is_weak, token_matches

from . import projects as project_ops
from . import seat_sync
from . import testgen
from .printing import print_slip
from .seat_sync import SeatSyncError
from .testgen import TestgenError
from .slips import WIFI_SSID, compose_checkin_slip, public_base, public_host, save_join_qr, seat_join_url
from .store import Store

ROOT = Path(__file__).resolve().parents[2]
SALON_NAME = os.environ.get("BYOI_SALON_NAME", "BYOI salon · cafe")
HOST_WEB = ROOT / "apps" / "host-web"
CODER_WEB = ROOT / "apps" / "coder"
GUEST_WEB = ROOT / "apps" / "guest-web"


class BoardIn(BaseModel):
    title: str
    brief: str
    wellness_minutes: int = Field(90, ge=15, le=240)
    break_after: int = Field(50, ge=10, le=180)
    project_id: str | None = None
    spec: str = ""


class ProjectIn(BaseModel):
    kind: str = Field("local", pattern="^(local|clone|github)$")
    name: str | None = None
    path: str | None = None
    url: str | None = None
    description: str = ""
    private: bool = True


class BoardProjectIn(BaseModel):
    project_id: str | None = None


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

    def is_desk(request: Request) -> bool:
        """True when the operator is on this PC (same-machine host + seat)."""
        host = request.client.host if request.client else ""
        return host in {"127.0.0.1", "::1", "localhost"}

    def require_host(request: Request, authorization: str | None) -> None:
        if token_is_weak():
            raise HTTPException(503, "set a non-default BYOI_HOST_TOKEN (scripts/salon-tls.sh)")
        if is_desk(request):
            return
        if not token_matches(authorization):
            raise HTTPException(401, "host token required — open http://127.0.0.1:8080/ on this PC")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "salon": SALON_NAME}

    @app.get("/api/board")
    def get_board() -> dict:
        return {"items": store.board()}

    @app.post("/api/board")
    def post_board(request: Request, body: BoardIn, authorization: str | None = Header(default=None)) -> dict:
        require_host(request, authorization)
        try:
            return store.add_board(
                body.title,
                body.brief,
                body.wellness_minutes,
                body.break_after,
                project_id=body.project_id,
                spec=body.spec,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/projects")
    def get_projects() -> dict:
        return {"projects": store.projects()}

    @app.post("/api/projects")
    def post_project(request: Request, body: ProjectIn, authorization: str | None = Header(default=None)) -> dict:
        require_host(request, authorization)
        try:
            if body.kind == "github":
                made = project_ops.create_github(
                    name=body.name or "",
                    description=body.description,
                    private=body.private,
                )
            elif body.kind == "clone":
                made = project_ops.clone_url(body.url or "", body.name)
            else:
                made = project_ops.use_local(body.path or "", body.name)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        return store.add_project(name=made["name"] or "project", local_path=made["local_path"] or "", github=made.get("github"))

    @app.post("/api/board/{board_id}/project")
    def assign_project(
        request: Request,
        board_id: str,
        body: BoardProjectIn,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_host(request, authorization)
        try:
            return store.set_board_project(board_id, body.project_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/seats")
    def get_seats() -> dict:
        return {"seats": store.seats()}

    @app.get("/api/claude-accounts")
    def claude_accounts(request: Request, authorization: str | None = Header(default=None)) -> dict:
        require_host(request, authorization)
        try:
            return seat_sync.list_accounts()
        except SeatSyncError:
            return {"accounts": [], "account": None, "quota": None, "handoff": False}

    @app.get("/api/sessions/{session_id}/handoff")
    def session_handoff(session_id: str) -> Response:
        from apps.seat.accounts import read_handoff

        sess = store.session(session_id)
        if not sess:
            raise HTTPException(404, "unknown session")
        text = read_handoff(session_id)
        if not text:
            raise HTTPException(404, "no compact handoff yet")
        return Response(text, media_type="text/markdown; charset=utf-8")

    @app.post("/api/sessions/check-in")
    def check_in(request: Request, body: CheckInIn, authorization: str | None = Header(default=None)) -> dict:
        require_host(request, authorization)
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
        qr_path = save_join_qr(join, data)
        store.log_print("check-in", {"session_id": sess["id"], "otp": sess["unlock_otp"]}, printed.get("png"))
        host = public_host(seat["agent_url"])
        return {
            "session": sess,
            "join": join,
            "ssh": f"ssh guest@{host}",
            "guest": "/guest/",
            "tmux": "tmux attach -t claude-guest",
            "print": printed,
            "qr": str(qr_path),
            "otp": sess["unlock_otp"],
            "seat_admitted": True,
        }

    @app.get("/api/live")
    def api_live(request: Request, seat_id: str | None = None, authorization: str | None = Header(default=None)) -> dict:
        """Mirror of each occupied seat's guest chat (Claude Code stream)."""
        require_host(request, authorization)
        seats = store.seats()
        if seat_id:
            seats = [s for s in seats if s["id"] == seat_id]
        sessions = []
        for seat in seats:
            if not seat.get("session"):
                continue
            try:
                live = seat_sync.live_snapshot(seat)
            except SeatSyncError as exc:
                live = {"error": str(exc), "history": [], "busy": False}
            sessions.append({"seat": seat, "live": live})
        return {"sessions": sessions}

    @app.post("/api/sessions/{session_id}/claim")
    def claim(session_id: str, body: ClaimIn) -> dict:
        try:
            sess = store.claim(session_id, body.board_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        item = store.board_item(body.board_id)
        project = (item or {}).get("project")
        path = (project or {}).get("local_path") if project else None
        if path:
            seat = store.seat(sess["seat_id"])
            try:
                seat_sync.set_workspace(seat, path)
            except SeatSyncError as exc:
                raise HTTPException(502, "seat did not switch to this project's folder") from exc
        return {"session": sess, "item": item, "project": project}

    def _revoke_seat(seat: dict | None) -> None:
        try:
            seat_sync.revoke_session(seat)
        except SeatSyncError as exc:
            raise HTTPException(502, "seat did not drop the OTP") from exc

    @app.post("/api/seats/free-all")
    def free_all(request: Request, authorization: str | None = Header(default=None)) -> dict:
        require_host(request, authorization)
        seen: set[str] = set()
        for seat in store.seats():
            url = seat.get("agent_url") or ""
            if url in seen:
                continue
            seen.add(url)
            _revoke_seat(seat)
        return {"seats": store.free_all()}

    @app.post("/api/seats/{seat_id}/free")
    def free_seat(request: Request, seat_id: str, authorization: str | None = Header(default=None)) -> dict:
        require_host(request, authorization)
        seat = store.seat(seat_id)
        if not seat:
            raise HTTPException(404, "unknown seat")
        _revoke_seat(seat)
        return {"seat": store.free_seat(seat_id)}

    def _host_test_job(
        session_id: str, seat: dict | None, *, spec: str, title: str, cwd: str | None
    ) -> dict:
        """Seat pins the tree to a git ref; the host generates, runs, and grades."""
        local_ok = bool(cwd) and Path(cwd).is_dir()
        info = seat_sync.submit_solution(
            seat, session_id=session_id, cwd=cwd, push=not local_ok
        )
        ref = info.get("ref")
        if not ref:
            raise TestgenError("the seat did not pin a submission ref")
        # One PC: fetch straight off the seat's repo. Two PCs: the seat pushed it.
        source = info.get("toplevel") if local_ok else info.get("remote")
        if not source:
            raise TestgenError("no reachable source for the submission")
        return testgen.run(
            spec=spec, title=title, source=str(source), ref=str(ref), session_id=session_id
        )

    def _verify_job(session_id: str, seat: dict | None, item: dict | None) -> None:
        spec = (item or {}).get("spec") or ""
        if not str(spec).strip():
            store.set_test_report(
                session_id,
                {"summary": "No spec on this brief.", "passed": 0, "failed": 0, "cases": []},
            )
            return
        cwd = ((item or {}).get("project") or {}).get("local_path")
        title = (item or {}).get("title") or ""
        fallback_reason: str | None = None
        try:
            store.set_test_report(
                session_id,
                _host_test_job(session_id, seat, spec=spec, title=title, cwd=cwd),
            )
            return
        except (TestgenError, SeatSyncError) as exc:
            fallback_reason = str(exc)
        except Exception as exc:  # never leave the phone spinning on "running"
            fallback_reason = f"host testing failed: {exc}"

        try:
            report = seat_sync.verify_solution(seat, spec=spec, title=title, cwd=cwd)
        except Exception as exc:
            report = {
                "summary": str(exc),
                "passed": 0,
                "failed": 1,
                "cases": [{"name": "seat verifier", "pass": False, "detail": str(exc)}],
            }
        if fallback_reason:
            note = f"Graded on the seat ({fallback_reason})."
            report["summary"] = f"{note} {report.get('summary') or ''}".strip()
        store.set_test_report(session_id, report)

    @app.post("/api/sessions/{session_id}/complete")
    def complete(session_id: str, background_tasks: BackgroundTasks) -> dict:
        sess = store.session(session_id)
        if not sess:
            raise HTTPException(404, "unknown session")
        item = store.board_item(sess["board_id"]) if sess.get("board_id") else None
        seat = store.seat(sess["seat_id"])
        _revoke_seat(seat)
        done = store.complete(session_id)
        testing = bool((item or {}).get("spec"))
        if testing:
            store.set_test_running(session_id)
            background_tasks.add_task(_verify_job, session_id, seat, item)
            done = store.session(session_id)
        return {"session": done, "testing": testing, "item": item}

    @app.get("/api/sessions/{session_id}/tests")
    def session_tests(session_id: str) -> dict:
        sess = store.session(session_id)
        if not sess:
            raise HTTPException(404, "unknown session")
        return {
            "session_id": session_id,
            "test_status": sess.get("test_status"),
            "test_report": sess.get("test_report"),
        }

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
        return RedirectResponse(url=f"/guest/{q}")

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

    @app.get("/local/handoff")
    async def handoff_via_seat(request: Request, ticket: str) -> Response:
        """Forward compact handoff so the PWA can stay same-origin on :8080."""
        seat_url = os.environ.get("BYOI_SEAT_URL", "https://127.0.0.1:8787")
        verify: bool | ssl.SSLContext = True
        try:
            from apps.tls import guest_verify_context, paths as tls_paths

            if tls_paths().ca.is_file():
                verify = guest_verify_context()
        except (OSError, ssl.SSLError):
            verify = True
        async with httpx.AsyncClient(verify=verify) as client:
            proxied = await client.get(
                f"{seat_url.rstrip('/')}/local/handoff",
                params={"ticket": ticket},
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

    @app.get("/coder")
    def coder_index(otp: str | None = None) -> RedirectResponse:
        q = f"?otp={otp}" if otp else ""
        return RedirectResponse(url=f"/guest/{q}")

    @app.get("/guest")
    def guest_index(otp: str | None = None) -> RedirectResponse:
        q = f"?otp={otp}" if otp else ""
        return RedirectResponse(url=f"/guest/{q}")

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

    @app.get("/last-qr.png")
    def last_qr() -> FileResponse:
        path = data / "last-qr.png"
        if not path.is_file():
            raise HTTPException(404, "no QR yet")
        return FileResponse(path, media_type="image/png")

    if GUEST_WEB.is_dir():
        app.mount("/guest", StaticFiles(directory=GUEST_WEB, html=True), name="guest-pwa")

    return app


app = create_app()
