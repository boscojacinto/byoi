"""House API + static host/guest apps."""

from __future__ import annotations

import hmac
import json
import logging
import os
import ssl
import time
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from apps.host_token import token_is_weak, token_matches
from apps.secrets import read_secret

from . import caddy
from . import deploy as deploy_ops
from . import github_app
from . import github_issues
from . import guest_report
from . import infra as desk_infra
from . import media as media_ops
from . import object_store
from . import operator
from . import seats
from . import projects as project_ops
from . import seat_sync
from . import testgen
from . import printing
from .printing import print_slip
from .deploy import DeployError
from .seat_sync import SeatSyncError
from .testgen import TestgenError
from .slips import (
    WIFI_SSID,
    compose_checkin_slip,
    join_base,
    public_base,
    public_host,
    save_join_qr,
    seat_join_url,
)
from .store import ProjectBusy, Store

log = logging.getLogger("uvicorn.error")

ROOT = Path(__file__).resolve().parents[2]
SALON_NAME = os.environ.get("BYOI_SALON_NAME", "BYOI salon · cafe")


def _date_str(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d")


# How long after the relay last polled we still call the printer online.
RELAY_OFFLINE_AFTER = 90.0


def on_demand_seats() -> bool:
    """Whether the desk raises a seat container per visit.

    Off by default so the repo still runs on one salon PC with a seat agent
    already up, which is what scripts/run-seat.sh gives you.
    """
    return os.environ.get("BYOI_SEATS", "static").strip().lower() == "ondemand"
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
    kind: str = Field("local", pattern="^(local|clone|github|template)$")
    name: str | None = None
    path: str | None = None
    url: str | None = None
    template: str | None = None
    description: str = ""
    private: bool = True
    push: bool = False


class DeployIn(BaseModel):
    production: bool = False


class BoardProjectIn(BaseModel):
    project_id: str | None = None


class BoardSpecIn(BaseModel):
    spec: str = ""


class LoginIn(BaseModel):
    password: str


class PrintDoneIn(BaseModel):
    ok: bool = True
    error: str | None = None


class CheckInIn(BaseModel):
    seat_id: str
    coder_name: str


class ClaimIn(BaseModel):
    board_id: str


def create_app(data_dir: Path | None = None) -> FastAPI:
    data = Path(data_dir or os.environ.get("BYOI_DATA", ROOT / "data"))
    store = Store(data / "salon.db")
    _relay: dict[str, float] = {}
    _issue_sync_at: dict[str, float] = {}
    ISSUE_SYNC_TTL = 60.0

    def sync_project_issues(project_id: str, *, force: bool = False) -> dict | None:
        """Merge a github project's open issues onto its board rows.

        None means "not a GitHub project" (nothing to do) or "synced too
        recently, skipped" — both are fine to ignore. Raises on a real fetch
        failure so a manual sync can surface it to the host.
        """
        project = store.project(project_id)
        if not project:
            raise KeyError("unknown project")
        slug = project_ops.github_repo_slug(project.get("github"))
        if not slug:
            return None
        if not force and time.time() - _issue_sync_at.get(project_id, 0.0) < ISSUE_SYNC_TTL:
            return None
        issues = github_issues.fetch_open_issues(slug, token=_app_token_for(project, slug))
        result = store.sync_board_issues(project_id, issues)
        _issue_sync_at[project_id] = time.time()
        return result

    def _app_token_for(project: dict, slug: str) -> str | None:
        """A GitHub App installation token for this project's repo, if the
        desk has an App configured and it's installed there. None falls back
        to whatever `gh auth login` the desk process already has — so a
        project that hasn't been linked to the App yet still works exactly
        as it did before the App existed.
        """
        if not github_app.configured():
            return None
        installation_id = project.get("github_installation_id")
        if installation_id is None:
            try:
                installation_id = github_app.installation_id_for(slug)
            except github_app.GithubAppError as exc:
                log.warning("BYOI: GitHub App lookup failed for %s: %s", slug, exc)
                return None
            if installation_id is None:
                return None
            store.set_project_installation(project["id"], installation_id)
        try:
            return github_app.installation_token(installation_id)
        except github_app.GithubAppError as exc:
            log.warning("BYOI: could not mint an installation token for %s: %s", slug, exc)
            return None

    def sync_all_github_projects() -> None:
        """Best-effort refresh before the board is read — a `gh` failure (not
        installed, not authenticated, repo gone private) must never break
        Solutions for everyone else, so it's swallowed here and only surfaced
        through the explicit sync endpoint."""
        for project in store.projects():
            try:
                sync_project_issues(project["id"])
            except (KeyError, github_issues.GithubIssuesError) as exc:
                log.warning("BYOI: could not sync issues for project %s: %s", project["id"], exc)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if on_demand_seats():
            try:
                gone = seats.reconcile(store)["removed"]
                if gone:
                    log.info("BYOI: cleared %d seat(s) with no live session", len(gone))
            except seats.SeatError as exc:
                log.warning("BYOI: could not reconcile seats at startup: %s", exc)
        yield

    app = FastAPI(title="BYOI salon", version="0.1.0", lifespan=lifespan)
    if HOST_WEB.is_dir():
        app.mount("/host/assets", StaticFiles(directory=HOST_WEB), name="host-assets")
    if CODER_WEB.is_dir():
        app.mount("/coder/assets", StaticFiles(directory=CODER_WEB), name="coder-assets")

    def require_operator(request: Request, authorization: str | None) -> None:
        """A signed desk cookie, or the host token for machine callers.

        There is deliberately no same-machine shortcut. The desk sits behind a
        reverse proxy now, so every request appears to come from the proxy — a
        loopback test there does not identify the operator, it admits everyone.
        """
        if token_is_weak():
            raise HTTPException(503, "set a non-default BYOI_HOST_TOKEN (scripts/salon-tls.sh)")
        if operator.read_cookie(request.cookies.get(operator.COOKIE_NAME)):
            return
        if token_matches(authorization):
            return
        raise HTTPException(401, "sign in to the desk")

    @app.middleware("http")
    async def slide_operator_cookie(request: Request, call_next):
        """Keep an active operator signed in without extending the hard deadline."""
        response = await call_next(request)
        claims = operator.read_cookie(request.cookies.get(operator.COOKIE_NAME))
        if claims and claims.stale():
            response.set_cookie(value=operator.refresh_cookie(claims), **operator.cookie_kwargs())
        return response

    def client_ip(request: Request) -> str:
        """The guest's address. Correct only because uvicorn runs with
        --proxy-headers and --forwarded-allow-ips set to the proxy."""
        return request.client.host if request.client else "unknown"

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "salon": SALON_NAME}

    @app.get("/api/session")
    def get_session(request: Request) -> dict:
        claims = operator.read_cookie(request.cookies.get(operator.COOKIE_NAME))
        return {
            "signed_in": bool(claims),
            "password_set": operator.password_is_set(),
            "expires_at": claims.expires_at if claims else None,
            "salon": SALON_NAME,
        }

    @app.post("/api/login")
    def post_login(request: Request, body: LoginIn) -> Response:
        if not operator.password_is_set():
            raise HTTPException(503, "no operator password set — run scripts/salon-secrets.sh operator")
        who = client_ip(request)
        wait = operator.throttle.locked_for(who)
        if wait:
            raise HTTPException(429, f"too many attempts — try again in {int(wait) // 60 + 1} min")
        if not operator.verify_password(body.password):
            operator.throttle.record_failure(who)
            raise HTTPException(401, "wrong password")
        operator.throttle.clear(who)
        response = JSONResponse({"ok": True, "salon": SALON_NAME})
        response.set_cookie(value=operator.issue_cookie(), **operator.cookie_kwargs())
        return response

    @app.post("/api/logout")
    def post_logout() -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(operator.COOKIE_NAME, path="/")
        return response

    @app.get("/api/board")
    def get_board() -> dict:
        sync_all_github_projects()
        return {"items": store.board()}

    @app.post("/api/board")
    def post_board(request: Request, body: BoardIn, authorization: str | None = Header(default=None)) -> dict:
        require_operator(request, authorization)
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
        require_operator(request, authorization)
        try:
            if body.kind == "template":
                made = project_ops.from_template(
                    template=body.template or "",
                    name=body.name,
                    private=body.private,
                    github=body.push,
                )
            elif body.kind == "github":
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
        local_path = made["local_path"] or ""
        framework = made.get("framework") or (project_ops.detect(local_path).get("framework") if local_path else None)
        created = store.add_project(
            name=made["name"] or "project",
            local_path=local_path,
            github=made.get("github"),
            framework=framework,
            template=made.get("template"),
        )
        if project_ops.is_github_project(created):
            try:
                sync_project_issues(created["id"], force=True)
            except github_issues.GithubIssuesError as exc:
                log.warning("BYOI: could not sync issues for new project %s: %s", created["id"], exc)
        return created

    # --- media a brief needs as an input ---------------------------------
    #
    # Operator-only, all three. Guests never write to the bucket: it is a
    # billed resource that any key can read in full, so it stays off the
    # untrusted path. The guest's chat photos are a separate, transient thing.

    @app.get("/api/projects/{project_id}/media")
    def get_project_media(project_id: str) -> dict:
        return {
            "media": store.media_for_project(project_id),
            "configured": object_store.configured(),
        }

    @app.post("/api/projects/{project_id}/media")
    async def post_project_media(
        request: Request,
        project_id: str,
        filename: str = "",
        role: str = "",
        board_id: str = "",
        authorization: str | None = Header(default=None),
    ) -> dict:
        """The file's bytes are the request body; its name and description ride
        in the query string. Raw rather than multipart so the desk image does
        not need python-multipart for a route only the desk UI ever calls."""
        require_operator(request, authorization)
        data = await request.body()
        try:
            return media_ops.add(
                store,
                project_id=project_id,
                filename=filename or "file",
                data=data,
                content_type=request.headers.get("content-type", ""),
                role=role,
                board_id=board_id or None,
            )
        except KeyError as exc:
            raise HTTPException(404, "unknown project") from exc
        except object_store.ObjectStoreError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.delete("/api/projects/{project_id}/media/{media_id}")
    def delete_project_media(
        request: Request,
        project_id: str,
        media_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_operator(request, authorization)
        gone = media_ops.remove(store, media_id)
        if not gone:
            raise HTTPException(404, "unknown media")
        return {"ok": True, "removed": gone}

    @app.post("/api/board/{board_id}/media/{media_id}")
    def attach_media_to_board(
        request: Request,
        board_id: str,
        media_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        """Narrow one file to a single brief, or (with board_id 'all') widen it
        back to every brief on the project."""
        require_operator(request, authorization)
        try:
            return store.set_media_board(media_id, None if board_id == "all" else board_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown media") from exc

    @app.post("/api/projects/{project_id}/sync-issues")
    def sync_issues_route(
        request: Request, project_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        require_operator(request, authorization)
        try:
            result = sync_project_issues(project_id, force=True)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except github_issues.GithubIssuesError as exc:
            raise HTTPException(502, str(exc)) from exc
        if result is None:
            raise HTTPException(400, "this project's origin is not a GitHub repo")
        return {"project_id": project_id, **result}

    @app.get("/api/github/app")
    def github_app_status() -> dict:
        return {"configured": github_app.configured(), "slug": github_app.slug()}

    @app.get("/api/github/app/new")
    def github_app_new(
        request: Request, authorization: str | None = Header(default=None)
    ) -> HTMLResponse:
        """A one-click "Create GitHub App" page: an auto-submitting form to
        GitHub's manifest-flow endpoint, pre-filled so the operator's only
        action is clicking Create — GitHub mints the id, slug, and private
        key itself and hands them back to /api/github/app/created."""
        require_operator(request, authorization)
        base = join_base()
        domain = urlparse(base).hostname or "byoi-salon"
        # GitHub caps App names at 34 characters — a plain domain like
        # "salon.aipilots.online" already leaves little room to spare.
        app_name = f"BYOI sync ({domain})"[:34]
        manifest = {
            "name": app_name,
            "url": base,
            "hook_attributes": {"url": f"{base}/api/github/app/hook", "active": False},
            "redirect_url": f"{base}/api/github/app/created",
            "setup_url": f"{base}/api/github/app/setup",
            "setup_on_update": True,
            "public": False,
            "default_permissions": {"issues": "read", "metadata": "read"},
            "default_events": [],
        }
        manifest_value = html_escape(json.dumps(manifest), quote=True)
        html = f"""<!doctype html><html><body>
<form id="ghAppManifest" action="https://github.com/settings/apps/new" method="post">
<input type="hidden" name="manifest" value="{manifest_value}">
</form>
<script>document.getElementById('ghAppManifest').submit()</script>
</body></html>"""
        return HTMLResponse(html)

    @app.get("/api/github/app/created")
    def github_app_created(
        request: Request, code: str, authorization: str | None = Header(default=None)
    ) -> Response:
        """Where GitHub's manifest flow redirects after the operator clicks
        Create — trades the one-time code for real credentials and stores
        them, then sends the operator back to the desk."""
        require_operator(request, authorization)
        try:
            data = github_app.convert_manifest_code(code)
        except github_app.GithubAppError as exc:
            raise HTTPException(502, str(exc)) from exc
        github_app.store_credentials(data)
        return RedirectResponse(url="/", status_code=303)

    @app.get("/api/github/app/setup")
    def github_app_setup(
        request: Request,
        installation_id: int,
        state: str | None = None,
        setup_action: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> Response:
        """Where GitHub sends the operator back after installing (or
        updating) the App on a repo. `state` carries the project id we asked
        for in the install link, so this is what actually links the two."""
        require_operator(request, authorization)
        if state:
            try:
                store.set_project_installation(state, installation_id)
            except KeyError:
                pass
        return RedirectResponse(url="/", status_code=303)

    @app.get("/api/projects/{project_id}/github-app-install-url")
    def github_app_install_url(
        request: Request, project_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        require_operator(request, authorization)
        if not store.project(project_id):
            raise HTTPException(404, "unknown project")
        app_slug = github_app.slug()
        if not app_slug:
            raise HTTPException(400, "no GitHub App configured yet")
        return {"url": f"https://github.com/apps/{app_slug}/installations/new?state={project_id}"}

    @app.get("/api/templates")
    def get_templates() -> dict:
        return {"templates": project_ops.templates()}

    @app.post("/api/projects/{project_id}/fetch")
    def fetch_project(
        request: Request, project_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        """Clone the folder now, so the first guest of the day does not wait on git."""
        require_operator(request, authorization)
        project = store.project(project_id)
        if not project:
            raise HTTPException(404, "unknown project")
        try:
            local_path = project_ops.ensure_local(project)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"project": project, "local_path": local_path}

    @app.post("/api/board/{board_id}/project")
    def assign_project(
        request: Request,
        board_id: str,
        body: BoardProjectIn,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_operator(request, authorization)
        try:
            return store.set_board_project(board_id, body.project_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/board/{board_id}/spec")
    def assign_spec(
        request: Request,
        board_id: str,
        body: BoardSpecIn,
        authorization: str | None = Header(default=None),
    ) -> dict:
        """Specs & QA: (re)write the acceptance spec on an existing brief. The
        next 'I'm done' on this brief is graded against whatever is saved
        here — a spec can be tightened after a brief has already gone out."""
        require_operator(request, authorization)
        try:
            return store.set_board_spec(board_id, body.spec)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/seats")
    def get_seats() -> dict:
        return {"seats": store.seats()}

    @app.get("/api/claude-accounts")
    def claude_accounts(request: Request, authorization: str | None = Header(default=None)) -> dict:
        require_operator(request, authorization)
        try:
            return seat_sync.list_accounts()
        except SeatSyncError:
            return {"accounts": [], "account": None, "quota": None, "handoff": False}

    @app.get("/api/seats/{seat_id}/usage")
    def seat_usage(
        seat_id: str, request: Request, authorization: str | None = Header(default=None)
    ) -> dict:
        require_operator(request, authorization)
        seat = store.seat(seat_id)
        if not seat:
            raise HTTPException(404, "unknown seat")
        sess = seat.get("session")
        try:
            return seat_sync.usage_stats(
                seat,
                label=seat.get("claude_label"),
                since=sess["started_at"] if sess else None,
            )
        except SeatSyncError as exc:
            return {
                "error": str(exc),
                "quota": None,
                "stats": None,
                "guest_stats": None,
                "guest_name": None,
            }

    @app.get("/api/usage/timeseries")
    def usage_timeseries(
        request: Request,
        group_by: str = "seat",
        authorization: str | None = Header(default=None),
    ) -> dict:
        """Daily token totals for the floor's usage graph.

        `seat` is one line per chair — that chair's Claude account's full
        history. `guest` is one line per guest name, built from the salon's own
        visit history (started_at..ends_at, or "still live" -> now) rather than
        the account's raw timeline, since one account can serve many guests.
        """
        require_operator(request, authorization)
        window_days = 14
        cutoff = time.time() - window_days * 86400
        seats_list = store.seats()

        if group_by == "guest":
            merged: dict[str, dict[str, int]] = {}
            for sess in store.sessions_since(cutoff):
                seat = next((s for s in seats_list if s["id"] == sess["seat_id"]), None)
                if not seat:
                    continue
                labels = sess.get("account_labels") or (
                    [seat["claude_label"]] if seat.get("claude_label") else []
                )
                until = sess.get("ends_at") or time.time()
                bucket = merged.setdefault(sess["coder_name"], {})
                for label in labels:
                    try:
                        resp = seat_sync.usage_stats(
                            seat, label=label, since=sess["started_at"], until=until
                        )
                    except SeatSyncError:
                        continue
                    for day in (resp.get("guest_stats") or {}).get("daily") or []:
                        bucket[day["date"]] = bucket.get(day["date"], 0) + day["total_tokens"]
            series = [
                {"key": name, "points": [{"date": d, "total_tokens": t} for d, t in sorted(pts.items())]}
                for name, pts in merged.items()
                if pts
            ]
        else:
            series = []
            for seat in seats_list:
                label = seat.get("claude_label")
                if not label:
                    continue
                try:
                    resp = seat_sync.usage_stats(seat, label=label)
                except SeatSyncError:
                    continue
                daily = (resp.get("stats") or {}).get("daily") or []
                points = [
                    {"date": d["date"], "total_tokens": d["total_tokens"]}
                    for d in daily
                    if d["date"] >= _date_str(cutoff)
                ]
                if points:
                    series.append({"key": seat["name"], "points": points})

        return {"group_by": group_by, "days": window_days, "series": series}

    @app.get("/api/floor/occupancy")
    def floor_occupancy(request: Request, authorization: str | None = Header(default=None)) -> dict:
        """Visits per day, for the floor's occupancy graph — real check-in
        history, not a live-instant snapshot dressed up as a trend."""
        require_operator(request, authorization)
        window_days = 14
        cutoff = time.time() - window_days * 86400
        counts: dict[str, int] = {}
        for sess in store.sessions_since(cutoff):
            day = _date_str(sess["started_at"])
            counts[day] = counts.get(day, 0) + 1
        points = [{"date": d, "visits": n} for d, n in sorted(counts.items())]
        return {"days": window_days, "points": points}

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

    def _slip_for(sess: dict, seat: dict) -> dict:
        """Compose, print, and record the check-in slip for a ready seat."""
        join = seat_join_url(seat, sess["unlock_otp"])
        image = compose_checkin_slip(
            salon=SALON_NAME,
            seat_name=seat["name"],
            coder_name=sess["coder_name"],
            board_title=None,
            otp=sess["unlock_otp"],
            wellness_minutes=90,
            break_after=50,
            wifi_ssid=None if on_demand_seats() else WIFI_SSID,
            join=join,
        )
        payload = {"session_id": sess["id"], "otp": sess["unlock_otp"], "seat": seat.get("name")}
        if printing.print_mode() == "relay":
            # The printer is at the counter and the desk is not. Queue the slip
            # and carry on — the QR is on screen either way, so a printer that
            # is offline delays a piece of paper, not the check-in.
            job_id = store.enqueue_print("check-in", payload, None)
            png = printing.save_slip_png(image, data, job_id)
            store.set_print_png(job_id, str(png))
            printed = {"mode": "relay", "job": job_id, "status": "queued", "png": str(png)}
        else:
            printed = print_slip(image, data)
            store.log_print("check-in", payload, printed.get("png"))
        qr_path = save_join_qr(join, data)
        host = public_host(seat["agent_url"])
        return {
            "join": join,
            "ssh": f"ssh guest@{host}",
            "guest": "/guest/",
            "tmux": "tmux attach -t claude-guest",
            "print": printed,
            "qr": str(qr_path),
        }

    def _raise_seat(sess: dict, seat: dict) -> None:
        """Background half of a cloud check-in.

        The container, its certificate, and its public hostname all have to
        exist before the slip is worth printing — a QR pointing at a seat that
        is not up yet is worse than a guest waiting ten seconds for one.
        """
        try:
            seats.provision(store, sess, seat)
            ready = store.seat(seat["id"]) or seat
            _slip_for(sess, ready)
            store.set_seat_runtime(seat["id"], state="ready", public_host=ready.get("public_host"))
        except (seats.SeatError, SeatSyncError, caddy.CaddyError) as exc:
            log.warning("BYOI: could not raise a seat for %s: %s", sess["id"], exc)
            _abandon_seat(sess, seat, str(exc))
        except Exception as exc:  # noqa: BLE001 - a check-in must not wedge the desk
            log.exception("BYOI: unexpected failure raising a seat for %s", sess["id"])
            _abandon_seat(sess, seat, str(exc))

    def _abandon_seat(sess: dict, seat: dict, why: str) -> None:
        """Clean up a half-raised seat, then leave the reason on the chair.

        Order matters: teardown clears the chair's runtime columns, so recording
        the failure first would erase it before the desk could poll for it.
        """
        seats.teardown(store, sess, seat)
        store.free_seat(seat["id"])
        store.set_seat_runtime(seat["id"], state="failed", error=why)

    @app.post("/api/sessions/check-in")
    def check_in(
        request: Request,
        body: CheckInIn,
        background: BackgroundTasks,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_operator(request, authorization)
        try:
            sess = store.check_in(body.seat_id, body.coder_name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        seat = store.seat(body.seat_id)
        assert seat

        if on_demand_seats():
            store.set_seat_runtime(body.seat_id, state="preparing")
            background.add_task(_raise_seat, sess, seat)
            return {
                "session": sess,
                "state": "preparing",
                "otp": sess["unlock_otp"],
                "seat_admitted": False,
                "poll": f"/api/sessions/{sess['id']}/seat",
            }

        try:
            seat_sync.admit_session(seat, sess)
        except SeatSyncError as exc:
            store.free_seat(body.seat_id)
            raise HTTPException(
                502,
                "seat did not accept the OTP — is the seat agent up on this PC?",
            ) from exc
        return {
            "session": sess,
            "state": "ready",
            **_slip_for(sess, seat),
            "otp": sess["unlock_otp"],
            "seat_admitted": True,
        }

    @app.get("/api/sessions/{session_id}/seat")
    def seat_state(request: Request, session_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Where a check-in has got to. The desk polls this while a seat comes up."""
        require_operator(request, authorization)
        sess = store.session(session_id)
        if not sess:
            raise HTTPException(404, "unknown session")
        seat = store.seat(sess["seat_id"]) or {}
        state = seat.get("state") or ("ready" if sess["status"] != "done" else "idle")
        out = {
            "session": sess,
            "seat": seat,
            "state": state,
            "error": seat.get("error"),
            "otp": sess["unlock_otp"],
        }
        if state == "ready":
            out["join"] = seat_join_url(seat, sess["unlock_otp"])
            out["qr"] = str(data / "last-qr.png")
            out["public_host"] = seat.get("public_host")
        return out

    @app.get("/api/live")
    def api_live(request: Request, seat_id: str | None = None, authorization: str | None = Header(default=None)) -> dict:
        """Mirror of each occupied seat's guest chat (Claude Code stream)."""
        require_operator(request, authorization)
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

    def _infra_job(session_id: str, seat: dict | None, path: str) -> None:
        """Pulling images takes a while; never make the guest wait on the claim."""
        try:
            if on_demand_seats():
                # The desk owns Docker. The seat only gets told where the
                # database ended up — see apps/api/infra.py.
                env = desk_infra.up(
                    session_id=session_id, container=seats.container_name(session_id)
                )
                seat_sync.push_infra_env(
                    seat, session_id=session_id, env=desk_infra.public_env(env), cwd=path
                )
            else:
                seat_sync.infra_up(seat, session_id=session_id, cwd=path)
        except Exception:
            # The guest can retry from the chat; a cold stack is not a failed claim.
            log.warning("BYOI: infrastructure for %s did not come up", session_id, exc_info=True)

    @app.post("/api/sessions/{session_id}/claim")
    def claim(session_id: str, body: ClaimIn, background_tasks: BackgroundTasks) -> dict:
        try:
            sess = store.claim(session_id, body.board_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ProjectBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        item = store.board_item(body.board_id)
        project = (item or {}).get("project")
        path = (project or {}).get("local_path") if project else None
        needs: list[str] = []
        if path:
            try:
                path = project_ops.ensure_local(project)
            except FileNotFoundError as exc:
                raise HTTPException(404, str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(502, f"could not fetch this project: {exc}") from exc
            seat = store.seat(sess["seat_id"])
            # On one PC the seat can just open the project folder. A seat
            # container cannot: the folder is not mounted into it, and mounting
            # the whole projects root would hand every guest every other guest's
            # work. So the project is cloned into this visit's own workspace.
            target = path
            if on_demand_seats():
                try:
                    target = seats.seed_workspace(session_id, path)
                except seats.SeatError as exc:
                    raise HTTPException(502, f"could not put this project on the seat: {exc}") from exc
            # Files the brief needs as inputs, fetched to a directory beside the
            # clone. Never fatal: a brief whose photos will not download is
            # still a brief the guest can work on, and a claim that 502s here
            # costs them their visit.
            media_target: str | None = None
            landed: list = []
            # Ask before making anything: most briefs have no media, and
            # media_dir() would otherwise create a runtime folder for every one.
            if store.media_for_board(body.board_id):
                try:
                    landed = media_ops.materialize(
                        store, body.board_id, seats.media_dir(session_id)
                    )
                except Exception as exc:  # noqa: BLE001 - a claim must not fail on media
                    log.warning("media for %s could not be prepared: %s", body.board_id, exc)
                    landed = []
            if landed:
                # The seat reaches it by the path *it* sees: a container looks at
                # the bind mount, a static seat at the desk's own filesystem.
                media_target = (
                    seats.GUEST_MEDIA if on_demand_seats() else str(seats.media_dir(session_id))
                )
            try:
                seat_sync.set_workspace(seat, target, media_target)
            except SeatSyncError as exc:
                raise HTTPException(502, "seat did not switch to this project's folder") from exc
            detected = project_ops.detect(path)
            needs = list(detected.get("needs") or [])
            if project and detected.get("framework"):
                store.set_project_framework(project["id"], detected["framework"])
            if needs:
                background_tasks.add_task(_infra_job, session_id, seat, target)
        return {"session": sess, "item": item, "project": project, "infra": needs}

    def _teardown_deployment(session_id: str | None) -> None:
        """Ephemeral by policy: nothing a guest deployed outlives their seat."""
        if not session_id:
            return
        live = store.deployment_for_session(session_id)
        if not live or live.get("torn_down_at") or live.get("state") == "torn_down":
            return
        try:
            result = deploy_ops.teardown(live)
            detail = "; ".join(result.get("problems") or []) or None
        except Exception as exc:
            detail = str(exc)
        store.mark_torn_down(live["id"], detail)

    def _lock_seat(seat: dict | None, session_id: str | None) -> None:
        """Lock the guest out. Leaves the container and its workspace alone —
        grading still needs to reach both after this returns."""
        if on_demand_seats() and seat and session_id:
            result = seats.lock(session_id, seat)
            for problem in result.get("problems", []):
                log.warning("BYOI: locking %s — %s", seat.get("id"), problem)
            return
        try:
            seat_sync.revoke_session(seat)
        except SeatSyncError as exc:
            raise HTTPException(502, "seat did not drop the OTP") from exc

    def _destroy_seat(seat: dict | None, session_id: str | None) -> None:
        """The rest of teardown. Only call once nothing still needs the
        container or its bind-mounted workspace — grading fetches the
        submission ref straight out of it."""
        if on_demand_seats() and seat and session_id:
            result = seats.destroy(store, session_id, seat)
            for problem in result.get("problems", []):
                log.warning("BYOI: freeing %s — %s", seat.get("id"), problem)

    def _revoke_seat(seat: dict | None, session_id: str | None = None) -> None:
        """Free a seat right now — for an operator forcing a chair free, or a
        visit with nothing to grade. The complete() grading path uses
        _lock_seat then _destroy_seat instead, so the container survives long
        enough to be graded."""
        _teardown_deployment(session_id)
        _lock_seat(seat, session_id)
        _destroy_seat(seat, session_id)

    @app.post("/api/seats/free-all")
    def free_all(request: Request, authorization: str | None = Header(default=None)) -> dict:
        require_operator(request, authorization)
        seen: set[str] = set()
        for seat in store.seats():
            live = store._live_session(seat["id"])
            _teardown_deployment((live or {}).get("id"))
            url = seat.get("agent_url") or ""
            if url in seen:
                continue
            seen.add(url)
            _revoke_seat(seat)
        return {"seats": store.free_all()}

    @app.post("/api/seats/{seat_id}/free")
    def free_seat(request: Request, seat_id: str, authorization: str | None = Header(default=None)) -> dict:
        require_operator(request, authorization)
        seat = store.seat(seat_id)
        if not seat:
            raise HTTPException(404, "unknown seat")
        live = store._live_session(seat_id)
        _revoke_seat(seat, (live or {}).get("id"))
        return {"seat": store.free_seat(seat_id)}

    def _host_test_job(
        session_id: str, seat: dict | None, *, spec: str, title: str, cwd: str | None
    ) -> dict:
        """Seat pins the tree to a git ref; the host generates, runs, and grades."""
        if on_demand_seats():
            # `cwd` is a desk path; the seat has its own. Sending None lets the
            # seat answer with the workspace it was given at claim time, and the
            # desk reads the pinned ref back out of the same bind mount rather
            # than making the seat push it anywhere.
            host_src = seats.workspace_source(session_id, cwd)
            local_ok = host_src is not None
            info = seat_sync.submit_solution(
                seat, session_id=session_id, cwd=None, push=not local_ok
            )
            source = str(host_src) if local_ok else info.get("remote")
        else:
            local_ok = bool(cwd) and Path(cwd).is_dir()
            info = seat_sync.submit_solution(
                seat, session_id=session_id, cwd=cwd, push=not local_ok
            )
            # One PC: fetch straight off the seat's repo. Two: the seat pushed it.
            source = info.get("toplevel") if local_ok else info.get("remote")
        ref = info.get("ref")
        if not ref:
            raise TestgenError("the seat did not pin a submission ref")
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
            # Both graders are down. `grader_error` keeps the guest's screen
            # honest about that: without it a dead pipeline reads on the phone
            # as a failed check, blaming the guest for our outage.
            report = {
                "summary": str(exc),
                "passed": 0,
                "failed": 1,
                "grader_error": True,
                "cases": [{"name": "seat verifier", "pass": False, "detail": str(exc)}],
            }
        if fallback_reason:
            note = f"Graded on the seat ({fallback_reason})."
            report["summary"] = f"{note} {report.get('summary') or ''}".strip()
        store.set_test_report(session_id, report)

    def _verify_then_destroy(session_id: str, seat: dict | None, item: dict | None) -> None:
        """Grade first — grading reads the container and its bind-mounted
        workspace — then release the seat for the next guest."""
        try:
            _verify_job(session_id, seat, item)
        finally:
            _destroy_seat(seat, session_id)

    @app.post("/api/sessions/{session_id}/complete")
    def complete(session_id: str, background_tasks: BackgroundTasks) -> dict:
        sess = store.session(session_id)
        if not sess:
            raise HTTPException(404, "unknown session")
        item = store.board_item(sess["board_id"]) if sess.get("board_id") else None
        seat = store.seat(sess["seat_id"])
        testing = bool((item or {}).get("spec"))
        _teardown_deployment(session_id)
        _lock_seat(seat, session_id)
        done = store.complete(session_id)
        if testing:
            # The container and workspace stay up until grading has read what
            # it needs from them — destroying either first is what used to
            # turn a real "seat did not pin a submission ref" into a bogus DNS
            # failure, after the container that grading needed was already gone.
            store.set_test_running(session_id)
            background_tasks.add_task(_verify_then_destroy, session_id, seat, item)
            done = store.session(session_id)
        else:
            _destroy_seat(seat, session_id)
        return {"session": done, "testing": testing, "item": item}

    def _deploy_job(deployment_id: str, session_id: str, seat: dict | None, item: dict | None) -> None:
        project = (item or {}).get("project") or {}
        cwd = project.get("local_path")
        desk_project_id = project.get("id")
        try:
            local_ok = bool(cwd) and Path(cwd).is_dir()
            info = seat_sync.submit_solution(
                seat, session_id=session_id, cwd=cwd, push=not local_ok, kind="deploy"
            )
            ref = info.get("ref")
            source = info.get("toplevel") if local_ok else info.get("remote")
            if not (ref and source):
                raise DeployError("the seat did not pin a deployable ref")
            result = deploy_ops.run(
                session_id=session_id,
                source=str(source),
                ref=str(ref),
                project_id=desk_project_id,
                # Every solution on the same desk project lands on the same
                # Vercel project — None on the first deploy, which lets
                # `vercel deploy` mint one.
                vercel_project_id=project.get("vercel_project_id"),
                vercel_org_id=project.get("vercel_org_id"),
                # And the same managed Postgres/Redis/auth secret, so the data
                # a guest's app writes is still there for whoever deploys this
                # project next.
                db_resources=project.get("infra_resources"),
            )
        except (DeployError, SeatSyncError) as exc:
            store.update_deployment(deployment_id, state="failed", detail=str(exc)[:1000])
            return
        except Exception as exc:  # never leave the phone polling "running"
            store.update_deployment(deployment_id, state="failed", detail=str(exc)[:1000])
            return
        if desk_project_id and result.get("vercel_project_id") and result.get("vercel_org_id"):
            store.set_project_vercel(
                desk_project_id,
                vercel_project_id=result["vercel_project_id"],
                vercel_org_id=result["vercel_org_id"],
            )
        if desk_project_id and result.get("resources") is not None:
            store.set_project_infra(desk_project_id, result["resources"])
        store.update_deployment(
            deployment_id,
            state="ready",
            url=result["url"],
            ref=str(info.get("ref") or ""),
            detail=result.get("detail"),
            resources=result.get("resources") or [],
        )
        # A preview that is up but broken is worth knowing about immediately.
        spec = (item or {}).get("spec") or ""
        if str(spec).strip():
            try:
                report = testgen.run_smoke(
                    spec=spec,
                    title=(item or {}).get("title") or "",
                    url=result["url"],
                    session_id=session_id,
                )
                store.set_test_report(session_id, report)
            except Exception as exc:
                store.update_deployment(
                    deployment_id, detail=f"deployed; smoke test skipped: {exc}"[:1000]
                )

    @app.post("/api/sessions/{session_id}/deploy")
    def deploy_session(
        session_id: str, body: DeployIn, background_tasks: BackgroundTasks
    ) -> dict:
        sess = store.session(session_id)
        if not sess:
            raise HTTPException(404, "unknown session")
        item = store.board_item(sess["board_id"]) if sess.get("board_id") else None
        project = (item or {}).get("project") or {}
        if not project.get("local_path"):
            raise HTTPException(409, "this brief has no project to deploy")
        seat = store.seat(sess["seat_id"])
        record = store.start_deployment(
            session_id=session_id, project_id=project.get("id")
        )
        background_tasks.add_task(_deploy_job, record["id"], session_id, seat, item)
        return {"deployment": record}

    @app.get("/api/sessions/{session_id}/deployment")
    def get_deployment(session_id: str) -> dict:
        if not store.session(session_id):
            raise HTTPException(404, "unknown session")
        return {"session_id": session_id, "deployment": store.deployment_for_session(session_id)}

    @app.get("/api/sessions/grading")
    def grading_sessions(
        request: Request, authorization: str | None = Header(default=None)
    ) -> dict:
        """Specs & QA: what the desk shows once a guest taps 'I'm done' — the
        seat and board panels stop tracking a visit the moment it completes,
        so this is the only place left to watch a suite run and see what it
        found, including after the seat has been freed."""
        require_operator(request, authorization)
        return {"sessions": store.grading_sessions()}

    @app.get("/api/sessions/{session_id}/tests")
    def session_tests(session_id: str) -> dict:
        """The guest's own result, redacted.

        This is the one grading endpoint a phone can reach without operator
        auth, so it never serves the stored report: that quotes the suite —
        assertion source, test paths, tracebacks — and the guest is graded
        blind. `guest_report.redact` rebuilds it from the spec clauses plus
        reasons it writes itself. The full report stays on /api/sessions/grading.
        """
        sess = store.session(session_id)
        if not sess:
            raise HTTPException(404, "unknown session")
        return {
            "session_id": session_id,
            "test_status": sess.get("test_status"),
            "test_report": guest_report.redact(sess.get("test_report")),
        }

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        sess = store.session(session_id)
        if not sess:
            raise HTTPException(404, "unknown session")
        item = store.board_item(sess["board_id"]) if sess.get("board_id") else None
        return {"session": sess, "item": item, "seat": store.seat(sess["seat_id"])}

    def _live_seat_url() -> str:
        """The seat a guest on the desk's own origin is talking to.

        With one seat per session there is no single address to hardcode, so the
        live one is looked up. The env override stays for the salon PC, where
        there is exactly one seat agent and it is not this process.
        """
        override = os.environ.get("BYOI_SEAT_URL", "").strip()
        if override:
            return override
        for seat in store.seats():
            if seat.get("session") and seat.get("agent_url"):
                return seat["agent_url"]
        return "https://127.0.0.1:8787"

    def require_relay(request: Request, authorization: str | None) -> None:
        """The venue's printer agent, or an operator looking at the queue."""
        token = read_secret("BYOI_PRINT_RELAY_TOKEN")
        if token:
            presented = (authorization or "").strip()
            expected = f"Bearer {token}"
            if len(presented) == len(expected) and hmac.compare_digest(presented, expected):
                _relay["seen_at"] = time.time()
                return
        require_operator(request, authorization)

    @app.get("/api/print/status")
    def print_status(request: Request, authorization: str | None = Header(default=None)) -> dict:
        require_operator(request, authorization)
        seen = _relay.get("seen_at")
        return {
            "mode": printing.print_mode(),
            # The relay polls; if it has not in a while, the counter's printer
            # is unreachable and the operator should know before a guest waits.
            "online": bool(seen and time.time() - seen < RELAY_OFFLINE_AFTER),
            "seen_at": seen,
            **store.print_queue(),
        }

    @app.get("/api/print/next")
    def print_next(request: Request, authorization: str | None = Header(default=None)) -> Response:
        """Hand the relay the next slip. 204 when there is nothing to print."""
        require_relay(request, authorization)
        _relay["seen_at"] = time.time()
        job = store.claim_print_job()
        if not job:
            return Response(status_code=204)
        return JSONResponse(
            {
                "id": job["id"],
                "kind": job["kind"],
                "payload": job["payload"],
                "png": f"/api/print/{job['id']}.png",
            }
        )

    @app.get("/api/print/{job_id}.png")
    def print_png(request: Request, job_id: str, authorization: str | None = Header(default=None)) -> FileResponse:
        require_relay(request, authorization)
        job = store.print_job(job_id)
        path = Path(job["dumped_path"]) if job and job.get("dumped_path") else None
        if not path or not path.is_file():
            raise HTTPException(404, "no image for this print job")
        return FileResponse(path, media_type="image/png", filename=f"{job_id}.png")

    @app.post("/api/print/{job_id}/done")
    def print_done(
        request: Request,
        job_id: str,
        body: PrintDoneIn,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_relay(request, authorization)
        job = store.finish_print_job(job_id, ok=body.ok, error=body.error)
        if not job:
            raise HTTPException(404, "unknown print job")
        return {"job": job}

    @app.get("/api/join")
    def join(otp: str) -> dict:
        sess = store.session_by_otp(otp)
        if not sess:
            raise HTTPException(404, "unknown slip")
        if sess["status"] == "done":
            raise HTTPException(410, "session finished")
        seat = store.seat(sess["seat_id"])
        agent = (
            public_base(seat["agent_url"], public_host=seat.get("public_host")) if seat else None
        )
        sync_all_github_projects()
        return {
            "session": sess,
            "seat": seat,
            "board": store.board(),
            "seat_agent": agent,
            # None when the seat is a cloud container: there is no salon Wi-Fi
            # for the guest to be on, and naming one sends them looking for it.
            "wifi_ssid": None if on_demand_seats() else WIFI_SSID,
        }

    @app.get("/join")
    def join_page(otp: str | None = None) -> RedirectResponse:
        q = f"?otp={otp}" if otp else ""
        return RedirectResponse(url=f"/guest/{q}")

    @app.post("/local/unlock")
    async def unlock_via_seat(request: Request):
        """Forward unlock to the seat agent so the PWA can stay same-origin on :8080."""
        seat_url = _live_seat_url()
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
        seat_url = _live_seat_url()
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
