"""Pull a project's open GitHub issues, for syncing onto the solution board.

Shells out to the `gh` CLI rather than calling the REST API directly, matching
the existing `gh repo create` pattern in projects.py — it reuses whatever auth
the host already set up with `gh auth login`, with no new dependency.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

_FIELDS = "number,title,body,url,updatedAt"


class GithubIssuesError(RuntimeError):
    """Issues could not be fetched — missing `gh`, not authenticated, no access, etc."""


def fetch_open_issues(repo_slug: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """Open issues for ``owner/repo``, newest-updated first.

    Pull requests are excluded — `gh issue list` already omits them.
    """
    argv = [
        "gh", "issue", "list",
        "--repo", repo_slug,
        "--state", "open",
        "--limit", str(limit),
        "--json", _FIELDS,
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=True)
    except FileNotFoundError as exc:
        raise GithubIssuesError("GitHub CLI (gh) is not on PATH — install it and run gh auth login") from exc
    except subprocess.TimeoutExpired as exc:
        raise GithubIssuesError(f"listing issues for {repo_slug} timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise GithubIssuesError((exc.stderr or exc.stdout or "gh issue list failed").strip()) from exc
    try:
        issues = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise GithubIssuesError("gh issue list returned invalid JSON") from exc
    if not isinstance(issues, list):
        raise GithubIssuesError("gh issue list returned an unexpected shape")
    return issues
