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
    # Lets the venue's printer agent claim slips. Desk-only for the same
    # reason the rest are: the seat runs guest code.
    "BYOI_PRINT_RELAY_TOKEN": "print-relay.token",
}


def env_file() -> Path:
    raw = os.environ.get("BYOI_ENV_FILE", "").strip()
    return Path(raw).expanduser() if raw else ROOT / ".env"


def dotenv_values(path: Path | None = None) -> dict[str, str]:
    """Parse a .env well enough for credentials. Not a shell: no expansion."""
    target = path or env_file()
    out: dict[str, str] = {}
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


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


def _from_file(name: str) -> str | None:
    path = secret_file(name)
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    first = lines[0].strip() if lines else ""
    return first or None


def read_secret(name: str) -> str | None:
    """Environment, then data/secrets/, then .env. None when nothing is set.

    data/secrets/ beats .env deliberately: it is what `salon-secrets.sh` writes,
    so rotating a credential there is never silently overridden by a stale copy
    someone left in .env.
    """
    env = os.environ.get(name, "").strip()
    if env:
        return env
    managed = _from_file(name)
    if managed:
        return managed
    return dotenv_values().get(name, "").strip() or None


def source_of(name: str) -> str | None:
    if os.environ.get(name, "").strip():
        return "env"
    if _from_file(name):
        return "file"
    if dotenv_values().get(name, "").strip():
        return "dotenv"
    return None


def status() -> list[dict[str, object]]:
    """What the desk has configured — never the values themselves."""
    out: list[dict[str, object]] = []
    for name in SECRETS:
        path = secret_file(name)
        source = source_of(name)
        where = {"file": str(path), "dotenv": str(env_file())}.get(source or "")
        out.append(
            {
                "name": name,
                "configured": source is not None,
                "source": source,
                "path": where or None,
                "world_readable": source == "file" and is_world_readable(path),
            }
        )
    return out


def desk_only_names() -> tuple[str, ...]:
    """Credentials that must never reach a process that runs guest code."""
    return tuple(SECRETS)


def scrub(env: dict[str, str]) -> dict[str, str]:
    """Drop desk-only credentials from an environment about to be handed on."""
    for name in desk_only_names():
        env.pop(name, None)
    return env
