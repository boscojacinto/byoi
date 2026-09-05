"""Claude drafts a Brief/Specs & QA/time-budget recommendation from a title,
grounded in the project's own checkout."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import board_suggest, testgen
from apps.api.board_suggest import BoardSuggestError
from apps.api.main import create_app
from apps.api.store import Store

SUGGESTION = {
    "brief": "Add a footer link to the privacy policy.",
    "spec": "- Every page renders a footer link labelled 'Privacy'.\n- The link points at /privacy.",
    "wellness_minutes": 60,
    "break_after": 30,
}


def _desk(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path), headers={"Authorization": "Bearer byoi-host"})


def _log_in_host(monkeypatch, tmp_path: Path) -> None:
    accounts = tmp_path / "accounts"
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(accounts))
    host_dir = accounts / testgen.host_account_label()
    host_dir.mkdir(parents=True)
    (host_dir / ".credentials.json").write_text("{}")


# --------------------------------------------------------------------- parse_suggestion


def test_parse_suggestion_happy_path():
    raw = json.dumps({"structured_output": SUGGESTION})
    result = board_suggest.parse_suggestion(raw)
    assert result["brief"] == SUGGESTION["brief"]
    assert result["wellness_minutes"] == 60
    assert result["break_after"] == 30


def test_parse_suggestion_clamps_out_of_range_minutes():
    bad = {**SUGGESTION, "wellness_minutes": 10_000, "break_after": 1}
    result = board_suggest.parse_suggestion(json.dumps({"structured_output": bad}))
    assert result["wellness_minutes"] == board_suggest.MAX_WELLNESS
    assert result["break_after"] == board_suggest.MIN_BREAK


def test_parse_suggestion_fixes_a_break_not_shorter_than_the_session():
    bad = {**SUGGESTION, "wellness_minutes": 60, "break_after": 90}
    result = board_suggest.parse_suggestion(json.dumps({"structured_output": bad}))
    assert result["break_after"] < result["wellness_minutes"]


def test_parse_suggestion_rejects_an_empty_brief():
    bad = {**SUGGESTION, "brief": "   "}
    with pytest.raises(BoardSuggestError, match="brief or spec"):
        board_suggest.parse_suggestion(json.dumps({"structured_output": bad}))


def test_parse_suggestion_rejects_unparseable_output():
    with pytest.raises(BoardSuggestError, match="could not parse"):
        board_suggest.parse_suggestion("not json at all")


# --------------------------------------------------------------------- suggest_solution


def test_suggest_solution_requires_host_login(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path / "accounts"))
    with pytest.raises(BoardSuggestError, match="is not logged in"):
        board_suggest.suggest_solution(title="Add a footer link", project={"name": "p", "local_path": str(tmp_path)})


def test_suggest_solution_grounds_the_call_in_the_project_checkout(tmp_path: Path, monkeypatch):
    _log_in_host(monkeypatch, tmp_path)
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    monkeypatch.setattr(board_suggest.project_ops, "ensure_local", lambda p: str(project_dir))

    seen = {}

    def fake_run(argv, *, cwd, capture_output, text, timeout, env):
        seen["argv"] = argv
        seen["cwd"] = cwd
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"structured_output": SUGGESTION}), stderr="")

    monkeypatch.setattr(board_suggest.subprocess, "run", fake_run)
    result = board_suggest.suggest_solution(
        title="Add a footer link", project={"name": "fusionstudio", "local_path": str(project_dir)}
    )
    assert result["brief"] == SUGGESTION["brief"]
    assert seen["cwd"] == str(project_dir)
    assert "--allowedTools" in seen["argv"]
    tools = seen["argv"][seen["argv"].index("--allowedTools") + 1]
    assert set(tools.split(",")) == {"Read", "Glob", "Grep"}
    assert "Bash" not in tools and "Write" not in tools and "Edit" not in tools


def test_suggest_solution_surfaces_a_missing_repo(tmp_path: Path, monkeypatch):
    _log_in_host(monkeypatch, tmp_path)
    monkeypatch.setattr(
        board_suggest.project_ops,
        "ensure_local",
        lambda p: (_ for _ in ()).throw(FileNotFoundError("gone")),
    )
    with pytest.raises(BoardSuggestError, match="gone"):
        board_suggest.suggest_solution(title="t", project={"name": "p", "local_path": str(tmp_path)})


def test_suggest_solution_times_out(tmp_path: Path, monkeypatch):
    _log_in_host(monkeypatch, tmp_path)
    monkeypatch.setattr(board_suggest.project_ops, "ensure_local", lambda p: str(tmp_path))

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(board_suggest.subprocess, "run", fake_run)
    with pytest.raises(BoardSuggestError, match="timed out"):
        board_suggest.suggest_solution(title="t", project={"name": "p", "local_path": str(tmp_path)})


# --------------------------------------------------------------------------------- API


def test_board_suggest_route_returns_the_draft(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="fusionstudio", local_path=str(tmp_path))
    store.close()

    monkeypatch.setattr(
        "apps.api.board_suggest.suggest_solution", lambda **kw: SUGGESTION
    )
    desk = _desk(tmp_path)
    res = desk.post("/api/board/suggest", json={"title": "Add a footer link", "project_id": proj["id"]})
    assert res.status_code == 200
    assert res.json() == SUGGESTION


def test_board_suggest_route_rejects_an_unknown_project(tmp_path: Path):
    desk = _desk(tmp_path)
    res = desk.post("/api/board/suggest", json={"title": "t", "project_id": "nope"})
    assert res.status_code == 404


def test_board_suggest_route_surfaces_a_generation_failure(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="fusionstudio", local_path=str(tmp_path))
    store.close()

    def _fail(**kw):
        raise BoardSuggestError("host Claude account 'claude-host' is not logged in")

    monkeypatch.setattr("apps.api.board_suggest.suggest_solution", _fail)
    desk = _desk(tmp_path)
    res = desk.post("/api/board/suggest", json={"title": "t", "project_id": proj["id"]})
    assert res.status_code == 502


def test_board_suggest_route_requires_operator(tmp_path: Path):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="fusionstudio", local_path=str(tmp_path))
    store.close()
    desk = TestClient(create_app(tmp_path))
    res = desk.post("/api/board/suggest", json={"title": "t", "project_id": proj["id"]})
    assert res.status_code == 401
