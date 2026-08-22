from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.store import Store
from apps.seat.claude_chat import ClaudeChat


def _desk(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path), client=("127.0.0.1", 50000))


def test_store_attaches_project(tmp_path: Path):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="slip", local_path=str(tmp_path), github="https://github.com/x/slip")
    item = store.add_board("Fix QR", "ship contrast", 60, 40, project_id=proj["id"])
    assert item["project"]["name"] == "slip"
    assert item["project"]["local_path"] == str(tmp_path)
    listed = store.board()
    assert any(i["id"] == item["id"] and i["project"]["id"] == proj["id"] for i in listed)


def test_host_creates_local_project_and_publishes(tmp_path: Path):
    desk = _desk(tmp_path)
    folder = tmp_path / "app"
    folder.mkdir()
    created = desk.post("/api/projects", json={"kind": "local", "path": str(folder), "name": "cafe-app"})
    assert created.status_code == 200
    body = created.json()
    assert body["name"] == "cafe-app"
    assert body["local_path"] == str(folder.resolve())
    brief = desk.post(
        "/api/board",
        json={"title": "Ship the app", "brief": "make it run", "project_id": body["id"]},
    )
    assert brief.status_code == 200
    assert brief.json()["project"]["name"] == "cafe-app"


def test_unknown_project_on_board_is_404(tmp_path: Path):
    desk = _desk(tmp_path)
    res = desk.post("/api/board", json={"title": "x", "brief": "y", "project_id": "nope"})
    assert res.status_code == 404


def test_claim_pushes_workspace_to_seat(tmp_path: Path, monkeypatch):
    desk = _desk(tmp_path)
    folder = tmp_path / "work"
    folder.mkdir()
    proj = desk.post("/api/projects", json={"kind": "local", "path": str(folder), "name": "work"}).json()
    brief = desk.post(
        "/api/board",
        json={"title": "Use this repo", "brief": "edit it", "project_id": proj["id"]},
    ).json()
    seen = []
    monkeypatch.setattr("apps.api.seat_sync.set_workspace", lambda seat, path: seen.append(path) or {"ok": True})
    check = desk.post("/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"})
    sid = check.json()["session"]["id"]
    claimed = desk.post(f"/api/sessions/{sid}/claim", json={"board_id": brief["id"]})
    assert claimed.status_code == 200
    assert seen == [str(folder.resolve())]
    assert claimed.json()["project"]["name"] == "work"


def test_create_github_repo_uses_gh(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BYOI_PROJECTS_DIR", str(tmp_path / "projects"))
    dest = tmp_path / "projects" / "neon"

    def fake_run(argv, *, cwd=None, timeout=180):
        if argv[:3] == ["gh", "repo", "create"]:
            dest.mkdir(parents=True)
            (dest / "README.md").write_text("# neon\n")
            class R:
                stdout = ""
                stderr = ""
            return R()
        if argv[:3] == ["git", "remote", "get-url"]:
            class R:
                stdout = "https://github.com/salon/neon.git\n"
                stderr = ""
            return R()
        raise AssertionError(argv)

    monkeypatch.setattr("apps.api.projects._run", fake_run)
    desk = _desk(tmp_path)
    res = desk.post("/api/projects", json={"kind": "github", "name": "neon", "private": True})
    assert res.status_code == 200
    assert res.json()["github"] == "https://github.com/salon/neon.git"
    assert Path(res.json()["local_path"]).is_dir()


def test_claude_chat_switches_cwd(tmp_path: Path):
    chat = ClaudeChat()
    other = tmp_path / "repo"
    other.mkdir()
    assert chat.set_workspace(other) == other.resolve()
    assert chat.workspace_path == other.resolve()
