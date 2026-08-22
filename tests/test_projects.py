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


def test_detect_reads_a_byoi_manifest(tmp_path):
    from apps.api.projects import detect

    (tmp_path / "byoi.json").write_text('{"framework":"remix","needs":["postgres"],"build":"x"}')
    out = detect(tmp_path)
    assert out["source"] == "manifest"
    assert out["framework"] == "remix"
    assert out["needs"] == ["postgres"]
    assert out["deployable"] is True


def test_detect_falls_back_to_heuristics(tmp_path):
    from apps.api.projects import detect

    (tmp_path / "package.json").write_text(
        '{"dependencies":{"next":"15","ioredis":"5","@auth/core":"0.3"},'
        '"scripts":{"build":"next build","db:init":"node x"}}'
    )
    (tmp_path / ".env.example").write_text("DATABASE_URL=postgres://x\n")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    out = detect(tmp_path)
    assert out["source"] == "detected"
    assert out["framework"] == "nextjs"
    assert sorted(out["needs"]) == ["auth", "postgres", "redis"]
    assert out["install"] == "pnpm install"
    assert out["migrate"] == "pnpm run db:init"


def test_detect_marks_a_python_repo_undeployable(tmp_path):
    from apps.api.projects import detect

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    out = detect(tmp_path)
    assert out["framework"] == "python"
    assert out["deployable"] is False


def test_detect_survives_broken_json(tmp_path):
    from apps.api.projects import detect

    (tmp_path / "package.json").write_text("{not json")
    assert detect(tmp_path)["framework"] is None


def test_templates_carry_their_spec():
    from apps.api.projects import templates

    starter = next(t for t in templates() if t["name"] == "next-fullstack")
    assert "GET /api/health" in starter["spec"]
    assert sorted(starter["needs"]) == ["auth", "postgres", "redis"]


def test_from_template_rejects_an_unknown_name(tmp_path, monkeypatch):
    import pytest

    from apps.api.projects import from_template

    monkeypatch.setenv("BYOI_PROJECTS_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="unknown template"):
        from_template(template="nope")
