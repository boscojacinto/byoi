"""Per-session Postgres and Redis, raised by the desk rather than the seat.

The seat runs guest code, so it does not get a Docker socket. What is worth
holding here is that moving the containers did not change the contract the app
reads, and that the seat still owns its own .env.local.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import infra as desk_infra
from apps.seat import infra as seat_infra
from apps.seat.control import app as control_app

HOST = {"Authorization": "Bearer byoi-host"}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_DESK_INFRA_DIR", str(tmp_path / "desk-infra"))


def test_hosts_are_derived_from_the_session():
    assert desk_infra.network_name("ab/CD 1") == "byoi-infra-abcd1"
    assert desk_infra.db_host("abc123") == "byoi-pg-abc123"
    assert desk_infra.cache_host("abc123") == "byoi-redis-abc123"


def test_the_compose_file_publishes_no_ports():
    """Two seats are kept apart by being on different networks. Scraping an
    ephemeral host port back out of docker was the old way and is gone."""
    body = desk_infra.COMPOSE.format(
        postgres_image="postgres:16-alpine",
        redis_image="redis:7-alpine",
        db_password="pw",
        cache_password="pw",
        db_host="byoi-pg-abc",
        cache_host="byoi-redis-abc",
        network="byoi-infra-abc",
    )
    assert "ports:" not in body
    assert "name: byoi-infra-abc" in body
    assert "container_name: byoi-pg-abc" in body


def test_urls_point_at_service_names(tmp_path, monkeypatch):
    """The app still reads DATABASE_URL and REDIS_URL and nothing else —
    only what is on the other end of them moved."""
    calls = []

    def _fake_compose(*args, cwd, project, timeout=180):
        calls.append(args)
        import subprocess

        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(desk_infra, "_docker_ok", lambda: None)
    monkeypatch.setattr(desk_infra, "_compose", _fake_compose)
    env = desk_infra.up(session_id="abc123")

    assert env["DATABASE_URL"].startswith("postgresql://byoi:")
    assert "@byoi-pg-abc123:5432/byoi" in env["DATABASE_URL"]
    assert "@byoi-redis-abc123:6379" in env["REDIS_URL"]
    assert "127.0.0.1" not in env["DATABASE_URL"]
    assert env["AUTH_SECRET"]


def test_passwords_survive_a_second_up(tmp_path, monkeypatch):
    monkeypatch.setattr(desk_infra, "_docker_ok", lambda: None)
    monkeypatch.setattr(
        desk_infra, "_compose", lambda *a, **k: __import__("subprocess").CompletedProcess(a, 0, "", "")
    )
    first = desk_infra.up(session_id="abc123")
    second = desk_infra.up(session_id="abc123")
    assert first["DATABASE_URL"] == second["DATABASE_URL"]
    assert first["AUTH_SECRET"] == second["AUTH_SECRET"]


def test_attach_puts_the_seat_on_its_own_network(monkeypatch):
    seen = {}

    def _run(args, **kwargs):
        seen["args"] = args
        return __import__("subprocess").CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(desk_infra.subprocess, "run", _run)
    desk_infra.attach("abc123", "byoi-seat-abc123")
    assert seen["args"] == [
        "docker", "network", "connect", "byoi-infra-abc123", "byoi-seat-abc123",
    ]


def test_status_without_a_stack_is_quiet():
    assert desk_infra.status("nothing-here") == {"up": False, "services": [], "env": {}}


def test_down_without_a_stack_is_a_noop():
    assert desk_infra.down("nothing-here") == {"ok": True, "removed": False}


# --- the seat's half ---------------------------------------------------------


def test_the_seat_writes_the_desks_urls_into_env_local(tmp_path):
    """The seat cannot raise these containers, but it still owns the file."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / seat_infra.ENV_FILE).write_text("MY_OWN=1\n")

    client = TestClient(control_app)
    res = client.post(
        "/local/infra/env",
        json={
            "session_id": "abc123",
            "cwd": str(proj),
            "env": {"DATABASE_URL": "postgresql://byoi:pw@byoi-pg-abc123:5432/byoi"},
        },
        headers=HOST,
    )
    assert res.status_code == 200
    body = (proj / seat_infra.ENV_FILE).read_text()
    assert "MY_OWN=1" in body
    assert "byoi-pg-abc123" in body


def test_the_env_route_needs_the_host_token(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    client = TestClient(control_app)
    res = client.post(
        "/local/infra/env",
        json={"session_id": "abc123", "cwd": str(proj), "env": {"DATABASE_URL": "x"}},
    )
    assert res.status_code == 401


def test_a_missing_workspace_is_a_409(tmp_path):
    client = TestClient(control_app)
    res = client.post(
        "/local/infra/env",
        json={"session_id": "abc123", "cwd": str(tmp_path / "nope"), "env": {"A": "1"}},
        headers=HOST,
    )
    assert res.status_code == 409


# --- which driver the desk picks ---------------------------------------------


def test_cloud_claims_use_the_desk_driver(tmp_path, monkeypatch):
    from apps.api.main import create_app

    monkeypatch.setenv("BYOI_SEATS", "ondemand")
    monkeypatch.setattr("apps.api.seats.provision", lambda *a, **k: {})
    used = {}
    monkeypatch.setattr(
        "apps.api.infra.up",
        lambda **kw: used.setdefault("desk", kw) or {"DATABASE_URL": "postgresql://x"},
    )
    monkeypatch.setattr(
        "apps.api.seat_sync.push_infra_env", lambda *a, **k: used.setdefault("pushed", k) or {}
    )
    monkeypatch.setattr(
        "apps.api.seat_sync.infra_up",
        lambda *a, **k: pytest.fail("a cloud seat has no docker to raise a stack with"),
    )

    client = TestClient(create_app(tmp_path), headers=HOST)
    proj = tmp_path / "proj"
    proj.mkdir()
    # A brief with a real data layer is the only kind that raises a stack.
    (proj / "byoi.json").write_text(
        json.dumps({"framework": "next", "needs": ["postgres", "redis"]})
    )
    project = client.post(
        "/api/projects", json={"kind": "local", "name": "p", "path": str(proj)}
    ).json()
    brief = client.post(
        "/api/board",
        json={"title": "t", "brief": "b", "spec": "- works", "project_id": project["id"]},
    ).json()
    sid = client.post(
        "/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}
    ).json()["session"]["id"]
    client.post(f"/api/sessions/{sid}/claim", json={"board_id": brief["id"]})

    assert used["desk"]["session_id"] == sid
    assert used["desk"]["container"] == f"byoi-seat-{sid}"
    assert used["pushed"]["session_id"] == sid
