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
CREATE TABLE IF NOT EXISTS board (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    brief TEXT NOT NULL,
    wellness_minutes INTEGER NOT NULL DEFAULT 90,
    break_after INTEGER NOT NULL DEFAULT 50,
    published INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
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
    FOREIGN KEY (seat_id) REFERENCES seats(id)
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
        "Rewrite the coder PWA so a phone on the same Wi-Fi as the seat PC can scan the slip and attach the TTY.",
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
        self._seed()
        self._reconcile_occupancy()

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
        return dict(row) if row else None

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

    def board(self, published_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM board"
        if published_only:
            sql += " WHERE published=1"
        sql += " ORDER BY created_at DESC"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def board_item(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM board WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def add_board(self, title: str, brief: str, wellness_minutes: int, break_after: int) -> dict[str, Any]:
        item = {
            "id": str(uuid.uuid4())[:8],
            "title": title,
            "brief": brief,
            "wellness_minutes": wellness_minutes,
            "break_after": break_after,
            "published": 1,
            "created_at": time.time(),
        }
        self.conn.execute(
            "INSERT INTO board (id, title, brief, wellness_minutes, break_after, published, created_at) "
            "VALUES (:id,:title,:brief,:wellness_minutes,:break_after,:published,:created_at)",
            item,
        )
        self.conn.commit()
        return item

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
        return dict(row) if row else None

    def session_by_otp(self, otp: str) -> dict[str, Any] | None:
        needle = (otp or "").strip().casefold()
        if not needle:
            return None
        row = self.conn.execute("SELECT * FROM sessions WHERE lower(unlock_otp)=?", (needle,)).fetchone()
        return dict(row) if row else None

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
