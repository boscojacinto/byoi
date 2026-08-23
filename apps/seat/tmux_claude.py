"""Seat tmux session that holds the local Claude Code TTY."""

from __future__ import annotations

import os
import shutil
import subprocess

from apps.secrets import scrub

SESSION = os.environ.get("BYOI_TMUX", "claude-guest")
CLAUDE_BIN = os.environ.get("BYOI_CLAUDE", "claude")


def has_session() -> bool:
    if not shutil.which("tmux"):
        return False
    return subprocess.run(
        ["tmux", "has-session", "-t", SESSION],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def ensure_session() -> dict:
    """Create detached ``claude-guest`` if missing. Falls back to a shell."""
    if not shutil.which("tmux"):
        return {"tmux": None, "error": "tmux not installed", "cmd": None}
    if has_session():
        return {"tmux": SESSION, "created": False, "cmd": None}
    inner = shutil.which(CLAUDE_BIN) or shutil.which("bash") or "/bin/sh"
    # The session holds a shell an operator can type into; keep desk-only
    # deploy credentials out of it.
    subprocess.check_call(
        ["tmux", "new-session", "-d", "-s", SESSION, inner], env=scrub(os.environ.copy())
    )
    return {"tmux": SESSION, "created": True, "cmd": inner}


def attach_argv() -> list[str]:
    """Command to attach on the seat PTY. tmux if present, else claude/bash."""
    if shutil.which("tmux"):
        ensure_session()
        return ["tmux", "attach", "-t", SESSION]
    inner = shutil.which(CLAUDE_BIN) or shutil.which("bash") or "/bin/sh"
    return [inner]
