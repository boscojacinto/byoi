"""Operator login for the desk.

The desk used to trust anything arriving from ``127.0.0.1``. That worked when it
was only ever opened on the salon PC, and it fails open the moment a reverse
proxy is in front of it: every request then arrives from the proxy, so the
loopback test either locks the operator out or admits the whole internet.

So the desk gets a real credential. One shared operator password, scrypt-hashed
into ``data/secrets/operator.hash``, exchanged for a signed cookie. The host
token still works alongside it — that is how the seat, the print relay, and any
other machine caller authenticate, and none of them have a browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.secrets import secrets_dir

COOKIE_NAME = "byoi_desk"
# scrypt at these parameters is ~100ms on the desk VM. The salon has one
# operator password, so a slow hash is the only thing standing between a leaked
# hash file and the floor.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1


def _maxmem(n: int, r: int) -> int:
    """OpenSSL caps scrypt at 32 MiB unless told otherwise, and these
    parameters need exactly that much. Ask for headroom rather than quietly
    weakening the hash to fit."""
    return 128 * n * r * 2

SESSION_TTL = float(os.environ.get("BYOI_OPERATOR_TTL", 12 * 3600))
IDLE_TTL = float(os.environ.get("BYOI_OPERATOR_IDLE", 2 * 3600))
REFRESH_AFTER = 300.0

MAX_FAILURES = 8
LOCKOUT_S = 900.0


class OperatorError(RuntimeError):
    """A precondition the desk should report, not a 500."""


def hash_file() -> Path:
    raw = os.environ.get("BYOI_OPERATOR_HASH_FILE", "").strip()
    return Path(raw).expanduser() if raw else secrets_dir() / "operator.hash"


def key_file() -> Path:
    raw = os.environ.get("BYOI_SESSION_KEY_FILE", "").strip()
    return Path(raw).expanduser() if raw else secrets_dir() / "session.key"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# --- password ---------------------------------------------------------------


def format_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
        maxmem=_maxmem(SCRYPT_N, SCRYPT_R),
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def set_password(password: str) -> Path:
    if len(password) < 8:
        raise OperatorError("operator password must be at least 8 characters")
    path = hash_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(format_hash(password) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def password_is_set() -> bool:
    path = hash_file()
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def verify_password(password: str) -> bool:
    try:
        stored = hash_file().read_text(encoding="utf-8").strip()
    except OSError:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_unb64(digest)),
            maxmem=_maxmem(int(n), int(r)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, _unb64(digest))


# --- cookie -----------------------------------------------------------------

_key_lock = threading.Lock()


def session_key() -> bytes:
    """Signing key for desk cookies, minted on first use.

    Losing it only signs everyone out, so generating it here rather than making
    it another thing the operator has to set up is the right trade.
    """
    path = key_file()
    with _key_lock:
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        if not raw:
            raw = secrets.token_hex(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            path.write_text(raw + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
    return bytes.fromhex(raw)


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(session_key(), payload, hashlib.sha256).digest())


def issue_cookie(*, now: float | None = None) -> str:
    now = time.time() if now is None else now
    payload = json.dumps(
        {"sub": "operator", "iat": now, "exp": now + SESSION_TTL, "seen": now},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{_b64(payload)}.{_sign(payload)}"


@dataclass(frozen=True)
class Claims:
    issued_at: float
    expires_at: float
    seen_at: float

    def stale(self, *, now: float | None = None) -> bool:
        """True when the cookie is valid but old enough to be worth re-issuing."""
        now = time.time() if now is None else now
        return now - self.seen_at > REFRESH_AFTER


def read_cookie(value: str | None, *, now: float | None = None) -> Claims | None:
    if not value or "." not in value:
        return None
    now = time.time() if now is None else now
    body, _, signature = value.partition(".")
    try:
        payload = _unb64(body)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    try:
        data: dict[str, Any] = json.loads(payload)
    except ValueError:
        return None
    if data.get("sub") != "operator":
        return None
    try:
        expires = float(data["exp"])
        seen = float(data["seen"])
        issued = float(data["iat"])
    except (KeyError, TypeError, ValueError):
        return None
    if now >= expires:
        return None
    if now - seen > IDLE_TTL:
        return None
    return Claims(issued_at=issued, expires_at=expires, seen_at=seen)


def refresh_cookie(claims: Claims, *, now: float | None = None) -> str:
    """Slide ``seen`` forward without extending the absolute deadline."""
    now = time.time() if now is None else now
    payload = json.dumps(
        {"sub": "operator", "iat": claims.issued_at, "exp": claims.expires_at, "seen": now},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{_b64(payload)}.{_sign(payload)}"


def cookie_kwargs() -> dict[str, Any]:
    """Cookie flags. ``Secure`` is off only where the desk is served over HTTP."""
    secure = os.environ.get("BYOI_COOKIE_SECURE", "1") != "0"
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "secure": secure,
        "samesite": "lax",
        "path": "/",
        "max_age": int(SESSION_TTL),
    }


# --- throttle ---------------------------------------------------------------


class Throttle:
    """Per-address failure counter for the login route.

    The desk is on the public internet now, so the password needs the same
    treatment the seat's OTP already gets in ``apps/seat/gate.py``.
    """

    def __init__(self, max_failures: int = MAX_FAILURES, lockout_s: float = LOCKOUT_S) -> None:
        self.max_failures = max_failures
        self.lockout_s = lockout_s
        self._lock = threading.Lock()
        self._state: dict[str, tuple[int, float]] = {}

    def locked_for(self, who: str, *, now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            failures, until = self._state.get(who, (0, 0.0))
            if failures >= self.max_failures and until > now:
                return until - now
            return 0.0

    def record_failure(self, who: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            failures, until = self._state.get(who, (0, 0.0))
            if until <= now and failures >= self.max_failures:
                failures = 0
            failures += 1
            self._state[who] = (failures, now + self.lockout_s)

    def clear(self, who: str) -> None:
        with self._lock:
            self._state.pop(who, None)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


throttle = Throttle()
