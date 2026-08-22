"""Create or attach a git project that a board solution can sit on."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def projects_root() -> Path:
    raw = os.environ.get("BYOI_PROJECTS_DIR", "").strip()
    path = Path(raw).expanduser() if raw else ROOT / "data" / "projects"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def slug(name: str) -> str:
    base = (name or "").strip().rstrip("/").split("/")[-1]
    base = re.sub(r"\.git$", "", base, flags=re.I)
    out = re.sub(r"[^a-zA-Z0-9._-]+", "-", base).strip("-_.")
    return out.lower() or "project"


def _run(argv: list[str], *, cwd: Path | None = None, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_remote(dest: Path) -> str | None:
    try:
        return _run(["git", "remote", "get-url", "origin"], cwd=dest).stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def use_local(path: str, name: str | None = None) -> dict[str, str | None]:
    dest = Path(path).expanduser().resolve()
    if not dest.is_dir():
        raise FileNotFoundError(f"not a directory: {dest}")
    return {"name": name or dest.name, "local_path": str(dest), "github": _git_remote(dest)}


def clone_url(url: str, name: str | None = None) -> dict[str, str | None]:
    src = (url or "").strip()
    if not src or " " in src:
        raise ValueError("clone URL required")
    folder = slug(name or src)
    dest = projects_root() / folder
    if dest.exists():
        raise ValueError(f"folder already exists: {dest}")
    try:
        _run(["git", "clone", src, str(dest)])
    except FileNotFoundError as exc:
        raise RuntimeError("git is not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError((exc.stderr or exc.stdout or "git clone failed").strip()) from exc
    return {"name": folder, "local_path": str(dest), "github": src}


def create_github(*, name: str, description: str = "", private: bool = True) -> dict[str, str | None]:
    repo = (name or "").strip().lstrip("@")
    folder = slug(repo)
    dest = projects_root() / folder
    if dest.exists():
        raise ValueError(f"folder already exists: {dest}")
    argv = ["gh", "repo", "create", repo, "--clone", "--description", description or repo]
    argv.append("--private" if private else "--public")
    try:
        _run(argv, cwd=projects_root())
    except FileNotFoundError as exc:
        raise RuntimeError("GitHub CLI (gh) is not on PATH — install it and run gh auth login") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError((exc.stderr or exc.stdout or "gh repo create failed").strip()) from exc
    if not dest.is_dir():
        raise RuntimeError(f"gh did not clone into {dest}")
    return {"name": folder, "local_path": str(dest), "github": _git_remote(dest)}
