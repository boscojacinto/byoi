"""Per-session Postgres and Redis, raised by the desk.

This used to run on the seat. It cannot any more: the seat is a container that
runs the guest's Claude, which has Bash, so giving it a Docker socket would give
guest code root on this VM. It is the same reasoning that already keeps the
Vercel token off the seat.

So the desk raises the stack, puts it on a network only that seat is attached
to, and hands the seat back three URLs over mTLS. The app still reads nothing
but ``DATABASE_URL``, ``REDIS_URL``, and ``AUTH_SECRET`` — the contract in
docs/salon.md is unchanged, only who runs the containers.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

from apps.seat.infra import (
    POSTGRES_IMAGE,
    REDIS_IMAGE,
    InfraError,
    compose_project,
    public_env,
)

ROOT = Path(__file__).resolve().parents[2]

# No published ports. Two seats never collide because they are on different
# networks, not because they were handed different ephemeral host ports — which
# also means the URLs are predictable instead of scraped back out of docker.
COMPOSE = """\
services:
  postgres:
    image: {postgres_image}
    container_name: {db_host}
    environment:
      POSTGRES_USER: byoi
      POSTGRES_PASSWORD: {db_password}
      POSTGRES_DB: byoi
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U byoi -d byoi"]
      interval: 2s
      timeout: 3s
      retries: 30
  redis:
    image: {redis_image}
    container_name: {cache_host}
    command: ["redis-server", "--requirepass", "{cache_password}"]
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "{cache_password}", "ping"]
      interval: 2s
      timeout: 3s
      retries: 30
networks:
  default:
    name: {network}
"""


def session_slug(session_id: str) -> str:
    return compose_project(session_id)[len("byoi-") :]


def network_name(session_id: str) -> str:
    return f"byoi-infra-{session_slug(session_id)}"


def db_host(session_id: str) -> str:
    return f"byoi-pg-{session_slug(session_id)}"


def cache_host(session_id: str) -> str:
    return f"byoi-redis-{session_slug(session_id)}"


def state_dir(session_id: str) -> Path:
    raw = os.environ.get("BYOI_DESK_INFRA_DIR", "").strip()
    base = Path(raw).expanduser() if raw else Path(os.environ.get("BYOI_DATA", ROOT / "data")) / "infra"
    dest = base / compose_project(session_id)
    dest.mkdir(parents=True, exist_ok=True)
    return dest.resolve()


def _docker_ok() -> None:
    if not shutil.which("docker"):
        raise InfraError("docker is not on PATH")
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise InfraError("docker compose plugin is unavailable")


def _compose(*args: str, cwd: Path, project: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", project, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def env_for(session_id: str) -> dict[str, str] | None:
    path = state_dir(session_id) / "env.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def attach(session_id: str, container: str) -> None:
    """Put the seat on this session's data network, and nothing else on it."""
    res = subprocess.run(
        ["docker", "network", "connect", network_name(session_id), container],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if res.returncode != 0 and "already exists" not in (res.stderr or ""):
        raise InfraError(f"could not attach {container} to its data network: {(res.stderr or '').strip()[:240]}")


def up(*, session_id: str, container: str | None = None, timeout: int = 240) -> dict[str, str]:
    """Start the stack for this session and return the URLs the app reads."""
    _docker_ok()
    dest = state_dir(session_id)
    project = compose_project(session_id)

    existing = env_for(session_id)
    db_password = (existing or {}).get("_db_password") or secrets.token_urlsafe(18)
    cache_password = (existing or {}).get("_cache_password") or secrets.token_urlsafe(18)
    auth_secret = (existing or {}).get("AUTH_SECRET") or secrets.token_urlsafe(32)

    (dest / "docker-compose.yml").write_text(
        COMPOSE.format(
            postgres_image=POSTGRES_IMAGE,
            redis_image=REDIS_IMAGE,
            db_password=db_password,
            cache_password=cache_password,
            db_host=db_host(session_id),
            cache_host=cache_host(session_id),
            network=network_name(session_id),
        ),
        encoding="utf-8",
    )
    proc = _compose("up", "-d", "--wait", cwd=dest, project=project, timeout=timeout)
    if proc.returncode != 0:
        raise InfraError((proc.stderr or proc.stdout or "docker compose up failed").strip()[-800:])

    if container:
        attach(session_id, container)

    env = {
        "DATABASE_URL": f"postgresql://byoi:{db_password}@{db_host(session_id)}:5432/byoi",
        "REDIS_URL": f"redis://default:{cache_password}@{cache_host(session_id)}:6379",
        "AUTH_SECRET": auth_secret,
        # The guest's dev server runs inside the seat container, so its own
        # loopback is the right origin.
        "AUTH_URL": "http://127.0.0.1:3000",
        "AUTH_TRUST_HOST": "true",
        "_db_password": db_password,
        "_cache_password": cache_password,
    }
    (dest / "env.json").write_text(json.dumps(env, indent=2), encoding="utf-8")
    os.chmod(dest / "env.json", 0o600)
    return env


def status(session_id: str) -> dict[str, Any]:
    dest = state_dir(session_id)
    project = compose_project(session_id)
    if not (dest / "docker-compose.yml").is_file():
        return {"up": False, "services": [], "env": {}}
    proc = _compose("ps", "--format", "json", cwd=dest, project=project, timeout=60)
    services: list[dict[str, Any]] = []
    for line in (proc.stdout or "").strip().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, list):
            services.extend(r for r in row if isinstance(r, dict))
        elif isinstance(row, dict):
            services.append(row)
    running = [s for s in services if str(s.get("State", "")).lower() == "running"]
    return {
        "up": bool(running),
        "services": [
            {"name": s.get("Service") or s.get("Name"), "state": s.get("State"), "health": s.get("Health")}
            for s in services
        ],
        "env": public_env(env_for(session_id)),
    }


def down(session_id: str, *, timeout: int = 120) -> dict[str, Any]:
    """Stop the stack, delete its volumes, and remove its network."""
    dest = state_dir(session_id)
    project = compose_project(session_id)
    if not (dest / "docker-compose.yml").is_file():
        return {"ok": True, "removed": False}
    if not shutil.which("docker"):
        return {"ok": False, "removed": False, "detail": "docker is not on PATH"}
    proc = _compose("down", "-v", "--remove-orphans", cwd=dest, project=project, timeout=timeout)
    ok = proc.returncode == 0
    # `down` removes the network only once nothing is attached; the seat may
    # still be, and its own teardown does not know about this network.
    subprocess.run(
        ["docker", "network", "rm", network_name(session_id)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    for name in ("docker-compose.yml", "env.json"):
        try:
            (dest / name).unlink()
        except OSError:
            pass
    return {
        "ok": ok,
        "removed": True,
        "detail": None if ok else (proc.stderr or proc.stdout or "").strip()[-400:],
    }
