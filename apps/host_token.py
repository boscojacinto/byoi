"""Shared desk/seat host token. Prefer a file from ``scripts/salon-tls.sh``."""

from __future__ import annotations

import hmac
import os
from pathlib import Path

DEFAULT_HOST_TOKEN = "byoi-host"
ROOT = Path(__file__).resolve().parents[1]


def token_file() -> Path:
    override = os.environ.get("BYOI_HOST_TOKEN_FILE", "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get("BYOI_TLS_DIR", ROOT / "data" / "tls")) / "host.token"


def host_token() -> str:
    env = os.environ.get("BYOI_HOST_TOKEN", "").strip()
    if env:
        return env
    path = token_file()
    if path.is_file():
        return path.read_text(encoding="utf-8").splitlines()[0].strip()
    return DEFAULT_HOST_TOKEN


def allow_default_token() -> bool:
    if os.environ.get("BYOI_ALLOW_DEFAULT_TOKEN", "") == "1":
        return True
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def token_is_weak(token: str | None = None) -> bool:
    return (token or host_token()) == DEFAULT_HOST_TOKEN and not allow_default_token()


def token_matches(authorization: str | None) -> bool:
    expected = f"Bearer {host_token()}".encode()
    got = (authorization or "").encode()
    if len(got) != len(expected):
        return False
    return hmac.compare_digest(got, expected)
