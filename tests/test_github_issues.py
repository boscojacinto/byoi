"""Solutions sourced from a GitHub project's open issues."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import github_issues, projects as project_ops
from apps.api.main import create_app
from apps.api.store import Store

# Captured before any autouse fixture stubs `fetch_open_issues` off, so the
# tests that exercise its real implementation (mocking subprocess instead)
# still reach it.
_real_fetch_open_issues = github_issues.fetch_open_issues


def _desk(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path), headers={"Authorization": "Bearer byoi-host"})


def _issue(number: int, title: str, body: str = "") -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "url": f"https://github.com/salon/neon/issues/{number}",
        "updatedAt": "2026-01-01T00:00:00Z",
    }


def _project_titles(store: Store, project_id: str) -> set[str]:
    return {i["title"] for i in store.board() if i["project_id"] == project_id}


# --------------------------------------------------------------------- slug detection


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://github.com/salon/neon", "salon/neon"),
        ("https://github.com/salon/neon.git", "salon/neon"),
        ("git@github.com:salon/neon.git", "salon/neon"),
        ("https://gitlab.com/salon/neon.git", None),
        (None, None),
        ("", None),
    ],
)
def test_github_repo_slug(url, slug):
    assert project_ops.github_repo_slug(url) == slug


def test_is_github_project_follows_the_slug():
    assert project_ops.is_github_project({"github": "https://github.com/salon/neon"})
    assert not project_ops.is_github_project({"github": "https://gitlab.com/salon/neon"})
    assert not project_ops.is_github_project({"github": None})


# --------------------------------------------------------------------- fetch_open_issues


def test_fetch_open_issues_parses_gh_json(monkeypatch):
    issues = [_issue(1, "Fix header")]

    def fake_run(argv, **kwargs):
        assert argv[:3] == ["gh", "issue", "list"]
        assert "--repo" in argv and "salon/neon" in argv
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(issues), stderr="")

    monkeypatch.setattr(github_issues.subprocess, "run", fake_run)
    assert _real_fetch_open_issues("salon/neon") == issues


def test_fetch_open_issues_needs_gh_on_path(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(github_issues.subprocess, "run", fake_run)
    with pytest.raises(github_issues.GithubIssuesError):
        _real_fetch_open_issues("salon/neon")


def test_fetch_open_issues_surfaces_gh_failure(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr="not authenticated")

    monkeypatch.setattr(github_issues.subprocess, "run", fake_run)
    with pytest.raises(github_issues.GithubIssuesError, match="not authenticated"):
        _real_fetch_open_issues("salon/neon")


# --------------------------------------------------------------------- Store.sync_board_issues


def test_sync_adds_updates_and_closes_issues(tmp_path: Path):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="neon", local_path=str(tmp_path), github="https://github.com/salon/neon")

    first = store.sync_board_issues(proj["id"], [_issue(1, "Fix header"), _issue(2, "Fix footer")])
    assert first == {"added": 2, "updated": 0, "removed": 0}
    assert _project_titles(store, proj["id"]) == {"Fix header", "Fix footer"}

    second = store.sync_board_issues(proj["id"], [_issue(1, "Fix header — retitled")])
    assert second == {"added": 0, "updated": 1, "removed": 1}
    remaining = [i for i in store.board() if i["project_id"] == proj["id"]]
    assert len(remaining) == 1
    assert remaining[0]["title"] == "Fix header — retitled"
    assert remaining[0]["github_issue_number"] == 1


def test_sync_preserves_host_edits_on_an_existing_issue_row(tmp_path: Path):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="neon", local_path=str(tmp_path), github="https://github.com/salon/neon")
    store.sync_board_issues(proj["id"], [_issue(1, "Fix header")])
    item = next(i for i in store.board() if i["project_id"] == proj["id"])
    store.set_board_spec(item["id"], "must not crash")

    store.sync_board_issues(proj["id"], [_issue(1, "Fix header")])
    refreshed = store.board_item(item["id"])
    assert refreshed["spec"] == "must not crash"


def test_sync_unpublishes_a_closed_issue_that_was_already_claimed(tmp_path: Path):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="neon", local_path=str(tmp_path), github="https://github.com/salon/neon")
    store.sync_board_issues(proj["id"], [_issue(1, "Fix header")])
    item = next(i for i in store.board() if i["project_id"] == proj["id"])
    sess = store.check_in("seat-1", "Ada")
    store.claim(sess["id"], item["id"])

    result = store.sync_board_issues(proj["id"], [])
    assert result["removed"] == 1
    assert item["id"] not in {i["id"] for i in store.board()}
    assert store.board_item(item["id"])["published"] == 0


def test_sync_unknown_project_raises(tmp_path: Path):
    store = Store(tmp_path / "salon.db")
    with pytest.raises(KeyError):
        store.sync_board_issues("nope", [])


# --------------------------------------------------------------------- API


def test_sync_issues_route_merges_onto_the_board(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="neon", local_path=str(tmp_path), github="https://github.com/salon/neon")
    store.close()

    monkeypatch.setattr(
        "apps.api.github_issues.fetch_open_issues",
        lambda slug, **kw: [_issue(1, "Fix header")],
    )
    desk = _desk(tmp_path)
    res = desk.post(f"/api/projects/{proj['id']}/sync-issues")
    assert res.status_code == 200
    assert res.json() == {"project_id": proj["id"], "added": 1, "updated": 0, "removed": 0}

    board = desk.get("/api/board").json()["items"]
    imported = [i for i in board if i["project"]["id"] == proj["id"]]
    assert len(imported) == 1
    assert imported[0]["title"] == "Fix header"
    assert imported[0]["github_issue_number"] == 1


def test_sync_issues_route_rejects_a_non_github_project(tmp_path: Path):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="local-only", local_path=str(tmp_path))
    store.close()

    desk = _desk(tmp_path)
    res = desk.post(f"/api/projects/{proj['id']}/sync-issues")
    assert res.status_code == 400


def test_sync_issues_route_surfaces_a_gh_failure(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="neon", local_path=str(tmp_path), github="https://github.com/salon/neon")
    store.close()

    def _fail(slug, **kw):
        raise github_issues.GithubIssuesError("not authenticated")

    monkeypatch.setattr("apps.api.github_issues.fetch_open_issues", _fail)
    desk = _desk(tmp_path)
    res = desk.post(f"/api/projects/{proj['id']}/sync-issues")
    assert res.status_code == 502


def test_board_get_auto_syncs_a_github_project(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="neon", local_path=str(tmp_path), github="https://github.com/salon/neon")
    store.close()

    calls = []

    def fake_fetch(slug, **kw):
        calls.append(slug)
        return [_issue(1, "Fix header")] if slug == "salon/neon" else []

    monkeypatch.setattr("apps.api.github_issues.fetch_open_issues", fake_fetch)
    desk = _desk(tmp_path)
    board = desk.get("/api/board").json()["items"]
    assert any(i["title"] == "Fix header" for i in board)
    assert calls.count("salon/neon") == 1

    # Second call within the TTL window does not re-fetch this project.
    desk.get("/api/board")
    assert calls.count("salon/neon") == 1


def test_creating_a_github_project_syncs_its_issues(tmp_path: Path, monkeypatch):
    def fake_run(argv, *, cwd=None, timeout=180):
        assert argv[:3] == ["gh", "repo", "create"]
        dest = project_ops.projects_root() / "neon"
        dest.mkdir(parents=True)
        (dest / "README.md").write_text("# neon\n")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def fake_remote(dest):
        return "https://github.com/salon/neon.git"

    monkeypatch.setattr(project_ops, "_run", fake_run)
    monkeypatch.setattr(project_ops, "_git_remote", fake_remote)
    monkeypatch.setattr(
        "apps.api.github_issues.fetch_open_issues",
        lambda slug, **kw: [_issue(1, "Fix header")],
    )
    desk = _desk(tmp_path)
    created = desk.post("/api/projects", json={"kind": "github", "name": "neon", "private": True})
    assert created.status_code == 200

    board = desk.get("/api/board").json()["items"]
    imported = [i for i in board if i["project"]["id"] == created.json()["id"]]
    assert len(imported) == 1
    assert imported[0]["title"] == "Fix header"
