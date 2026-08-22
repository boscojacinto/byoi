"""SQLite persistence for seats, board, sessions."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS seats (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    claude_label TEXT NOT NULL,
    pan_ssid TEXT NOT NULL,
    pan_cidr TEXT NOT NULL DEFAULT '192.168.44.0/24',
    agent_url TEXT NOT NULL DEFAULT 'http://127.0.0.1:8787',
    status TEXT NOT NULL DEFAULT 'idle'
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    github TEXT,
    local_path TEXT NOT NULL,
    created_at REAL NOT NULL
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
CREATE TABLE IF NOT EXISTS print_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    dumped_path TEXT
);
"""

SEED_SEATS = [
    ("seat-1", "Seat 1 — Window", "claude-seat-1", "salon Wi-Fi"),
    ("seat-2", "Seat 2 — Fern", "claude-seat-2", "salon Wi-Fi"),
    ("seat-3", "Seat 3 — Booth", "claude-seat-3", "salon Wi-Fi"),
]

SEED_BOARD = [
    (
        "Fix the PeriPage QR slip so it scans in low cafe light",
        "Harden contrast and quiet-zone on the check-in QR printed by the A6 304dpi. Ship a before/after dump.",
        60,
        40,
    ),
    (
        "Join guide that a first-time Android guest can follow on cafe Wi-Fi",
        "Rewrite the guest PWA so a phone on the same Wi-Fi as the seat PC can scan the slip and chat with Claude Code.",
        75,
        45,
    ),
    (
        "Wellness break chime that cannot be skipped from the seat",
        "After the session timer, lock unlock() until the host taps resume. Print the break on the slip.",
        90,
        50,
    ),
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
        sess_cols = self._columns("sessions")
        if "test_status" not in sess_cols:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN test_status TEXT")
        if "test_report" not in sess_cols:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN test_report TEXT")
        self.conn.commit()

    def _seed(self) -> None:
        n = self.conn.execute("SELECT COUNT(*) FROM seats").fetchone()[0]
        if n == 0:
            for sid, name, label, ssid in SEED_SEATS:
                self.conn.execute(
                    "INSERT INTO seats (id, name, claude_label, pan_ssid) VALUES (?,?,?,?)",
                    (sid, name, label, ssid),
                )
        n = self.conn.execute("SELECT COUNT(*) FROM board").fetchone()[0]
        if n == 0:
            now = time.time()
            for title, brief, wellness, brk in SEED_BOARD:
                self.conn.execute(
                    "INSERT INTO board (id, title, brief, wellness_minutes, break_after, published, created_at) "
                    "VALUES (?,?,?,?,?,1,?)",
                    (str(uuid.uuid4())[:8], title, brief, wellness, brk, now),
                )
        self.conn.commit()

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
            UPDATE seats SET status='idle'
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
        return item

    def _with_project(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        pid = item.get("project_id")
        item["project"] = self.project(pid) if pid else None
        return item

    def projects(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def project(self, project_id: str | None) -> dict[str, Any] | None:
        if not project_id:
            return None
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else None

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

    def board(self, published_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM board"
        if published_only:
            sql += " WHERE published=1"
        sql += " ORDER BY created_at DESC"
        return [self._with_project(dict(r)) for r in self.conn.execute(sql).fetchall()]  # type: ignore[misc]

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

    def claim(self, session_id: str, board_id: str) -> dict[str, Any]:
        item = self.board_item(board_id)
        if not item:
            raise KeyError("unknown board item")
        sess = self.session(session_id)
        if not sess:
            raise KeyError("unknown session")
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

    def log_print(self, kind: str, payload: dict[str, Any], dumped_path: str | None) -> str:
        job_id = str(uuid.uuid4())[:10]
        self.conn.execute(
            "INSERT INTO print_jobs (id, kind, payload, created_at, dumped_path) VALUES (?,?,?,?,?)",
            (job_id, kind, json.dumps(payload), time.time(), dumped_path),
        )
        self.conn.commit()
        return job_id
