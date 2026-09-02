"""SQLite persistence for seats, board, sessions."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from . import seed_board


class ProjectBusy(RuntimeError):
    """Another live session already holds this project."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS seats (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    claude_label TEXT NOT NULL,
    pan_ssid TEXT NOT NULL,
    pan_cidr TEXT NOT NULL DEFAULT '192.168.44.0/24',
    agent_url TEXT NOT NULL DEFAULT 'http://127.0.0.1:8787',
    status TEXT NOT NULL DEFAULT 'idle',
    -- A chair is a place in the room. The container that serves it is raised at
    -- check-in and destroyed at checkout, so everything below is per-visit.
    container_id TEXT,
    public_host TEXT,
    state TEXT NOT NULL DEFAULT 'idle',
    error TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    github TEXT,
    local_path TEXT NOT NULL,
    created_at REAL NOT NULL,
    -- Set from the first deploy of any solution on this project, so every
    -- later deploy — this guest's or the next guest's — lands on the same
    -- Vercel project instead of minting a new one per visit.
    vercel_project_id TEXT,
    vercel_org_id TEXT,
    -- Managed Postgres/Redis/auth secret provisioned for this project's
    -- deploys, same shape as deployments.resources. Kept across guests and
    -- sessions so the data a guest's app writes is still there for whoever
    -- deploys this project next.
    infra_resources TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS board (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    brief TEXT NOT NULL,
    wellness_minutes INTEGER NOT NULL DEFAULT 90,
    break_after INTEGER NOT NULL DEFAULT 50,
    published INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    project_id TEXT,
    spec TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    seat_id TEXT NOT NULL,
    coder_name TEXT NOT NULL,
    board_id TEXT,
    started_at REAL NOT NULL,
    ends_at REAL,
    status TEXT NOT NULL DEFAULT 'checked_in',
    unlock_otp TEXT,
    rc_url TEXT,
    test_status TEXT,
    test_report TEXT,
    account_labels TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (seat_id) REFERENCES seats(id)
);
CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    project_id TEXT,
    provider TEXT NOT NULL DEFAULT 'vercel',
    state TEXT NOT NULL DEFAULT 'pending',
    url TEXT,
    ref TEXT,
    detail TEXT,
    resources TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    torn_down_at REAL,
    FOREIGN KEY (session_id) REFERENCES sessions (id)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS print_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    dumped_path TEXT,
    -- The printer is Bluetooth, so it stays at the counter while the desk is in
    -- the cloud. A relay at the venue claims jobs from here.
    status TEXT NOT NULL DEFAULT 'done',
    claimed_at REAL,
    finished_at REAL,
    error TEXT
);
"""

SEED_SEATS = [
    ("seat-1", "Seat 1 — Window", "claude-seat-1", "salon Wi-Fi"),
    ("seat-2", "Seat 2 — Fern", "claude-seat-2", "salon Wi-Fi"),
    ("seat-3", "Seat 3 — Booth", "claude-seat-3", "salon Wi-Fi"),
]


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()
        self._seed()
        self._reconcile_occupancy()

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        if "project_id" not in self._columns("board"):
            self.conn.execute("ALTER TABLE board ADD COLUMN project_id TEXT")
        if "spec" not in self._columns("board"):
            self.conn.execute("ALTER TABLE board ADD COLUMN spec TEXT NOT NULL DEFAULT ''")
        proj_cols = self._columns("projects")
        if "framework" not in proj_cols:
            self.conn.execute("ALTER TABLE projects ADD COLUMN framework TEXT")
        if "template" not in proj_cols:
            self.conn.execute("ALTER TABLE projects ADD COLUMN template TEXT")
        if "vercel_project_id" not in proj_cols:
            self.conn.execute("ALTER TABLE projects ADD COLUMN vercel_project_id TEXT")
        if "vercel_org_id" not in proj_cols:
            self.conn.execute("ALTER TABLE projects ADD COLUMN vercel_org_id TEXT")
        if "infra_resources" not in proj_cols:
            self.conn.execute(
                "ALTER TABLE projects ADD COLUMN infra_resources TEXT NOT NULL DEFAULT '[]'"
            )
        sess_cols = self._columns("sessions")
        if "test_status" not in sess_cols:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN test_status TEXT")
        if "test_report" not in sess_cols:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN test_report TEXT")
        if "account_labels" not in sess_cols:
            self.conn.execute(
                "ALTER TABLE sessions ADD COLUMN account_labels TEXT NOT NULL DEFAULT '[]'"
            )
        print_cols = self._columns("print_jobs")
        for column, ddl in (
            ("status", "ALTER TABLE print_jobs ADD COLUMN status TEXT NOT NULL DEFAULT 'done'"),
            ("claimed_at", "ALTER TABLE print_jobs ADD COLUMN claimed_at REAL"),
            ("finished_at", "ALTER TABLE print_jobs ADD COLUMN finished_at REAL"),
            ("error", "ALTER TABLE print_jobs ADD COLUMN error TEXT"),
        ):
            if column not in print_cols:
                self.conn.execute(ddl)
        seat_cols = self._columns("seats")
        for column, ddl in (
            ("container_id", "ALTER TABLE seats ADD COLUMN container_id TEXT"),
            ("public_host", "ALTER TABLE seats ADD COLUMN public_host TEXT"),
            ("state", "ALTER TABLE seats ADD COLUMN state TEXT NOT NULL DEFAULT 'idle'"),
            ("error", "ALTER TABLE seats ADD COLUMN error TEXT"),
        ):
            if column not in seat_cols:
                self.conn.execute(ddl)
        self.conn.commit()

    def _seed(self) -> None:
        n = self.conn.execute("SELECT COUNT(*) FROM seats").fetchone()[0]
        if n == 0:
            for sid, name, label, ssid in SEED_SEATS:
                self.conn.execute(
                    "INSERT INTO seats (id, name, claude_label, pan_ssid) VALUES (?,?,?,?)",
                    (sid, name, label, ssid),
                )
        self._seed_board()
        self.conn.commit()

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _seed_project_id(self) -> str:
        """One project row for the site the default board works on."""
        github = seed_board.SEED_PROJECT["github"]
        row = self.conn.execute("SELECT id FROM projects WHERE github=?", (github,)).fetchone()
        if row:
            return row["id"]
        from .projects import projects_root

        local = projects_root() / seed_board.SEED_PROJECT["slug"]
        made = self.add_project(
            name=seed_board.SEED_PROJECT["name"], local_path=str(local), github=github
        )
        return made["id"]

    def _retire_defaults(self) -> None:
        """Take an earlier default off the board without erasing a past visit."""
        rows = self.conn.execute(
            "SELECT id FROM board WHERE id LIKE 'seed-%' OR title IN ({})".format(
                ",".join("?" * len(seed_board.LEGACY_TITLES)) or "''"
            ),
            tuple(seed_board.LEGACY_TITLES),
        ).fetchall()
        for row in rows:
            used = self.conn.execute(
                "SELECT 1 FROM sessions WHERE board_id=? LIMIT 1", (row["id"],)
            ).fetchone()
            if used:
                self.conn.execute("UPDATE board SET published=0 WHERE id=?", (row["id"],))
            else:
                self.conn.execute("DELETE FROM board WHERE id=?", (row["id"],))

    def _seed_board(self) -> None:
        """Publish apps/api/seed_board.py. Host-written briefs are left alone."""
        if self._meta("board_seed") == seed_board.SEED_VERSION:
            return
        project_id = self._seed_project_id()
        self._retire_defaults()
        now = time.time()
        for offset, item in enumerate(seed_board.SEED_BOARD):
            self.conn.execute(
                "INSERT INTO board (id, title, brief, wellness_minutes, break_after, "
                "published, created_at, project_id, spec) VALUES (?,?,?,?,?,1,?,?,?)",
                (
                    f"{item['id']}@{seed_board.SEED_VERSION}",
                    item["title"],
                    item["brief"],
                    item["wellness_minutes"],
                    item["break_after"],
                    now - offset,
                    project_id,
                    item["spec"],
                ),
            )
        self._set_meta("board_seed", seed_board.SEED_VERSION)

    def _live_session(self, seat_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE seat_id=? AND status IN ('checked_in','active') "
            "ORDER BY started_at DESC LIMIT 1",
            (seat_id,),
        ).fetchone()
        return self._session_dict(row)

    def _reconcile_occupancy(self) -> None:
        """Seat status follows live sessions so a finished visit cannot stick as occupied."""
        self.conn.execute(
            """
            UPDATE seats SET status='idle', state='idle', container_id=NULL,
                             public_host=NULL, error=NULL
            WHERE id NOT IN (
                SELECT seat_id FROM sessions WHERE status IN ('checked_in','active')
            )
            """
        )
        self.conn.execute(
            """
            UPDATE seats SET status='occupied'
            WHERE id IN (
                SELECT seat_id FROM sessions WHERE status IN ('checked_in','active')
            )
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def seats(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM seats ORDER BY name").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            sess = self._live_session(row["id"])
            item["session"] = sess
            item["status"] = "occupied" if sess else "idle"
            out.append(item)
        return out

    def seat(self, seat_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM seats WHERE id=?", (seat_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        sess = self._live_session(seat_id)
        item["session"] = sess
        item["status"] = "occupied" if sess else "idle"
        return item

    def _session_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        item = dict(row)
        raw = item.get("test_report")
        if isinstance(raw, str) and raw.strip():
            try:
                item["test_report"] = json.loads(raw)
            except json.JSONDecodeError:
                pass
        labels = item.get("account_labels")
        if isinstance(labels, str):
            try:
                item["account_labels"] = json.loads(labels or "[]")
            except json.JSONDecodeError:
                item["account_labels"] = []
        return item

    def _with_project(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        pid = item.get("project_id")
        item["project"] = self.project(pid) if pid else None
        return item

    def _project_dict(self, row: Any) -> dict[str, Any] | None:
        if not row:
            return None
        item = dict(row)
        raw = item.get("infra_resources")
        try:
            item["infra_resources"] = json.loads(raw) if raw else []
        except (TypeError, json.JSONDecodeError):
            item["infra_resources"] = []
        return item

    def projects(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [self._project_dict(r) for r in rows]  # type: ignore[misc]

    def project(self, project_id: str | None) -> dict[str, Any] | None:
        if not project_id:
            return None
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return self._project_dict(row)

    def add_project(
        self,
        *,
        name: str,
        local_path: str,
        github: str | None = None,
        framework: str | None = None,
        template: str | None = None,
    ) -> dict[str, Any]:
        item = {
            "id": str(uuid.uuid4())[:8],
            "name": name.strip() or Path(local_path).name,
            "github": (github or "").strip() or None,
            "local_path": str(Path(local_path).expanduser()),
            "framework": (framework or "").strip() or None,
            "template": (template or "").strip() or None,
            "created_at": time.time(),
        }
        self.conn.execute(
            "INSERT INTO projects (id, name, github, local_path, framework, template, created_at) "
            "VALUES (:id,:name,:github,:local_path,:framework,:template,:created_at)",
            item,
        )
        self.conn.commit()
        return item

    def set_project_framework(self, project_id: str, framework: str | None) -> None:
        self.conn.execute(
            "UPDATE projects SET framework=? WHERE id=?", ((framework or "") or None, project_id)
        )
        self.conn.commit()

    def set_project_vercel(self, project_id: str, *, vercel_project_id: str, vercel_org_id: str) -> None:
        """Record the Vercel project a desk project deploys to, first-write-wins."""
        self.conn.execute(
            "UPDATE projects SET vercel_project_id=?, vercel_org_id=? "
            "WHERE id=? AND vercel_project_id IS NULL",
            (vercel_project_id, vercel_org_id, project_id),
        )
        self.conn.commit()

    def set_project_infra(self, project_id: str, resources: list[dict[str, Any]]) -> None:
        """Replace the project's carried-over infra (Postgres/Redis/auth) with
        the caller's already-merged existing-plus-newly-provisioned list."""
        self.conn.execute(
            "UPDATE projects SET infra_resources=? WHERE id=?",
            (json.dumps(resources), project_id),
        )
        self.conn.commit()

    def _busy_project_ids(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT b.project_id FROM sessions s JOIN board b ON b.id = s.board_id "
            "WHERE b.project_id IS NOT NULL AND s.status IN ('checked_in','active')"
        ).fetchall()
        return {row[0] for row in rows}

    def board(self, published_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM board"
        if published_only:
            sql += " WHERE published=1"
        sql += " ORDER BY created_at DESC"
        busy = self._busy_project_ids()
        items = [self._with_project(dict(r)) for r in self.conn.execute(sql).fetchall()]  # type: ignore[misc]
        for item in items:
            pid = item.get("project_id")
            item["project_busy"] = bool(pid) and pid in busy
        return items

    def board_item(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM board WHERE id=?", (item_id,)).fetchone()
        return self._with_project(dict(row) if row else None)

    def add_board(
        self,
        title: str,
        brief: str,
        wellness_minutes: int,
        break_after: int,
        project_id: str | None = None,
        spec: str = "",
    ) -> dict[str, Any]:
        if project_id and not self.project(project_id):
            raise KeyError("unknown project")
        item = {
            "id": str(uuid.uuid4())[:8],
            "title": title,
            "brief": brief,
            "wellness_minutes": wellness_minutes,
            "break_after": break_after,
            "published": 1,
            "created_at": time.time(),
            "project_id": project_id,
            "spec": (spec or "").strip(),
        }
        self.conn.execute(
            "INSERT INTO board (id, title, brief, wellness_minutes, break_after, published, created_at, project_id, spec) "
            "VALUES (:id,:title,:brief,:wellness_minutes,:break_after,:published,:created_at,:project_id,:spec)",
            item,
        )
        self.conn.commit()
        return self.board_item(item["id"])  # type: ignore[return-value]

    def set_board_project(self, board_id: str, project_id: str | None) -> dict[str, Any]:
        item = self.board_item(board_id)
        if not item:
            raise KeyError("unknown board item")
        if project_id and not self.project(project_id):
            raise KeyError("unknown project")
        self.conn.execute("UPDATE board SET project_id=? WHERE id=?", (project_id, board_id))
        self.conn.commit()
        return self.board_item(board_id)  # type: ignore[return-value]

    def set_board_spec(self, board_id: str, spec: str) -> dict[str, Any]:
        item = self.board_item(board_id)
        if not item:
            raise KeyError("unknown board item")
        self.conn.execute("UPDATE board SET spec=? WHERE id=?", ((spec or "").strip(), board_id))
        self.conn.commit()
        return self.board_item(board_id)  # type: ignore[return-value]

    def check_in(self, seat_id: str, coder_name: str) -> dict[str, Any]:
        seat = self.seat(seat_id)
        if not seat:
            raise KeyError("unknown seat")
        live = self.conn.execute(
            "SELECT id FROM sessions WHERE seat_id=? AND status IN ('checked_in','active')",
            (seat_id,),
        ).fetchone()
        if live:
            raise ValueError("seat occupied")
        otp = uuid.uuid4().hex[:8]
        sess = {
            "id": str(uuid.uuid4())[:12],
            "seat_id": seat_id,
            "coder_name": coder_name.strip() or "guest",
            "board_id": None,
            "started_at": time.time(),
            "ends_at": None,
            "status": "checked_in",
            "unlock_otp": otp,
            "rc_url": None,
        }
        self.conn.execute(
            "INSERT INTO sessions (id, seat_id, coder_name, board_id, started_at, ends_at, status, unlock_otp, rc_url) "
            "VALUES (:id,:seat_id,:coder_name,:board_id,:started_at,:ends_at,:status,:unlock_otp,:rc_url)",
            sess,
        )
        self.conn.execute("UPDATE seats SET status='occupied' WHERE id=?", (seat_id,))
        self.conn.commit()
        return sess

    def set_seat_runtime(
        self,
        seat_id: str,
        *,
        state: str,
        agent_url: str | None = None,
        container_id: str | None = None,
        public_host: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        """Record what is currently serving this chair.

        ``agent_url`` is the container's address on the internal network, which
        is also where ``seat_sync.control_base`` derives the mTLS control URL
        from — so a chair with no container has nothing to talk to, by
        construction rather than by a stale default.
        """
        sets = ["state=?"]
        args: list[Any] = [state]
        for column, value in (
            ("agent_url", agent_url),
            ("container_id", container_id),
            ("public_host", public_host),
        ):
            if value is not None:
                sets.append(f"{column}=?")
                args.append(value)
        sets.append("error=?")
        args.append(error)
        args.append(seat_id)
        self.conn.execute(f"UPDATE seats SET {', '.join(sets)} WHERE id=?", args)
        self.conn.commit()
        return self.seat(seat_id)

    def clear_seat_runtime(self, seat_id: str) -> None:
        self.conn.execute(
            "UPDATE seats SET state='idle', container_id=NULL, public_host=NULL, error=NULL "
            "WHERE id=?",
            (seat_id,),
        )
        self.conn.commit()

    def set_session_accounts(self, session_id: str, labels: list[str]) -> None:
        self.conn.execute(
            "UPDATE sessions SET account_labels=? WHERE id=?",
            (json.dumps(labels), session_id),
        )
        self.conn.commit()

    def accounts_in_use(self, *, excluding: str | None = None) -> set[str]:
        """Labels held by other live visits.

        Two Claude Code processes pointed at one credential directory tread on
        each other, so an account is handed to at most one seat at a time.
        """
        rows = self.conn.execute(
            "SELECT id, account_labels FROM sessions WHERE status IN ('checked_in','active')"
        ).fetchall()
        held: set[str] = set()
        for row in rows:
            if excluding and row["id"] == excluding:
                continue
            try:
                held.update(json.loads(row["account_labels"] or "[]"))
            except ValueError:
                continue
        return held

    def live_sessions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE status IN ('checked_in','active')"
        ).fetchall()
        return [self._session_dict(row) for row in rows if row]  # type: ignore[misc]

    def sessions_since(self, cutoff: float) -> list[dict[str, Any]]:
        """Visits worth charting: started within the window, plus any still live
        regardless of how old — `ends_at` is a *planned* wellness cutoff, not an
        actual end time, so a still-active old visit must not be dropped."""
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE started_at>=? OR status IN ('checked_in','active') "
            "ORDER BY started_at DESC",
            (cutoff,),
        ).fetchall()
        return [self._session_dict(row) for row in rows if row]  # type: ignore[misc]

    def active_session_for_project(
        self, project_id: str | None, *, exclude_session_id: str | None = None
    ) -> dict[str, Any] | None:
        """The other live session already claimed onto this project, if any."""
        if not project_id:
            return None
        sql = (
            "SELECT s.* FROM sessions s JOIN board b ON b.id = s.board_id "
            "WHERE b.project_id=? AND s.status IN ('checked_in','active')"
        )
        args: list[Any] = [project_id]
        if exclude_session_id:
            sql += " AND s.id != ?"
            args.append(exclude_session_id)
        row = self.conn.execute(sql, args).fetchone()
        return self._session_dict(row)

    def claim(self, session_id: str, board_id: str) -> dict[str, Any]:
        item = self.board_item(board_id)
        if not item:
            raise KeyError("unknown board item")
        sess = self.session(session_id)
        if not sess:
            raise KeyError("unknown session")
        project_id = item.get("project_id")
        other = self.active_session_for_project(project_id, exclude_session_id=session_id)
        if other:
            raise ProjectBusy("another guest is already working on this project")
        ends = sess["started_at"] + item["wellness_minutes"] * 60
        self.conn.execute(
            "UPDATE sessions SET board_id=?, status='active', ends_at=? WHERE id=?",
            (board_id, ends, session_id),
        )
        self.conn.commit()
        return self.session(session_id)  # type: ignore[return-value]

    def complete(self, session_id: str) -> dict[str, Any]:
        sess = self.session(session_id)
        if not sess:
            raise KeyError("unknown session")
        self.conn.execute("UPDATE sessions SET status='done' WHERE id=?", (session_id,))
        self.conn.execute("UPDATE seats SET status='idle' WHERE id=?", (sess["seat_id"],))
        self.conn.commit()
        return self.session(session_id)  # type: ignore[return-value]

    def free_seat(self, seat_id: str) -> dict[str, Any]:
        """End any live session on this seat and mark it idle."""
        seat = self.seat(seat_id)
        if not seat:
            raise KeyError("unknown seat")
        self.conn.execute(
            "UPDATE sessions SET status='done' WHERE seat_id=? AND status IN ('checked_in','active')",
            (seat_id,),
        )
        self.conn.execute("UPDATE seats SET status='idle' WHERE id=?", (seat_id,))
        self.conn.commit()
        return self.seat(seat_id)  # type: ignore[return-value]

    def free_all(self) -> list[dict[str, Any]]:
        self.conn.execute(
            "UPDATE sessions SET status='done' WHERE status IN ('checked_in','active')"
        )
        self.conn.execute("UPDATE seats SET status='idle'")
        self.conn.commit()
        return self.seats()

    def session(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return self._session_dict(row)

    def set_test_running(self, session_id: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET test_status='running', test_report=NULL WHERE id=?",
            (session_id,),
        )
        self.conn.commit()

    def set_test_report(self, session_id: str, report: dict[str, Any]) -> dict[str, Any] | None:
        failed = int(report.get("failed") or 0)
        status = "failed" if failed else "passed"
        if not report.get("cases"):
            status = "passed" if not report.get("summary") else status
        self.conn.execute(
            "UPDATE sessions SET test_status=?, test_report=? WHERE id=?",
            (status, json.dumps(report), session_id),
        )
        self.conn.commit()
        return self.session(session_id)

    def grading_sessions(self, limit: int = 25) -> list[dict[str, Any]]:
        """Every visit that has ever had a spec graded, most recent first.

        `complete()` flips the seat back to idle and drops the session out of
        `_live_session` right away, so this is the only place the desk can
        still find a visit while its suite is running in the background — and
        afterwards, for the host to review what passed and what didn't.
        """
        rows = self.conn.execute(
            """
            SELECT s.*, b.title AS brief_title, se.name AS seat_name
            FROM sessions s
            LEFT JOIN board b ON b.id = s.board_id
            LEFT JOIN seats se ON se.id = s.seat_id
            WHERE s.test_status IS NOT NULL
            ORDER BY s.started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._session_dict(row) for row in rows]  # type: ignore[misc]

    # ------------------------------------------------------------- deployments

    def _deployment_dict(self, row: Any) -> dict[str, Any] | None:
        if not row:
            return None
        item = dict(row)
        raw = item.get("resources")
        try:
            item["resources"] = json.loads(raw) if raw else []
        except (TypeError, json.JSONDecodeError):
            item["resources"] = []
        return item

    def deployment(self, deployment_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        return self._deployment_dict(row)

    def deployment_for_session(self, session_id: str) -> dict[str, Any] | None:
        """The newest deployment for a session, torn down or not."""
        row = self.conn.execute(
            "SELECT * FROM deployments WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return self._deployment_dict(row)

    def live_deployments(self) -> list[dict[str, Any]]:
        """Everything still costing money — what teardown has to reach."""
        rows = self.conn.execute(
            "SELECT * FROM deployments WHERE torn_down_at IS NULL AND state != 'torn_down' "
            "ORDER BY created_at DESC"
        ).fetchall()
        return [d for d in (self._deployment_dict(r) for r in rows) if d]

    def start_deployment(
        self, *, session_id: str, project_id: str | None = None, provider: str = "vercel"
    ) -> dict[str, Any]:
        item = {
            "id": str(uuid.uuid4())[:8],
            "session_id": session_id,
            "project_id": project_id,
            "provider": provider,
            "state": "running",
            "url": None,
            "ref": None,
            "detail": None,
            "resources": "[]",
            "created_at": time.time(),
            "torn_down_at": None,
        }
        self.conn.execute(
            "INSERT INTO deployments (id, session_id, project_id, provider, state, url, ref, "
            "detail, resources, created_at, torn_down_at) "
            "VALUES (:id,:session_id,:project_id,:provider,:state,:url,:ref,:detail,:resources,"
            ":created_at,:torn_down_at)",
            item,
        )
        self.conn.commit()
        return self.deployment(item["id"]) or {}

    def update_deployment(
        self,
        deployment_id: str,
        *,
        state: str | None = None,
        url: str | None = None,
        ref: str | None = None,
        detail: str | None = None,
        resources: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        sets: list[str] = []
        args: list[Any] = []
        for field, value in (("state", state), ("url", url), ("ref", ref), ("detail", detail)):
            if value is not None:
                sets.append(f"{field}=?")
                args.append(value)
        if resources is not None:
            sets.append("resources=?")
            args.append(json.dumps(resources))
        if not sets:
            return self.deployment(deployment_id)
        args.append(deployment_id)
        self.conn.execute(f"UPDATE deployments SET {', '.join(sets)} WHERE id=?", args)
        self.conn.commit()
        return self.deployment(deployment_id)

    def mark_torn_down(self, deployment_id: str, detail: str | None = None) -> dict[str, Any] | None:
        self.conn.execute(
            "UPDATE deployments SET state='torn_down', torn_down_at=?, detail=COALESCE(?, detail) "
            "WHERE id=?",
            (time.time(), detail, deployment_id),
        )
        self.conn.commit()
        return self.deployment(deployment_id)

    def session_by_otp(self, otp: str) -> dict[str, Any] | None:
        needle = (otp or "").strip().casefold()
        if not needle:
            return None
        row = self.conn.execute("SELECT * FROM sessions WHERE lower(unlock_otp)=?", (needle,)).fetchone()
        return self._session_dict(row)

    def set_rc_url(self, session_id: str, url: str) -> None:
        self.conn.execute("UPDATE sessions SET rc_url=? WHERE id=?", (url, session_id))
        self.conn.commit()

    def enqueue_print(self, kind: str, payload: dict[str, Any], png_path: str | None) -> str:
        """Queue a slip for the relay at the venue to claim and print."""
        job_id = str(uuid.uuid4())[:10]
        self.conn.execute(
            "INSERT INTO print_jobs (id, kind, payload, created_at, dumped_path, status) "
            "VALUES (?,?,?,?,?,'queued')",
            (job_id, kind, json.dumps(payload), time.time(), png_path),
        )
        self.conn.commit()
        return job_id

    def claim_print_job(self, *, stale_after: float = 120.0) -> dict[str, Any] | None:
        """Hand the oldest waiting slip to the relay.

        A claim that is never finished is handed out again: the relay is a
        laptop at a counter and it will be closed, lose Wi-Fi, and be reopened.
        Reprinting a slip is cheap; silently never printing one is not.
        """
        cutoff = time.time() - stale_after
        row = self.conn.execute(
            "SELECT * FROM print_jobs WHERE status='queued' "
            "   OR (status='claimed' AND COALESCE(claimed_at, 0) < ?) "
            "ORDER BY created_at LIMIT 1",
            (cutoff,),
        ).fetchone()
        if not row:
            return None
        self.conn.execute(
            "UPDATE print_jobs SET status='claimed', claimed_at=? WHERE id=?",
            (time.time(), row["id"]),
        )
        self.conn.commit()
        return self.print_job(row["id"])

    def set_print_png(self, job_id: str, png_path: str) -> None:
        self.conn.execute("UPDATE print_jobs SET dumped_path=? WHERE id=?", (png_path, job_id))
        self.conn.commit()

    def print_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM print_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["payload"] = json.loads(item.get("payload") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        return item

    def finish_print_job(self, job_id: str, *, ok: bool, error: str | None = None) -> dict[str, Any] | None:
        self.conn.execute(
            "UPDATE print_jobs SET status=?, finished_at=?, error=? WHERE id=?",
            ("done" if ok else "failed", time.time(), None if ok else (error or "print failed"), job_id),
        )
        self.conn.commit()
        return self.print_job(job_id)

    def print_queue(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM print_jobs GROUP BY status"
        ).fetchall()
        counts = {row["status"]: row["n"] for row in rows}
        last = self.conn.execute(
            "SELECT id, status, created_at, finished_at, error FROM print_jobs "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return {
            "queued": counts.get("queued", 0),
            "claimed": counts.get("claimed", 0),
            "failed": counts.get("failed", 0),
            "last": dict(last) if last else None,
        }

    def log_print(self, kind: str, payload: dict[str, Any], dumped_path: str | None) -> str:
        job_id = str(uuid.uuid4())[:10]
        self.conn.execute(
            "INSERT INTO print_jobs (id, kind, payload, created_at, dumped_path) VALUES (?,?,?,?,?)",
            (job_id, kind, json.dumps(payload), time.time(), dumped_path),
        )
        self.conn.commit()
        return job_id
