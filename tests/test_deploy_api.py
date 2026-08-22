from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app


def _desk(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path), client=("127.0.0.1", 50000))


def _session_with_project(desk: TestClient, tmp_path: Path, *, spec: str = "") -> tuple[str, dict]:
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir(exist_ok=True)
    (proj_dir / "byoi.json").write_text('{"framework":"nextjs","needs":["auth"]}')
    project = desk.post("/api/projects", json={"kind": "local", "path": str(proj_dir)}).json()
    brief = desk.post(
        "/api/board",
        json={"title": "Notes", "brief": "build it", "spec": spec, "project_id": project["id"]},
    ).json()
    sid = desk.post(
        "/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}
    ).json()["session"]["id"]
    desk.post(f"/api/sessions/{sid}/claim", json={"board_id": brief["id"]})
    return sid, project


def test_templates_are_listed(tmp_path: Path):
    desk = _desk(tmp_path)
    names = [t["name"] for t in desk.get("/api/templates").json()["templates"]]
    assert "next-fullstack" in names


def test_creating_from_a_template_records_the_framework(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BYOI_PROJECTS_DIR", str(tmp_path / "projects"))
    desk = _desk(tmp_path)
    res = desk.post(
        "/api/projects", json={"kind": "template", "template": "next-fullstack", "name": "notes"}
    )
    assert res.status_code == 200, res.text
    project = res.json()
    assert project["framework"] == "nextjs"
    assert project["template"] == "next-fullstack"
    # It is a real git repo with a commit, ready to be pinned to a ref.
    assert (Path(project["local_path"]) / ".git").is_dir()
    assert (Path(project["local_path"]) / "app" / "api" / "health" / "route.ts").is_file()


def test_unknown_template_is_a_400(tmp_path: Path):
    desk = _desk(tmp_path)
    res = desk.post("/api/projects", json={"kind": "template", "template": "nope"})
    assert res.status_code == 400


def test_deploy_records_the_url(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "apps.api.seat_sync.submit_solution",
        lambda *a, **k: {"ref": "refs/byoi/deploys/x", "toplevel": str(tmp_path / "proj")},
    )
    monkeypatch.setattr(
        "apps.api.deploy.run",
        lambda **k: {
            "url": "https://preview.vercel.app",
            "resources": [{"kind": "auth"}],
            "notes": [],
            "framework": "nextjs",
            "detail": None,
        },
    )
    desk = _desk(tmp_path)
    sid, _ = _session_with_project(desk, tmp_path)
    started = desk.post(f"/api/sessions/{sid}/deploy", json={})
    assert started.status_code == 200
    got = desk.get(f"/api/sessions/{sid}/deployment").json()["deployment"]
    assert got["state"] == "ready"
    assert got["url"] == "https://preview.vercel.app"
    assert got["resources"] == [{"kind": "auth"}]


def test_a_failed_deploy_is_recorded_not_left_running(tmp_path: Path, monkeypatch):
    from apps.api.deploy import DeployError

    monkeypatch.setattr(
        "apps.api.seat_sync.submit_solution",
        lambda *a, **k: {"ref": "refs/byoi/deploys/x", "toplevel": str(tmp_path / "proj")},
    )

    def boom(**k):
        raise DeployError("build failed")

    monkeypatch.setattr("apps.api.deploy.run", boom)
    desk = _desk(tmp_path)
    sid, _ = _session_with_project(desk, tmp_path)
    desk.post(f"/api/sessions/{sid}/deploy", json={})
    got = desk.get(f"/api/sessions/{sid}/deployment").json()["deployment"]
    assert got["state"] == "failed"
    assert "build failed" in got["detail"]


def test_an_unexpected_deploy_crash_is_still_recorded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "apps.api.seat_sync.submit_solution",
        lambda *a, **k: {"ref": "refs/byoi/deploys/x", "toplevel": str(tmp_path / "proj")},
    )

    def boom(**k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("apps.api.deploy.run", boom)
    desk = _desk(tmp_path)
    sid, _ = _session_with_project(desk, tmp_path)
    desk.post(f"/api/sessions/{sid}/deploy", json={})
    got = desk.get(f"/api/sessions/{sid}/deployment").json()["deployment"]
    assert got["state"] == "failed"
    assert "disk on fire" in got["detail"]


def test_deploy_needs_a_project(tmp_path: Path):
    desk = _desk(tmp_path)
    sid = desk.post(
        "/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}
    ).json()["session"]["id"]
    assert desk.post(f"/api/sessions/{sid}/deploy", json={}).status_code == 409


def test_freeing_the_seat_tears_the_preview_down(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "apps.api.seat_sync.submit_solution",
        lambda *a, **k: {"ref": "refs/byoi/deploys/x", "toplevel": str(tmp_path / "proj")},
    )
    monkeypatch.setattr(
        "apps.api.deploy.run",
        lambda **k: {"url": "https://preview.vercel.app", "resources": [{"kind": "auth"}],
                     "notes": [], "framework": "nextjs", "detail": None},
    )
    torn: list[dict] = []
    monkeypatch.setattr(
        "apps.api.deploy.teardown",
        lambda d: torn.append(d) or {"ok": True, "problems": []},
    )
    desk = _desk(tmp_path)
    sid, _ = _session_with_project(desk, tmp_path)
    desk.post(f"/api/sessions/{sid}/deploy", json={})
    desk.post("/api/seats/seat-1/free")

    assert torn and torn[0]["url"] == "https://preview.vercel.app"
    got = desk.get(f"/api/sessions/{sid}/deployment").json()["deployment"]
    assert got["state"] == "torn_down"
    assert got["torn_down_at"] is not None


def test_teardown_problems_do_not_block_freeing_the_seat(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "apps.api.seat_sync.submit_solution",
        lambda *a, **k: {"ref": "refs/byoi/deploys/x", "toplevel": str(tmp_path / "proj")},
    )
    monkeypatch.setattr(
        "apps.api.deploy.run",
        lambda **k: {"url": "https://preview.vercel.app", "resources": [],
                     "notes": [], "framework": "nextjs", "detail": None},
    )
    monkeypatch.setattr(
        "apps.api.deploy.teardown", lambda d: (_ for _ in ()).throw(RuntimeError("vercel down"))
    )
    desk = _desk(tmp_path)
    sid, _ = _session_with_project(desk, tmp_path)
    desk.post(f"/api/sessions/{sid}/deploy", json={})
    freed = desk.post("/api/seats/seat-1/free")
    assert freed.status_code == 200
    got = desk.get(f"/api/sessions/{sid}/deployment").json()["deployment"]
    assert got["state"] == "torn_down"
    assert "vercel down" in got["detail"]


def test_teardown_is_not_repeated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "apps.api.seat_sync.submit_solution",
        lambda *a, **k: {"ref": "refs/byoi/deploys/x", "toplevel": str(tmp_path / "proj")},
    )
    monkeypatch.setattr(
        "apps.api.deploy.run",
        lambda **k: {"url": "https://preview.vercel.app", "resources": [],
                     "notes": [], "framework": "nextjs", "detail": None},
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        "apps.api.deploy.teardown", lambda d: calls.append(d) or {"ok": True, "problems": []}
    )
    desk = _desk(tmp_path)
    sid, _ = _session_with_project(desk, tmp_path)
    desk.post(f"/api/sessions/{sid}/deploy", json={})
    desk.post("/api/seats/seat-1/free")
    desk.post("/api/seats/seat-1/free")
    assert len(calls) == 1
