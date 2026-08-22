"""Seat-side OTP gate. Host admits an OTP; guests present it to open chat."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Any


def normalize_otp(otp: str | None) -> str:
    return (otp or "").strip().casefold()


def hash_otp(otp: str) -> str:
    return hashlib.sha256(normalize_otp(otp).encode("utf-8")).hexdigest()


class Gate:
    """In-memory admit state for one seat process."""

    def __init__(self, max_failures: int = 8) -> None:
        self.max_failures = max_failures
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._otp_hash: str | None = None
            self._session_id: str | None = None
            self._coder_name: str | None = None
            self._seat_id: str | None = None
            self._admitted_at: float | None = None
            self._ticket: str | None = None
            self._failures = 0
            self._locked = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "admitted": bool(self._otp_hash) and not self._locked,
                "locked": self._locked,
                "session_id": self._session_id,
                "coder_name": self._coder_name,
                "failures": self._failures,
            }

    def admit(self, *, otp: str, session_id: str, coder_name: str | None, seat_id: str | None) -> dict[str, Any]:
        otp_n = normalize_otp(otp)
        if len(otp_n) < 6:
            raise ValueError("otp too short")
        with self._lock:
            self._otp_hash = hash_otp(otp_n)
            self._session_id = session_id
            self._coder_name = coder_name
            self._seat_id = seat_id
            self._admitted_at = time.time()
            self._ticket = None
            self._failures = 0
            self._locked = False
        return {"ok": True, "session_id": session_id}

    def revoke(self) -> None:
        self.reset()

    def unlock(self, otp: str | None, *, open_gate: bool = False) -> str:
        """Validate OTP and return a TTY ticket. ``open_gate`` is a explicit dev bypass."""
        if open_gate and not normalize_otp(otp):
            return self._issue_ticket()
        presented = normalize_otp(otp)
        if not presented:
            raise PermissionError("otp required")
        with self._lock:
            if self._locked or not self._otp_hash:
                raise PermissionError("no live OTP on this seat — host must check you in")
            if hmac.compare_digest(self._otp_hash, hash_otp(presented)):
                self._failures = 0
                ticket = secrets.token_urlsafe(24)
                self._ticket = ticket
                return ticket
            self._failures += 1
            if self._failures >= self.max_failures:
                self._locked = True
                self._otp_hash = None
                self._ticket = None
                raise PermissionError("too many OTP attempts — host must check in again")
            raise PermissionError("invalid otp")

    def check_ticket(self, ticket: str | None) -> bool:
        presented = (ticket or "").strip()
        if not presented:
            return False
        with self._lock:
            if not self._ticket:
                return False
            return hmac.compare_digest(self._ticket, presented)

    def _issue_ticket(self) -> str:
        with self._lock:
            ticket = secrets.token_urlsafe(24)
            self._ticket = ticket
            return ticket


gate = Gate()
