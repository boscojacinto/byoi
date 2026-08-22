"""Capture the guest's tree as a git ref the host can fetch and grade.

The guest may be mid-edit with a dirty index, so nothing here may touch their
index, worktree, HEAD, or branches. Everything goes through a scratch index file
and lands on a namespaced ref under refs/byoi/ that no branch tracks.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

REF_PREFIXES = {
    "submission": "refs/byoi/submissions",
    "deploy": "refs/byoi/deploys",
}
REF_PREFIX = REF_PREFIXES["submission"]


class SubmissionError(RuntimeError):
    """Precondition the desk should report and fall back on, not a 500."""


def ref_for(session_id: str, kind: str = "submission") -> str:
    sid = (session_id or "").strip() or "seat"
    safe = "".join(c if (c.isalnum() or c in "-._") else "-" for c in sid)
    prefix = REF_PREFIXES.get(kind or "submission", REF_PREFIX)
    return f"{prefix}/{safe}"


def _git(*args: str, cwd: Path, env: dict[str, str] | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and proc.returncode != 0:
        raise SubmissionError((proc.stderr or proc.stdout or f"git {args[0]} failed").strip())
    return (proc.stdout or "").strip()


def _toplevel(cwd: Path) -> Path:
    try:
        top = _git("rev-parse", "--show-toplevel", cwd=cwd)
    except SubmissionError as exc:
        raise SubmissionError(f"not a git repository: {cwd}") from exc
    except FileNotFoundError as exc:
        raise SubmissionError("git is not on PATH") from exc
    if not top:
        raise SubmissionError(f"not a git repository: {cwd}")
    return Path(top)


def origin_url(cwd: Path) -> str | None:
    url = _git("remote", "get-url", "origin", cwd=cwd, check=False)
    return url or None


def capture(
    *, cwd: str | Path, session_id: str, push: bool = False, kind: str = "submission"
) -> dict[str, Any]:
    """Commit the working tree to refs/byoi/submissions/<session_id>.

    Returns the ref, its commit sha, the repo toplevel, and the origin URL when
    the ref was pushed. Raises SubmissionError on any precondition failure.
    """
    work = Path(cwd).expanduser().resolve()
    if not work.is_dir():
        raise SubmissionError(f"not a directory: {work}")
    top = _toplevel(work)
    ref = ref_for(session_id, kind)

    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        # Scratch index only — the guest's own index is never read or written.
        _git("add", "-A", cwd=top, env=env)
        tree = _git("write-tree", cwd=top, env=env)

    parent = _git("rev-parse", "--verify", "--quiet", "HEAD", cwd=top, check=False)
    args = ["commit-tree", tree]
    if parent:
        args += ["-p", parent]
    args += ["-m", f"byoi {kind} {session_id}"]
    commit = _git(*args, cwd=top)
    _git("update-ref", ref, commit, cwd=top)

    remote = origin_url(top)
    pushed = False
    if push:
        if not remote:
            raise SubmissionError("this project has no origin to push the submission to")
        _git("push", "--force", "origin", f"{ref}:{ref}", cwd=top)
        pushed = True

    return {
        "ref": ref,
        "commit": commit,
        "toplevel": str(top),
        "cwd": str(work),
        "remote": remote if pushed else None,
        "pushed": pushed,
    }
