"""Desk-only deploy credentials.

Same contract as ``host_token``: an environment variable wins, otherwise a file
under ``data/secrets/``. The file form exists because the desk is usually a
long-running process — exporting a variable in some other shell cannot reach it,
and putting a token on a command line leaks it into shell history and ``ps``.

These are **desk** credentials. Nothing here should ever be readable from a seat
that runs guest code.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# env var -> filename under the secrets dir
SECRETS: dict[str, str] = {
    "BYOI_VERCEL_TOKEN": "vercel.token",
    "BYOI_VERCEL_SCOPE": "vercel.scope",
    "BYOI_NEON_API_KEY": "neon.token",
    "BYOI_UPSTASH_EMAIL": "upstash.email",
    "BYOI_UPSTASH_API_KEY": "upstash.token",
}


def secrets_dir() -> Path:
    raw = os.environ.get("BYOI_SECRETS_DIR", "").strip()
    return Path(raw).expanduser() if raw else ROOT / "data" / "secrets"


def secret_file(name: str) -> Path:
    override = os.environ.get(f"{name}_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return secrets_dir() / SECRETS.get(name, name.lower())


def is_world_readable(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IRGRP | stat.S_IROTH))


def read_secret(name: str) -> str | None:
    """Environment first, then the file. Returns None when neither is set."""
    env = os.environ.get(name, "").strip()
    if env:
        return env
    path = secret_file(name)
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    first = value[0].strip() if value else ""
    return first or None


def status() -> list[dict[str, object]]:
    """What the desk has configured — never the values themselves."""
    out: list[dict[str, object]] = []
    for name in SECRETS:
        path = secret_file(name)
        from_env = bool(os.environ.get(name, "").strip())
        out.append(
            {
                "name": name,
                "configured": bool(read_secret(name)),
                "source": "env" if from_env else ("file" if path.is_file() else None),
                "path": str(path) if not from_env else None,
                "world_readable": (not from_env) and is_world_readable(path),
            }
        )
    return out
