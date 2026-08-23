"""A guest's own Claude account: ephemeral, RAM-only, revoked when the seat is freed.

A guest who already pays for Claude can run the session on their own account
instead of the salon's. The seat never holds a durable credential of theirs:

* the OAuth flow is **relayed to the guest's phone**, so their password and 2FA
  are typed into claude.ai on their own device, never on this machine;
* it lives on **tmpfs**, so it never reaches stable storage;
* freeing the seat **revokes it server-side** before unlinking the directory —
  deleting ``.credentials.json`` alone would leave a live refresh token valid
  until ``refreshTokenExpiresAt``.

Two things this does **not** buy, both of which the guest is told before they
sign in:

**The token is not narrowed.** ``claude auth login`` requests a fixed scope set —
``org:create_api_key user:profile user:inference user:sessions:claude_code
user:mcp_servers user:file_upload`` — and ignores ``CLAUDE_CODE_OAUTH_SCOPES``
entirely; a nonsense value produces the same URL. So the token on this seat can
do what Claude Code can normally do with their account, **including creating an
API key on their org**. What protects the guest is that it is short-lived and
revoked at checkout, not that it is weak. Never claim otherwise in guest-facing
copy: report ``granted_scopes()``, which reads what was actually issued.

**It is not private from the operator while the session runs.** Claude Code
executes on this machine, so root here can read the live token.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import shutil
import struct
import termios
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from apps.secrets import scrub

from .accounts import Account, handoffs_dir

ROOT = Path(__file__).resolve().parents[2]

GUEST_LABEL = "guest"
# Passed through as CLAUDE_CODE_OAUTH_SCOPES, which `claude auth login` (2.1.241)
# ignores — verified: setting it to a nonsense value produces an identical
# authorize URL. Kept because other code paths honour it and a future CLI may,
# but nothing may promise the guest a narrowed token on the strength of it.
DEFAULT_SCOPES = "user:inference"
# Scopes worth naming to a guest before they approve the sign-in.
NOTABLE_SCOPES = {
    "org:create_api_key": "create an API key on your account",
    "user:profile": "read your profile",
    "user:file_upload": "upload files",
    "user:mcp_servers": "reach your MCP servers",
}
# An abandoned sign-in must not leave a pty holding a tmpfs directory open. Ten
# minutes, not five: a guest has to open the link, sign in, clear 2FA and copy a
# code back, on a phone, in a cafe. Five was measured to be too tight.
LOGIN_TIMEOUT = 600.0
URL_WAIT = 45.0
CODE_WAIT = 90.0
LOGOUT_TIMEOUT = 30.0

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_URL = re.compile(r"https://[^\s\"'<>\x00-\x1f]+")


def claude_bin() -> str:
    binary = os.environ.get("BYOI_CLAUDE", "claude")
    return shutil.which(binary) or binary


def login_timeout() -> float:
    try:
        return float(os.environ.get("BYOI_GUEST_LOGIN_TIMEOUT", "").strip() or LOGIN_TIMEOUT)
    except ValueError:
        return LOGIN_TIMEOUT


def oauth_scopes() -> str:
    raw = os.environ.get("BYOI_GUEST_OAUTH_SCOPES", "").strip()
    return raw or DEFAULT_SCOPES


def _runtime_root() -> Path | None:
    raw = os.environ.get("BYOI_GUEST_RUNTIME_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg:
        return Path(xdg)
    candidate = Path(f"/run/user/{os.getuid()}")
    return candidate if candidate.is_dir() else None


def guest_root() -> Path:
    """Where ephemeral guest accounts live — tmpfs when we can get it.

    Deliberately **not** under ``accounts_dir()``: ``AccountPool.discover()``
    scans that tree, and a guest directory appearing there could be handed to
    the next guest by ``pick()``.
    """
    runtime = _runtime_root()
    if runtime is not None and runtime.is_dir() and os.access(runtime, os.W_OK):
        return (runtime / "byoi").resolve()
    return (ROOT / "data" / "guest-accounts").resolve()


def guest_dir(session_id: str | None) -> Path:
    """Path for a session's account. Does not create it — see ``ensure_guest_dir``."""
    sid = (str(session_id or "seat").strip() or "seat").replace("/", "_")
    return guest_root() / f"guest-{sid}"


def ensure_guest_dir(session_id: str | None) -> Path:
    path = guest_dir(session_id)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o700)
    return path


def guest_account(session_id: str | None) -> Account:
    return Account(label=GUEST_LABEL, config_dir=ensure_guest_dir(session_id))


def fstype(path: Path) -> str:
    """Filesystem backing ``path``, via /proc/mounts. '' when it cannot be told."""
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    target = str(path.resolve())
    best_mount = ""
    best_type = ""
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = parts[1].replace("\\040", " ")
        if target == mount or target.startswith(mount.rstrip("/") + "/"):
            if len(mount) >= len(best_mount):
                best_mount, best_type = mount, parts[2]
    return best_type


def on_tmpfs(path: Path | None = None) -> bool:
    return fstype(path or guest_root()) == "tmpfs"


def swap_devices() -> list[str]:
    """Active swap devices. tmpfs pages can land here, weakening the RAM-only claim."""
    try:
        lines = Path("/proc/swaps").read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return []
    return [parts[0] for parts in (line.split() for line in lines) if parts]


def storage_warnings() -> list[str]:
    """Honest notes about how well the RAM-only promise holds on this machine."""
    notes: list[str] = []
    root = guest_root()
    if not on_tmpfs(root):
        notes.append(
            f"guest credentials fall back to {root} on {fstype(root) or 'disk'} — "
            "no tmpfs runtime directory is available, so teardown overwrites and "
            "unlinks instead, which a journaling filesystem may not honour"
        )
    swaps = swap_devices()
    if swaps and on_tmpfs(root):
        notes.append(
            "swap is active on "
            + ", ".join(swaps)
            + " — tmpfs pages can be written there, so the RAM-only guarantee "
            "holds only if that swap is encrypted"
        )
    return notes


def requested_scopes(auth_url: str | None) -> list[str]:
    """What the sign-in URL actually asks for — not what we asked it to ask for."""
    if not auth_url:
        return []
    query = urlparse(auth_url).query
    return parse_qs(query).get("scope", [""])[0].split()


def granted_scopes(config_dir: Path | None) -> list[str]:
    """What the issued token actually carries, straight from .credentials.json."""
    if config_dir is None:
        return []
    try:
        data = json.loads((Path(config_dir) / ".credentials.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    scopes = oauth.get("scopes") if isinstance(oauth, dict) else None
    if isinstance(scopes, str):
        return scopes.split()
    return [str(s) for s in scopes] if isinstance(scopes, list) else []


def scope_powers(scopes: list[str]) -> list[str]:
    """Plain-language list of what this token lets the seat do with the account."""
    return [NOTABLE_SCOPES[s] for s in scopes if s in NOTABLE_SCOPES]


def credentials_ready(config_dir: Path | None) -> bool:
    """True once a usable OAuth token is on disk.

    Also the post-logout check: logout blanks accessToken in place rather than
    removing the file.
    """
    if config_dir is None:
        return False
    try:
        data = json.loads((Path(config_dir) / ".credentials.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return False
    return bool(str(oauth.get("accessToken") or "").strip())


def login_env(config_dir: Path) -> dict[str, str]:
    env = scrub(os.environ.copy())
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CLAUDE_CODE_OAUTH_SCOPES"] = oauth_scopes()
    # Force the printed-URL path. A browser on the seat's own screen is the wrong
    # device — the guest is meant to authenticate on their phone — and a localhost
    # callback would complete the flow here without them.
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"):
        env.pop(name, None)
    return env


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    packed = struct.pack("HHHH", max(1, rows), max(2, cols), 0, 0)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
    except OSError:
        pass


@dataclass
class GuestLogin:
    session_id: str
    config_dir: Path
    pid: int
    fd: int
    started_at: float
    buffer: str = ""
    auth_url: str | None = None
    closed: bool = False


_LOGINS: dict[str, GuestLogin] = {}


def _read_available(login: GuestLogin) -> bool:
    """Drain the pty into the buffer. False once the child's side is gone."""
    while True:
        try:
            chunk = os.read(login.fd, 4096)
        except BlockingIOError:
            return True
        except OSError:
            return False
        if not chunk:
            return False
        text = _ANSI.sub("", chunk.decode("utf-8", "replace"))
        login.buffer = (login.buffer + text)[-65536:]


def _find_url(buffer: str) -> str | None:
    for match in _URL.finditer(buffer):
        url = match.group(0).rstrip(".,)]}'\"")
        if "oauth" in url or "authorize" in url:
            return url
    return None


def _close(login: GuestLogin) -> None:
    if login.closed:
        return
    login.closed = True
    try:
        os.close(login.fd)
    except OSError:
        pass
    try:
        os.kill(login.pid, 15)
    except OSError:
        pass
    try:
        os.waitpid(login.pid, os.WNOHANG)
    except OSError:
        pass


async def begin_login(session_id: str | None) -> dict[str, Any]:
    """Start `claude auth login` in a pty and return the URL for the guest's phone."""
    sid = str(session_id or "seat")
    await abort_login(sid)
    config_dir = ensure_guest_dir(sid)
    env = login_env(config_dir)
    argv = [claude_bin(), "auth", "login"]

    # pty.fork() rather than openpty() + create_subprocess_exec: the CLI needs a
    # controlling terminal, not just an isatty() stdin, and pty.fork does the
    # setsid/TIOCSCTTY dance for us. The seat server is threaded, so the child
    # does nothing but exec — argv and env are both built above, before the fork.
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child never returns
        try:
            os.execvpe(argv[0], argv, env)
        finally:
            os._exit(1)

    # Wide, so a long OAuth URL is never wrapped across lines in the pty.
    _set_winsize(fd, 50, 1000)
    os.set_blocking(fd, False)
    login = GuestLogin(
        session_id=sid, config_dir=config_dir, pid=pid, fd=fd, started_at=time.time()
    )
    _LOGINS[sid] = login

    deadline = time.time() + URL_WAIT
    while time.time() < deadline:
        alive = _read_available(login)
        url = _find_url(login.buffer)
        if url:
            login.auth_url = url
            scopes = requested_scopes(url)
            return {
                "auth_url": url,
                "scopes": scopes,
                "powers": scope_powers(scopes),
                "session_id": sid,
            }
        if credentials_ready(config_dir):
            _close(login)
            _LOGINS.pop(sid, None)
            return {"auth_url": None, "done": True, "session_id": sid}
        if not alive:
            break
        await asyncio.sleep(0.1)

    tail = login.buffer.strip().splitlines()[-3:]
    _close(login)
    _LOGINS.pop(sid, None)
    raise RuntimeError(
        "could not start a Claude sign-in on this seat"
        + (f": {' '.join(tail)}" if tail else "")
    )


async def submit_code(session_id: str | None, code: str) -> dict[str, Any]:
    """Feed the guest's authorization code to the waiting login."""
    sid = str(session_id or "seat")
    login = _LOGINS.get(sid)
    if login is None:
        raise LookupError("no sign-in is waiting for a code")
    value = (code or "").strip()
    if not value:
        raise ValueError("no code given")

    try:
        os.write(login.fd, (value + "\n").encode())
    except OSError as exc:
        _close(login)
        _LOGINS.pop(sid, None)
        raise RuntimeError("the sign-in on this seat has already ended") from exc

    deadline = time.time() + CODE_WAIT
    while time.time() < deadline:
        alive = _read_available(login)
        if credentials_ready(login.config_dir):
            _close(login)
            _LOGINS.pop(sid, None)
            scopes = granted_scopes(login.config_dir)
            return {
                "ok": True,
                "session_id": sid,
                "email": await account_email(login.config_dir),
                "scopes": scopes,
                "powers": scope_powers(scopes),
            }
        if not alive:
            break
        await asyncio.sleep(0.1)

    tail = login.buffer.strip().splitlines()[-3:]
    _close(login)
    _LOGINS.pop(sid, None)
    raise RuntimeError("that code was not accepted" + (f": {' '.join(tail)}" if tail else ""))


async def account_email(config_dir: Path) -> str | None:
    """The signed-in address, for the guest to confirm it is their own account.

    Returns None when the narrowed scope does not carry profile information —
    that is expected, not an error.
    """
    env = scrub(os.environ.copy())
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin(),
            "auth",
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=LOGOUT_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    email = str((data or {}).get("email") or "").strip()
    return email or None


async def abort_login(session_id: str | None) -> None:
    """Kill an in-flight sign-in. Leaves the directory alone."""
    login = _LOGINS.pop(str(session_id or "seat"), None)
    if login is not None:
        _close(login)


async def _logout(config_dir: Path) -> bool:
    env = scrub(os.environ.copy())
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin(),
            "auth",
            "logout",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        await asyncio.wait_for(proc.wait(), timeout=LOGOUT_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return False
    return not credentials_ready(config_dir)


def _wipe(path: Path) -> None:
    """Remove the directory. Off tmpfs, overwrite first — best effort only.

    On a journaling filesystem an in-place overwrite is not a guarantee, which is
    exactly why the tmpfs location is the supported one.
    """
    if not path.exists():
        return
    if not on_tmpfs(path):
        for child in path.rglob("*"):
            if child.is_file() and not child.is_symlink():
                try:
                    size = child.stat().st_size
                    with child.open("r+b", buffering=0) as fh:
                        fh.write(b"\0" * size)
                        fh.flush()
                        os.fsync(fh.fileno())
                except OSError:
                    pass
    shutil.rmtree(path, ignore_errors=True)


def wipe_handoff(session_id: str | None) -> bool:
    """Drop the seat's on-disk compact summary for a session."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    path = handoffs_dir() / f"{sid}.md"
    try:
        path.unlink()
    except OSError:
        return False
    return True


async def teardown(session_id: str | None) -> dict[str, Any]:
    """Revoke, then erase. Never raises — freeing a seat must not fail on this."""
    sid = str(session_id or "seat")
    result: dict[str, Any] = {"revoked": False, "removed": False, "handoff": False}
    try:
        await abort_login(sid)
        config_dir = guest_dir(sid)
        if credentials_ready(config_dir):
            # Order matters: unlinking first would leave a live refresh token.
            result["revoked"] = await _logout(config_dir)
        existed = config_dir.exists()
        _wipe(config_dir)
        result["removed"] = existed and not config_dir.exists()
        result["handoff"] = wipe_handoff(sid)
    except Exception as exc:  # noqa: BLE001 - reported, never raised on
        result["error"] = str(exc)
    return result


def teardown_sync(session_id: str | None) -> dict[str, Any]:
    """``teardown`` for the synchronous control routes. Never raises.

    ``/local/revoke`` is a sync endpoint — FastAPI runs it in a worker thread, so
    there is normally no loop here and ``asyncio.run`` is right. The threaded
    branch covers a caller that does hold one.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(teardown(session_id))

    result: dict[str, Any] = {}

    def runner() -> None:
        result.update(asyncio.run(teardown(session_id)))

    worker = threading.Thread(target=runner, daemon=True)
    worker.start()
    worker.join(timeout=LOGOUT_TIMEOUT * 2)
    return result or {"revoked": False, "removed": False, "error": "teardown timed out"}


def pending(session_id: str | None) -> bool:
    return str(session_id or "seat") in _LOGINS


def sweep(now: float | None = None) -> list[str]:
    """Kill sign-ins the guest walked away from. Returns the sessions dropped."""
    cutoff = (now if now is not None else time.time()) - login_timeout()
    stale = [sid for sid, login in _LOGINS.items() if login.started_at < cutoff]
    for sid in stale:
        login = _LOGINS.pop(sid, None)
        if login is not None:
            _close(login)
    return stale
