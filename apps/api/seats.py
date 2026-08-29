"""Raise and destroy a seat container for one visit.

A seat used to be a PC in the room that stayed logged in between guests. In the
cloud it is a container that exists for exactly as long as the visit, which buys
two things the old model could not: a guest never inherits the previous guest's
process tree or /tmp, and an idle chair costs nothing.

Everything here runs on the **desk**, which is the only place with the Docker
socket. That is deliberate and load-bearing: the seat runs the guest's Claude,
which has Bash, so a Docker socket there would be root on this VM.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from apps.seat.accounts import AccountPool, accounts_dir
from apps.tls import TlsPaths, issue_seat_cert, paths as tls_paths

from . import caddy, seat_sync
from . import infra as desk_infra
from .testgen import host_account_label

log = logging.getLogger("uvicorn.error")

ROOT = Path(__file__).resolve().parents[2]
GUEST_PORT = 8787
CONTROL_PORT = 8788
# Where a seat container sees its own tree. The desk sees the same bytes under
# runtime_dir(session)/workspace.
GUEST_WORKSPACE = "/app/data/workspace"
READY_TIMEOUT = float(os.environ.get("BYOI_SEAT_READY_TIMEOUT", "120"))
READY_POLL_S = 1.0


class SeatError(RuntimeError):
    """A precondition the desk should report, not a 500."""


# --- naming -----------------------------------------------------------------


def container_name(session_id: str) -> str:
    sid = "".join(c for c in (session_id or "").lower() if c.isalnum() or c == "-")
    if not sid:
        raise SeatError("session id is not usable as a container name")
    return f"byoi-seat-{sid}"


def internal_url(session_id: str) -> str:
    return f"http://{container_name(session_id)}:{GUEST_PORT}"


def seat_image() -> str:
    return os.environ.get("BYOI_SEAT_IMAGE", "byoi-seat:latest")


def edge_network() -> str:
    return os.environ.get("BYOI_EDGE_NETWORK", "byoi-edge")


def control_network() -> str:
    return os.environ.get("BYOI_CONTROL_NETWORK", "byoi-ctl")


def max_seats() -> int:
    try:
        return max(1, int(os.environ.get("BYOI_MAX_SEATS", "4")))
    except ValueError:
        return 4


def runtime_dir(session_id: str) -> Path:
    raw = os.environ.get("BYOI_SEAT_RUNTIME_DIR", "").strip()
    base = Path(raw).expanduser() if raw else ROOT / "data" / "seat-runtime"
    dest = base / container_name(session_id)
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    return dest.resolve()


def workspace_dir(session_id: str) -> Path:
    """This visit's tree, as a directory on the VM rather than a named volume.

    A bind mount because the desk has to be able to *read* what the seat wrote.
    Grading fetches the submission ref straight out of here, which is what lets
    the cloud keep the one-PC path: no asking the guest's seat to push its work
    through the project's origin, and so no git credentials on the seat.

    It lives under runtime_dir(), so freeing the seat already removes it.
    """
    dest = runtime_dir(session_id) / "workspace"
    dest.mkdir(parents=True, exist_ok=True)
    return dest.resolve()


def seed_workspace(session_id: str, project_path: str | Path) -> str:
    """Put a board project inside this visit's workspace; return the seat's path.

    The project folder itself is never mounted into a seat. One guest would then
    be able to read and rewrite another's work — the same reason a visit only
    ever receives the Claude accounts allocated to it.
    """
    src = Path(project_path)
    if not src.is_dir():
        raise SeatError(f"the project folder is missing: {src}")
    dest = workspace_dir(session_id) / src.name
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    if (src / ".git").is_dir():
        if not shutil.which("git"):
            raise SeatError("git is not on PATH — the desk cannot seed a workspace")
        res = subprocess.run(
            ["git", "clone", str(src), str(dest)],
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("BYOI_SEED_TIMEOUT", "300")),
        )
        if res.returncode != 0:
            detail = (res.stderr or res.stdout).strip()[:400]
            raise SeatError(f"cloning {src.name} for the seat failed: {detail}")
        # The clone's origin would otherwise be a path that exists only in the
        # desk container, so a guest running `git push` would get a confusing
        # failure. Point it at whatever the project itself calls origin.
        upstream = subprocess.run(
            ["git", "-C", str(src), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=20.0,
        )
        url = upstream.stdout.strip() if upstream.returncode == 0 else ""
        subprocess.run(
            ["git", "-C", str(dest), "remote", "set-url", "origin", url] if url
            else ["git", "-C", str(dest), "remote", "remove", "origin"],
            capture_output=True,
            text=True,
            timeout=20.0,
        )
    else:
        # `local` projects can be any folder. Copy it so the guest still gets the
        # code; submission will report it is not a repo, exactly as it does today.
        shutil.copytree(src, dest, symlinks=True)

    return f"{GUEST_WORKSPACE}/{src.name}"


def workspace_source(session_id: str, project_path: str | None) -> Path | None:
    """Where the desk can read the tree the seat has been working in."""
    if not project_path:
        return None
    dest = workspace_dir(session_id) / Path(project_path).name
    return dest if (dest / ".git").is_dir() else None


def host_path(inside: Path) -> Path:
    """Translate a desk-container path to its path on the VM.

    Bind mounts are resolved by the Docker daemon on the host, so a path that is
    correct inside the desk container is wrong in `docker run` unless we say
    where /app/data actually lives.
    """
    root = os.environ.get("BYOI_HOST_DATA_DIR", "").strip()
    if not root:
        return inside
    data = Path(os.environ.get("BYOI_DATA", ROOT / "data")).resolve()
    try:
        return Path(root) / inside.resolve().relative_to(data)
    except ValueError:
        return inside


# --- docker -----------------------------------------------------------------


def _docker(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    if not shutil.which("docker"):
        raise SeatError("docker is not on PATH — the desk cannot raise a seat")
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _docker_ok(*args: str, timeout: float = 120.0, what: str = "docker") -> str:
    res = _docker(*args, timeout=timeout)
    if res.returncode != 0:
        raise SeatError(f"{what} failed: {(res.stderr or res.stdout).strip()[:400]}")
    return res.stdout.strip()


def container_exists(name: str) -> bool:
    res = _docker("inspect", "--format", "{{.Id}}", name, timeout=20.0)
    return res.returncode == 0


def live_seat_count() -> int:
    out = _docker("ps", "--filter", "name=^byoi-seat-", "--format", "{{.Names}}", timeout=20.0)
    return len([n for n in out.stdout.splitlines() if n.strip()])


# --- identity ---------------------------------------------------------------


def mint_identity(session_id: str) -> Path:
    """A control certificate and the host token, for this seat alone.

    Each seat gets its own key rather than a copy of a salon-wide one, so
    destroying the visit destroys the credential with it.
    """
    ca = tls_paths()
    if not ca.ca.is_file() or not ca.ca_key.is_file():
        raise SeatError("no salon CA — run scripts/salon-tls.sh")
    dest = runtime_dir(session_id) / "tls"
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    name = container_name(session_id)
    issue_seat_cert(ca, TlsPaths(dest), name=name, with_ips=False)
    if ca.token.is_file():
        token = dest / "host.token"
        token.write_bytes(ca.token.read_bytes())
        os.chmod(token, 0o600)
    return dest


def allocate_accounts(store: Any, session_id: str, *, want: int = 2) -> list[str]:
    """Claude accounts for this visit that no other live visit is using.

    The account the desk grades with is never one of them. It writes the
    acceptance suite blind, so handing it to the seat would mean the account
    that judges the work is the account that wrote it — and since every
    allocated account is bind-mounted into a container where the guest's Claude
    has Bash, it would hand over that credential too. With a small pool this is
    not a corner case: `claude-host` sorts ahead of `claude-seat-1`.
    """
    host = host_account_label()
    held = store.accounts_in_use(excluding=session_id)
    pool = [a.label for a in AccountPool().discover() if a.label != host]
    labels = [label for label in pool if label not in held]
    if not labels:
        # Two different problems, and the operator acts on them differently.
        if pool:
            raise SeatError(
                "every Claude account is already in use by a live seat — "
                "free a seat or add another account dir"
            )
        raise SeatError(
            f"no Claude account for a seat — {host!r} is reserved for grading; "
            "add another account dir"
        )
    return labels[:want]


# --- provisioning -----------------------------------------------------------


def _run_args(session_id: str, *, tls_dir: Path, labels: list[str], seat: dict[str, Any]) -> list[str]:
    name = container_name(session_id)
    accounts_root = accounts_dir()
    args = [
        "run", "-d",
        "--name", name,
        "--hostname", name,
        "--network", edge_network(),
        "--restart", "no",
        # A guest's own Claude token is written here, and tmpfs is the only
        # reason it never reaches the VM's disk.
        "--tmpfs", "/run/byoi:rw,noexec,nosuid,mode=0700,size=16m",
        "--memory", os.environ.get("BYOI_SEAT_MEMORY", "4g"),
        "--cpus", os.environ.get("BYOI_SEAT_CPUS", "2"),
        "--pids-limit", os.environ.get("BYOI_SEAT_PIDS", "1024"),
        "--security-opt", "no-new-privileges",
        "-v", f"{host_path(tls_dir)}:/app/data/tls:ro",
        "-v", f"{host_path(workspace_dir(session_id))}:{GUEST_WORKSPACE}",
        "-e", f"BYOI_SEAT_ID={seat.get('id', 'seat-1')}",
        "-e", f"BYOI_SEAT_NAME={seat.get('name', 'Seat')}",
        "-e", f"BYOI_SESSION_ID={session_id}",
        "-e", "BYOI_GUEST_NET=public",
        "-e", "BYOI_GUEST_TLS=0",
        "-e", "BYOI_TLS_DIR=/app/data/tls",
        "-e", "BYOI_HOST_TOKEN_FILE=/app/data/tls/host.token",
        "-e", "BYOI_GUEST_RUNTIME_DIR=/run/byoi",
        "-e", f"BYOI_SEAT_TLS_NAME={name}",
    ]
    if labels:
        args += ["-e", f"BYOI_CLAUDE_ACCOUNT={labels[0]}"]
    # Only the accounts this visit was given. Mounting the pool would let one
    # guest's session read the credentials another guest is sitting on.
    for label in labels:
        src = host_path(accounts_root / label)
        args += ["-v", f"{src}:/app/data/claude-accounts/{label}"]
    args.append(seat_image())
    return args


def provision(store: Any, session: dict[str, Any], seat: dict[str, Any]) -> dict[str, Any]:
    """Create the container, publish its hostname, and admit the OTP.

    Raises ``SeatError`` with something the operator can act on. The caller is
    responsible for freeing the seat — this does not decide that.
    """
    session_id = session["id"]
    seat_id = seat["id"]
    name = container_name(session_id)

    if live_seat_count() >= max_seats():
        raise SeatError(
            f"all {max_seats()} seats are up (BYOI_MAX_SEATS) — free one before checking in"
        )

    store.set_seat_runtime(seat_id, state="preparing", agent_url=internal_url(session_id))

    if container_exists(name):
        # A desk restart mid-visit can leave one behind. The visit is over
        # either way, so the container goes with it.
        _docker("rm", "-f", name, timeout=60.0)

    tls_dir = mint_identity(session_id)
    labels = allocate_accounts(store, session_id)
    store.set_session_accounts(session_id, labels)

    container_id = _docker_ok(
        *_run_args(session_id, tls_dir=tls_dir, labels=labels, seat=seat),
        what=f"starting {name}",
    )
    _docker_ok("network", "connect", control_network(), name, what="attaching the control network")

    seat_row = {**seat, "agent_url": internal_url(session_id)}
    wait_until_ready(seat_row)

    host = caddy.publish(session_id, f"{name}:{GUEST_PORT}")
    seat_sync.admit_session(seat_row, session)

    store.set_seat_runtime(
        seat_id,
        state="ready",
        agent_url=internal_url(session_id),
        container_id=container_id[:12],
        public_host=host,
    )
    log.info("BYOI: seat %s ready for session %s at %s", name, session_id, host)
    return {
        "container_id": container_id[:12],
        "public_host": host,
        "agent_url": internal_url(session_id),
        "accounts": labels,
    }


def wait_until_ready(seat: dict[str, Any], *, timeout: float | None = None) -> None:
    """Block until the seat answers on its mTLS control port.

    Answering there means the app is up *and* the certificate the desk minted is
    the one it loaded, so there is nothing left to be wrong at admit time.
    """
    deadline = time.time() + (timeout if timeout is not None else READY_TIMEOUT)
    last = "no response yet"
    while time.time() < deadline:
        try:
            seat_sync.seat_status(seat)
            return
        except seat_sync.SeatSyncError as exc:
            last = str(exc)
        time.sleep(READY_POLL_S)
    raise SeatError(f"seat did not come up in time: {last}")


# --- teardown ---------------------------------------------------------------


def teardown(store: Any, session: dict[str, Any], seat: dict[str, Any]) -> dict[str, Any]:
    """Destroy everything this visit created. Never raises.

    Freeing a seat must always succeed — an operator with a guest standing there
    cannot be blocked by a container that will not die. Each failure is recorded
    and reported instead.
    """
    session_id = session["id"]
    seat_id = seat.get("id", "")
    name = container_name(session_id)
    problems: list[str] = []

    try:
        # Revokes and unlinks the guest's own Claude token before the container
        # (and its tmpfs) goes away, so the refresh token does not outlive it.
        seat_sync.revoke_session({**seat, "agent_url": internal_url(session_id)})
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        problems.append(f"revoke: {exc}")

    for what, fn in (
        ("route", lambda: caddy.unpublish(session_id)),
        ("infra", lambda: desk_infra.down(session_id)),
        ("container", lambda: _docker("rm", "-f", name, timeout=60.0)),
        # Takes the guest's workspace with it: it is a directory in here, not a
        # named volume that would outlive the visit unless something removed it.
        ("identity", lambda: shutil.rmtree(runtime_dir(session_id), ignore_errors=True)),
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{what}: {exc}")

    if seat_id:
        try:
            store.clear_seat_runtime(seat_id)
            store.set_session_accounts(session_id, [])
        except Exception as exc:  # noqa: BLE001
            problems.append(f"store: {exc}")

    if problems:
        log.warning("BYOI: seat teardown for %s left problems: %s", session_id, "; ".join(problems))
    return {"ok": not problems, "problems": problems}


def reconcile(store: Any) -> dict[str, Any]:
    """Destroy containers and routes with no live session behind them.

    The desk can restart between a check-in and a checkout, and a seat left
    running would keep serving a guest the database thinks has gone home.
    """
    live = {s["id"] for s in store.live_sessions()}
    stray: list[str] = []
    out = _docker("ps", "-a", "--filter", "name=^byoi-seat-", "--format", "{{.Names}}", timeout=20.0)
    for line in out.stdout.splitlines():
        name = line.strip()
        if not name:
            continue
        session_id = name[len("byoi-seat-") :]
        if session_id not in live:
            stray.append(session_id)
            _docker("rm", "-f", name, timeout=60.0)
            shutil.rmtree(runtime_dir(session_id), ignore_errors=True)
    try:
        for session_id in caddy.published():
            if session_id not in live:
                caddy.unpublish(session_id)
                if session_id not in stray:
                    stray.append(session_id)
    except caddy.CaddyError as exc:
        log.warning("BYOI: could not reconcile edge routes: %s", exc)
    return {"removed": stray}
