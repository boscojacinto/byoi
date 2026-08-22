"""Per-session Postgres + Redis + auth issuer, in Docker, on the seat.

This is the *development and test* data layer. The guest's Claude talks to it
through `.env.local`, and the acceptance suite runs against it with no network
and no cloud spend. Deploying swaps these URLs for managed ones on the host.

One compose project per session (`byoi-<session>`), ephemeral host ports so two
seats on one PC never collide, and `down -v` at checkout so nothing survives.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

ENV_FILE = ".env.local"
BYOI_BLOCK_START = "# --- byoi salon: local infrastructure (managed, do not edit) ---"
BYOI_BLOCK_END = "# --- end byoi salon ---"

POSTGRES_IMAGE = os.environ.get("BYOI_POSTGRES_IMAGE", "postgres:16-alpine")
REDIS_IMAGE = os.environ.get("BYOI_REDIS_IMAGE", "redis:7-alpine")

COMPOSE = """\
services:
  postgres:
    image: {postgres_image}
    environment:
      POSTGRES_USER: byoi
      POSTGRES_PASSWORD: {db_password}
      POSTGRES_DB: byoi
    ports:
      - "5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U byoi -d byoi"]
      interval: 2s
      timeout: 3s
      retries: 30
  redis:
    image: {redis_image}
    command: ["redis-server", "--requirepass", "{cache_password}"]
    ports:
      - "6379"
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "{cache_password}", "ping"]
      interval: 2s
      timeout: 3s
      retries: 30
"""


class InfraError(RuntimeError):
    """Precondition the desk should report, not a 500."""


def compose_project(session_id: str) -> str:
    sid = "".join(c for c in (session_id or "seat").lower() if c.isalnum() or c == "-")
    return f"byoi-{sid or 'seat'}"


def state_dir(session_id: str) -> Path:
    from .accounts import ROOT

    raw = os.environ.get("BYOI_INFRA_DIR", "").strip()
    base = Path(raw).expanduser() if raw else ROOT / "data" / "infra"
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


def _published_port(cwd: Path, project: str, service: str, container_port: int) -> int:
    proc = _compose("port", service, str(container_port), cwd=cwd, project=project, timeout=60)
    text = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not text:
        raise InfraError(f"could not read the published port for {service}")
    # "0.0.0.0:49154" — and IPv6 forms like "[::]:49154"
    return int(text[-1].rsplit(":", 1)[-1])


def env_for(session_id: str) -> dict[str, str] | None:
    path = state_dir(session_id) / "env.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def up(*, session_id: str, cwd: str | Path, timeout: int = 240) -> dict[str, str]:
    """Start the stack and write its URLs into the project's .env.local."""
    _docker_ok()
    project_dir = Path(cwd).expanduser().resolve()
    if not project_dir.is_dir():
        raise InfraError(f"not a directory: {project_dir}")
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
        ),
        encoding="utf-8",
    )
    proc = _compose("up", "-d", "--wait", cwd=dest, project=project, timeout=timeout)
    if proc.returncode != 0:
        raise InfraError((proc.stderr or proc.stdout or "docker compose up failed").strip()[-800:])

    db_port = _published_port(dest, project, "postgres", 5432)
    cache_port = _published_port(dest, project, "redis", 6379)
    env = {
        "DATABASE_URL": f"postgresql://byoi:{db_password}@127.0.0.1:{db_port}/byoi",
        "REDIS_URL": f"redis://default:{cache_password}@127.0.0.1:{cache_port}",
        "AUTH_SECRET": auth_secret,
        "AUTH_URL": "http://127.0.0.1:3000",
        "AUTH_TRUST_HOST": "true",
        "_db_password": db_password,
        "_cache_password": cache_password,
    }
    (dest / "env.json").write_text(json.dumps(env, indent=2), encoding="utf-8")
    write_env_file(project_dir, env)
    return env


def public_env(env: dict[str, str] | None) -> dict[str, str]:
    """Drop the bookkeeping keys; what actually belongs in .env.local."""
    return {k: v for k, v in (env or {}).items() if not k.startswith("_")}


def write_env_file(project_dir: Path, env: dict[str, str]) -> Path:
    """Replace only our managed block, so a guest's own vars survive."""
    path = Path(project_dir) / ENV_FILE
    body = "\n".join(f"{k}={v}" for k, v in public_env(env).items())
    block = f"{BYOI_BLOCK_START}\n{body}\n{BYOI_BLOCK_END}\n"
    if path.is_file():
        current = path.read_text(encoding="utf-8")
        if BYOI_BLOCK_START in current and BYOI_BLOCK_END in current:
            head = current.split(BYOI_BLOCK_START)[0]
            tail = current.split(BYOI_BLOCK_END, 1)[1].lstrip("\n")
            path.write_text(head + block + tail, encoding="utf-8")
            return path
        joiner = "" if current.endswith("\n") or not current else "\n"
        path.write_text(current + joiner + block, encoding="utf-8")
        return path
    path.write_text(block, encoding="utf-8")
    return path


def status(session_id: str) -> dict[str, Any]:
    project = compose_project(session_id)
    dest = state_dir(session_id)
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
    """Stop the stack and delete its volumes. Safe to call when nothing is up."""
    dest = state_dir(session_id)
    project = compose_project(session_id)
    if not (dest / "docker-compose.yml").is_file():
        return {"ok": True, "removed": False}
    if not shutil.which("docker"):
        return {"ok": False, "removed": False, "detail": "docker is not on PATH"}
    proc = _compose("down", "-v", "--remove-orphans", cwd=dest, project=project, timeout=timeout)
    ok = proc.returncode == 0
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
