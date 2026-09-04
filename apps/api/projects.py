"""Create or attach a git project that a board solution can sit on."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "apps" / "templates"
MANIFEST = "byoi.json"
DEFAULT_BRANCH = "main"

# What a detected project needs from the salon, keyed by evidence in the tree.
NEEDS_HINTS = {
    "postgres": ("DATABASE_URL", "POSTGRES_URL", "prisma", "drizzle", "pg", "postgres"),
    "redis": ("REDIS_URL", "KV_URL", "UPSTASH", "ioredis", "redis"),
    "auth": ("AUTH_SECRET", "NEXTAUTH_SECRET", "next-auth", "@auth/core", "clerk"),
}


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


#: Matches both `https://github.com/owner/repo(.git)` and `git@github.com:owner/repo(.git)`.
_GITHUB_REMOTE_RE = re.compile(
    r"github\.com[:/]+(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


def github_repo_slug(remote_url: str | None) -> str | None:
    """``owner/repo`` if this remote is a github.com repo, else None.

    The ``projects.github`` column just stores whatever ``origin`` points to —
    any git host, not necessarily GitHub — so this is the one place that
    decides whether a project's Solutions can be sourced from GitHub Issues.
    """
    match = _GITHUB_REMOTE_RE.search((remote_url or "").strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def is_github_project(project: dict[str, Any]) -> bool:
    return github_repo_slug(project.get("github")) is not None


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


def ensure_local(project: dict[str, object]) -> str:
    """Make the project's folder real. Clones from GitHub the first time.

    The default board ships with a repo nobody has cloned yet, so the folder
    appears when a guest claims the brief (or when the host taps Fetch), not
    when the desk starts up offline.
    """
    raw = str(project.get("local_path") or "").strip()
    if not raw:
        raise FileNotFoundError("this project has no folder")
    dest = Path(raw).expanduser()
    if dest.is_dir():
        return str(dest.resolve())
    url = str(project.get("github") or "").strip()
    if not url:
        raise FileNotFoundError(f"folder is gone and there is no repo to clone: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(["git", "clone", url, str(dest)], timeout=600)
    except FileNotFoundError as exc:
        raise RuntimeError("git is not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError((exc.stderr or exc.stdout or "git clone failed").strip()) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"cloning {url} timed out") from exc
    return str(dest.resolve())


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


# --------------------------------------------------------------------- templates


def templates() -> list[dict[str, Any]]:
    """Starters the desk can offer when creating a project."""
    found: list[dict[str, Any]] = []
    if not TEMPLATES.is_dir():
        return found
    for child in sorted(TEMPLATES.iterdir()):
        manifest = child / MANIFEST
        if not child.is_dir() or not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        spec_path = child / "SPEC.md"
        found.append(
            {
                "name": child.name,
                "framework": data.get("framework"),
                "needs": data.get("needs") or [],
                "spec": spec_path.read_text(encoding="utf-8") if spec_path.is_file() else "",
            }
        )
    return found


def from_template(
    *, template: str, name: str | None = None, private: bool = True, github: bool = False
) -> dict[str, str | None]:
    """Copy a starter into data/projects/ and make it a git repo."""
    src = TEMPLATES / (template or "")
    if not (src / MANIFEST).is_file():
        raise ValueError(f"unknown template: {template}")
    folder = slug(name or template)
    dest = projects_root() / folder
    if dest.exists():
        raise ValueError(f"folder already exists: {dest}")
    shutil.copytree(src, dest)
    try:
        # Pin the branch: a bare `git init` follows the operator's local default,
        # which is still `master` on most boxes and surprises everyone downstream.
        _run(["git", "init", "-q", "-b", DEFAULT_BRANCH, "."], cwd=dest)
        _run(["git", "add", "-A"], cwd=dest)
        _run(["git", "commit", "-qm", f"Start from the {template} template."], cwd=dest)
    except FileNotFoundError as exc:
        raise RuntimeError("git is not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError((exc.stderr or exc.stdout or "git init failed").strip()) from exc

    remote = None
    if github:
        argv = ["gh", "repo", "create", folder, "--source", str(dest), "--push"]
        argv.append("--private" if private else "--public")
        try:
            _run(argv, cwd=dest)
            remote = _git_remote(dest)
        except FileNotFoundError as exc:
            raise RuntimeError("GitHub CLI (gh) is not on PATH") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError((exc.stderr or exc.stdout or "gh repo create failed").strip()) from exc

    detected = detect(dest)
    return {
        "name": folder,
        "local_path": str(dest),
        "github": remote,
        "framework": detected.get("framework"),
        "template": template,
    }


# --------------------------------------------------------------------- detection


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def detect(path: str | Path) -> dict[str, Any]:
    """Work out how to build, run, and provision for an arbitrary repo.

    A `byoi.json` in the tree wins outright — that is the escape hatch for
    anything this heuristic gets wrong.
    """
    root = Path(path).expanduser().resolve()
    manifest = root / MANIFEST
    if manifest.is_file():
        try:
            data = json.loads(_read(manifest))
            if isinstance(data, dict):
                data.setdefault("needs", [])
                data.setdefault("deployable", bool(data.get("framework")))
                data["source"] = "manifest"
                return data
        except json.JSONDecodeError:
            pass

    pkg_path = root / "package.json"
    pkg: dict[str, Any] = {}
    if pkg_path.is_file():
        try:
            loaded = json.loads(_read(pkg_path))
            pkg = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            pkg = {}
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    scripts = pkg.get("scripts") or {}

    framework = None
    if "next" in deps or (root / "next.config.mjs").is_file() or (root / "next.config.js").is_file():
        framework = "nextjs"
    elif "nuxt" in deps:
        framework = "nuxt"
    elif "astro" in deps:
        framework = "astro"
    elif "vite" in deps:
        framework = "vite"
    elif "svelte" in deps or "@sveltejs/kit" in deps:
        framework = "sveltekit"
    elif pkg:
        framework = "node"
    elif (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
        framework = "python"

    # Evidence for what infrastructure to provision: dependencies plus whatever
    # the project's own env example asks for.
    haystack = " ".join(deps).lower()
    for name in (".env.example", ".env.sample", ".env.local.example"):
        haystack += " " + _read(root / name).lower()
    haystack += " " + " ".join(str(v) for v in scripts.values()).lower()
    needs = [kind for kind, hints in NEEDS_HINTS.items()
             if any(h.lower() in haystack for h in hints)]

    manager = "npm"
    if (root / "pnpm-lock.yaml").is_file():
        manager = "pnpm"
    elif (root / "yarn.lock").is_file():
        manager = "yarn"

    return {
        "framework": framework,
        "needs": needs,
        "install": f"{manager} install" if pkg else None,
        "build": f"{manager} run build" if "build" in scripts else None,
        "dev": f"{manager} run dev" if "dev" in scripts else None,
        "migrate": f"{manager} run db:init" if "db:init" in scripts else None,
        "health": "/api/health" if (root / "app" / "api" / "health").is_dir() else None,
        "deployable": bool(framework and framework != "python"),
        "source": "detected",
    }
