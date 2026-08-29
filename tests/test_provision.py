"""Raising and destroying a seat container per visit.

Docker and Caddy are stubbed. What is being tested is the shape of what the
desk asks for — particularly the things a seat must *not* be given, since that
is the whole reason provisioning lives on the desk.
"""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import caddy, seats
from apps.api.main import create_app
from apps.api.store import Store

HOST = {"Authorization": "Bearer byoi-host"}


@pytest.fixture
def fake_docker(tmp_path, monkeypatch) -> Path:
    """A `docker` on PATH that records its arguments and succeeds."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    script = bin_dir / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        'case "$1" in\n'
        '  run) echo deadbeefcafe0000 ;;\n'
        '  ps) ;;\n'
        '  inspect) exit 1 ;;\n'
        'esac\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("BYOI_SEAT_RUNTIME_DIR", str(tmp_path / "runtime"))
    return log


@pytest.fixture
def caddy_admin(monkeypatch):
    """A stand-in for Caddy's admin API, over TCP because a test does not need
    the unix socket that keeps seats away from the real one."""
    state = {"routes": [], "deleted": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the test output clean
            pass

        def _send(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/config/apps/http/servers":
                self._send(200, json.dumps({"srv0": {"listen": [":443"]}}).encode())
            elif self.path.endswith("/routes"):
                self._send(200, json.dumps(state["routes"]).encode())
            else:
                self._send(404)

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", 0))
            state["routes"].insert(0, json.loads(self.rfile.read(length)))
            self._send(200)

        def do_DELETE(self):
            state["deleted"].append(self.path)
            state["routes"] = [
                r for r in state["routes"] if not self.path.endswith(r.get("@id", "\0"))
            ]
            self._send(200)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("BYOI_CADDY_ADMIN", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("BYOI_DOMAIN", "salon.example")
    yield state
    server.shutdown()


# --- naming -----------------------------------------------------------------


def test_names_are_derived_from_the_session():
    assert seats.container_name("abc123") == "byoi-seat-abc123"
    assert seats.internal_url("abc123") == "http://byoi-seat-abc123:8787"
    with pytest.raises(seats.SeatError):
        seats.container_name("!!!")


def test_a_session_gets_its_own_hostname(monkeypatch):
    monkeypatch.setenv("BYOI_DOMAIN", "salon.example")
    assert caddy.seat_hostname("abc123") == "s-abc123.salon.example"


# --- what the container is given, and what it is not -------------------------


def _args(monkeypatch, tmp_path, labels=("claude-seat-1",)):
    monkeypatch.setenv("BYOI_SEAT_RUNTIME_DIR", str(tmp_path / "runtime"))
    return seats._run_args(
        "abc123",
        tls_dir=tmp_path / "tls",
        labels=list(labels),
        seat={"id": "seat-1", "name": "Seat 1"},
    )


def test_a_seat_gets_no_docker_socket(monkeypatch, tmp_path):
    """The guest's Claude has Bash. A socket here would be root on the VM."""
    joined = " ".join(_args(monkeypatch, tmp_path))
    assert "docker.sock" not in joined


def test_a_seat_gets_no_deploy_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "should-never-be-passed")
    joined = " ".join(_args(monkeypatch, tmp_path))
    assert "should-never-be-passed" not in joined
    assert "BYOI_VERCEL_TOKEN" not in joined


def test_a_guests_own_token_lands_on_tmpfs(monkeypatch, tmp_path):
    args = _args(monkeypatch, tmp_path)
    tmpfs = args[args.index("--tmpfs") + 1]
    assert tmpfs.startswith("/run/byoi:")
    assert "mode=0700" in tmpfs
    assert "BYOI_GUEST_RUNTIME_DIR=/run/byoi" in args


def test_only_the_allocated_accounts_are_mounted(monkeypatch, tmp_path):
    """A guest must not be able to read the credentials another guest is on."""
    joined = " ".join(_args(monkeypatch, tmp_path, labels=("claude-seat-1", "claude-seat-2")))
    assert "/app/data/claude-accounts/claude-seat-1" in joined
    assert "/app/data/claude-accounts/claude-seat-2" in joined
    assert "claude-seat-3" not in joined


def test_the_seat_is_told_it_is_public_and_not_holding_tls(monkeypatch, tmp_path):
    args = _args(monkeypatch, tmp_path)
    assert "BYOI_GUEST_NET=public" in args
    assert "BYOI_GUEST_TLS=0" in args


def test_resource_caps_are_set(monkeypatch, tmp_path):
    args = _args(monkeypatch, tmp_path)
    for flag in ("--memory", "--cpus", "--pids-limit"):
        assert flag in args


# --- account allocation ------------------------------------------------------


def test_accounts_are_not_handed_to_two_seats(tmp_path, monkeypatch):
    accounts = tmp_path / "accounts"
    for name in ("claude-seat-1", "claude-seat-2"):
        (accounts / name).mkdir(parents=True)
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(accounts))

    store = Store(tmp_path / "salon.db")
    first = store.check_in("seat-1", "Ada")
    labels = seats.allocate_accounts(store, first["id"], want=1)
    store.set_session_accounts(first["id"], labels)

    second = store.check_in("seat-2", "Bo")
    other = seats.allocate_accounts(store, second["id"], want=1)
    assert set(labels).isdisjoint(other)


def test_running_out_of_accounts_says_so(tmp_path, monkeypatch):
    accounts = tmp_path / "accounts"
    (accounts / "claude-seat-1").mkdir(parents=True)
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(accounts))

    store = Store(tmp_path / "salon.db")
    first = store.check_in("seat-1", "Ada")
    store.set_session_accounts(first["id"], seats.allocate_accounts(store, first["id"], want=1))
    second = store.check_in("seat-2", "Bo")
    with pytest.raises(seats.SeatError) as err:
        seats.allocate_accounts(store, second["id"], want=1)
    assert "already in use" in str(err.value)


# --- the edge ----------------------------------------------------------------


def test_a_route_goes_in_front_of_the_wildcard_catch_all(caddy_admin):
    """Appended after it, the wildcard site's 404 would answer first."""
    caddy_admin["routes"].append({"@id": "wildcard-fallback"})
    caddy.publish("abc123", "byoi-seat-abc123:8787")
    assert caddy_admin["routes"][0]["@id"] == "byoi-seat-abc123"
    assert caddy_admin["routes"][0]["match"] == [{"host": ["s-abc123.salon.example"]}]
    dial = caddy_admin["routes"][0]["handle"][0]["routes"][0]["handle"][0]["upstreams"]
    assert dial == [{"dial": "byoi-seat-abc123:8787"}]


def test_unpublish_removes_it(caddy_admin):
    caddy.publish("abc123", "byoi-seat-abc123:8787")
    caddy.unpublish("abc123")
    assert not [r for r in caddy_admin["routes"] if r.get("@id") == "byoi-seat-abc123"]


def test_published_lists_only_our_routes(caddy_admin):
    caddy_admin["routes"].append({"@id": "something-else"})
    caddy.publish("abc123", "byoi-seat-abc123:8787")
    assert caddy.published() == ["abc123"]


def test_with_no_edge_configured_publish_is_a_no_op(monkeypatch):
    monkeypatch.delenv("BYOI_CADDY_ADMIN", raising=False)
    monkeypatch.setenv("BYOI_DOMAIN", "salon.example")
    assert caddy.publish("abc123", "byoi-seat-abc123:8787") == "s-abc123.salon.example"
    assert caddy.unpublish("abc123") is False


# --- provision and teardown --------------------------------------------------


@pytest.fixture
def ready_seat(monkeypatch, tmp_path):
    accounts = tmp_path / "accounts"
    (accounts / "claude-seat-1").mkdir(parents=True)
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(accounts))
    monkeypatch.setattr(seats, "mint_identity", lambda sid: tmp_path / "tls")
    monkeypatch.setattr(seats.seat_sync, "seat_status", lambda *a, **k: {"ok": True})


def test_provision_records_the_container_and_hostname(
    tmp_path, fake_docker, caddy_admin, ready_seat
):
    store = Store(tmp_path / "salon.db")
    sess = store.check_in("seat-1", "Ada")
    seat = store.seat("seat-1")
    out = seats.provision(store, sess, seat)

    assert out["public_host"] == f"s-{sess['id']}.salon.example"
    assert out["agent_url"] == seats.internal_url(sess["id"])
    after = store.seat("seat-1")
    assert after["state"] == "ready"
    assert after["container_id"] == "deadbeefcafe"
    # The control URL the desk will use is derived from that agent_url.
    from apps.api.seat_sync import control_base

    assert control_base(after) == f"https://byoi-seat-{sess['id']}:8788"


def test_provision_attaches_the_control_network(tmp_path, fake_docker, caddy_admin, ready_seat):
    store = Store(tmp_path / "salon.db")
    sess = store.check_in("seat-1", "Ada")
    seats.provision(store, sess, store.seat("seat-1"))
    assert "network connect byoi-ctl" in fake_docker.read_text()


def test_provision_refuses_past_the_seat_cap(tmp_path, fake_docker, caddy_admin, ready_seat, monkeypatch):
    """Seats are RAM on this VM now, not PCs somebody already owns."""
    monkeypatch.setenv("BYOI_MAX_SEATS", "1")
    monkeypatch.setattr(seats, "live_seat_count", lambda: 1)
    store = Store(tmp_path / "salon.db")
    sess = store.check_in("seat-1", "Ada")
    with pytest.raises(seats.SeatError) as err:
        seats.provision(store, sess, store.seat("seat-1"))
    assert "BYOI_MAX_SEATS" in str(err.value)


def test_teardown_removes_everything_the_visit_made(
    tmp_path, fake_docker, caddy_admin, ready_seat, monkeypatch
):
    monkeypatch.setattr(seats.seat_sync, "revoke_session", lambda *a, **k: {"ok": True})
    store = Store(tmp_path / "salon.db")
    sess = store.check_in("seat-1", "Ada")
    seats.provision(store, sess, store.seat("seat-1"))

    # The guest's tree is a directory on the VM, not a named volume, so what
    # proves it is gone is the directory rather than a `docker volume rm`.
    workspace = seats.workspace_dir(sess["id"])
    (workspace / "scratch.txt").write_text("guest work")

    result = seats.teardown(store, sess, store.seat("seat-1"))
    assert result["ok"], result["problems"]
    log = fake_docker.read_text()
    assert f"rm -f byoi-seat-{sess['id']}" in log
    assert not workspace.exists()
    assert not caddy_admin["routes"]
    assert store.seat("seat-1")["state"] == "idle"
    assert store.session(sess["id"])["account_labels"] == []


def test_teardown_reports_problems_but_never_raises(
    tmp_path, fake_docker, caddy_admin, ready_seat, monkeypatch
):
    """An operator with a guest standing there must always be able to free a chair."""

    def _boom(*a, **k):
        raise RuntimeError("seat is wedged")

    monkeypatch.setattr(seats.seat_sync, "revoke_session", _boom)
    store = Store(tmp_path / "salon.db")
    sess = store.check_in("seat-1", "Ada")
    result = seats.teardown(store, sess, store.seat("seat-1"))
    assert result["ok"] is False
    assert any("seat is wedged" in p for p in result["problems"])


def test_reconcile_clears_a_seat_with_no_live_session(tmp_path, monkeypatch, caddy_admin):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        'if [ "$1" = "ps" ]; then echo byoi-seat-ghost; fi\n'
        "exit 0\n"
    )
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("BYOI_SEAT_RUNTIME_DIR", str(tmp_path / "runtime"))

    store = Store(tmp_path / "salon.db")
    assert seats.reconcile(store)["removed"] == ["ghost"]
    assert "rm -f byoi-seat-ghost" in log.read_text()


# --- the desk's view ---------------------------------------------------------


def test_checkin_returns_preparing_and_a_poll_address(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_SEATS", "ondemand")
    # Nothing to raise a container with here; the point is the response shape.
    monkeypatch.setattr("apps.api.seats.provision", lambda *a, **k: {})
    client = TestClient(create_app(tmp_path), headers=HOST)
    res = client.post("/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"})
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "preparing"
    assert body["seat_admitted"] is False
    assert body["poll"] == f"/api/sessions/{body['session']['id']}/seat"


def test_a_failed_check_in_frees_the_chair_and_says_why(
    tmp_path, monkeypatch, fake_docker, caddy_admin
):
    """Teardown runs for real here: it clears the chair's runtime columns, so
    recording the reason before it would erase the reason."""
    monkeypatch.setenv("BYOI_SEATS", "ondemand")

    def _no_room(*a, **k):
        raise seats.SeatError("all 4 seats are up (BYOI_MAX_SEATS)")

    monkeypatch.setattr("apps.api.seats.provision", _no_room)
    monkeypatch.setattr(seats.seat_sync, "revoke_session", lambda *a, **k: {"ok": True})
    client = TestClient(create_app(tmp_path), headers=HOST)
    sid = client.post(
        "/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}
    ).json()["session"]["id"]
    state = client.get(f"/api/sessions/{sid}/seat").json()
    assert state["state"] == "failed"
    assert "BYOI_MAX_SEATS" in state["error"]
    assert client.get("/api/seats").json()["seats"][0]["status"] == "idle"


def test_the_poll_address_carries_the_join_url_once_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_SEATS", "ondemand")
    monkeypatch.setenv("BYOI_DOMAIN", "salon.example")
    client = TestClient(create_app(tmp_path), headers=HOST)
    sid = client.post(
        "/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}
    ).json()["session"]["id"]
    # Stand in for a finished provision.
    store = Store(Path(tmp_path) / "salon.db")
    store.set_seat_runtime(
        "seat-1",
        state="ready",
        agent_url=seats.internal_url(sid),
        public_host=f"s-{sid}.salon.example",
    )
    state = client.get(f"/api/sessions/{sid}/seat").json()
    assert state["state"] == "ready"
    assert state["join"] == f"https://s-{sid}.salon.example/join?otp={state['otp']}"


# --- the guest's tree -------------------------------------------------------


def _repo(root: Path, name: str, *, origin: str | None = None) -> Path:
    """A real git project of the shape the board hands out."""
    path = root / name
    path.mkdir(parents=True)
    (path / "README.md").write_text(f"# {name}\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "first"],
        cwd=path,
        check=True,
    )
    if origin:
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
    return path


def test_the_seat_gets_its_own_workspace_not_the_projects_root(
    tmp_path, fake_docker, caddy_admin, ready_seat
):
    """Mounting the projects root would hand each guest every other guest's work."""
    store = Store(tmp_path / "salon.db")
    sess = store.check_in("seat-1", "Ada")
    seats.provision(store, sess, store.seat("seat-1"))

    log = fake_docker.read_text()
    assert f"{seats.workspace_dir(sess['id'])}:{seats.GUEST_WORKSPACE}" in log
    assert "projects" not in log


def test_seed_workspace_clones_the_project_and_leaves_it_alone(tmp_path, fake_docker):
    project = _repo(tmp_path / "projects", "todo-api")
    seat_path = seats.seed_workspace("sess-1", project)

    assert seat_path == f"{seats.GUEST_WORKSPACE}/todo-api"
    clone = seats.workspace_dir("sess-1") / "todo-api"
    assert (clone / ".git").is_dir()
    assert (clone / "README.md").read_text() == "# todo-api\n"
    # The board's copy is the thing every later visit is cut from.
    assert not (project / "guest-scratch").exists()


def test_two_visits_never_share_a_workspace(tmp_path, fake_docker):
    project = _repo(tmp_path / "projects", "todo-api")
    seats.seed_workspace("sess-1", project)
    seats.seed_workspace("sess-2", project)

    one = seats.workspace_dir("sess-1") / "todo-api"
    two = seats.workspace_dir("sess-2") / "todo-api"
    assert one != two
    (one / "mine.txt").write_text("ada")
    assert not (two / "mine.txt").exists()


def test_the_clone_points_at_the_real_origin(tmp_path, fake_docker):
    """A guest's `git push` must not aim at a path only the desk container has."""
    project = _repo(tmp_path / "projects", "todo-api", origin="https://example.com/x.git")
    seats.seed_workspace("sess-1", project)

    clone = seats.workspace_dir("sess-1") / "todo-api"
    url = subprocess.run(
        ["git", "-C", str(clone), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    assert url.stdout.strip() == "https://example.com/x.git"


def test_a_project_that_is_not_a_repo_still_reaches_the_guest(tmp_path, fake_docker):
    """`local` projects are any folder somebody pointed the desk at."""
    plain = tmp_path / "projects" / "sketch"
    plain.mkdir(parents=True)
    (plain / "index.html").write_text("<h1>hi</h1>")

    seats.seed_workspace("sess-1", plain)
    clone = seats.workspace_dir("sess-1") / "sketch"
    assert (clone / "index.html").read_text() == "<h1>hi</h1>"
    assert seats.workspace_source("sess-1", str(plain)) is None


def test_seed_workspace_reports_a_missing_project(tmp_path, fake_docker):
    with pytest.raises(seats.SeatError) as err:
        seats.seed_workspace("sess-1", tmp_path / "nope")
    assert "missing" in str(err.value)


def test_workspace_source_is_what_the_desk_grades_from(tmp_path, fake_docker):
    project = _repo(tmp_path / "projects", "todo-api")
    seats.seed_workspace("sess-1", project)

    src = seats.workspace_source("sess-1", str(project))
    assert src == seats.workspace_dir("sess-1") / "todo-api"
    assert seats.workspace_source("sess-1", None) is None


def test_claiming_in_the_cloud_sends_the_seat_its_own_path(tmp_path, monkeypatch):
    """The desk's path for a project means nothing inside a seat container.

    Regression: the desk used to hand over its own /app/data/projects/<slug>,
    which the seat could not stat, so every project-backed claim failed with
    "seat did not switch to this project's folder".
    """
    monkeypatch.setenv("BYOI_SEATS", "ondemand")
    monkeypatch.setenv("BYOI_SEAT_RUNTIME_DIR", str(tmp_path / "runtime"))
    sent: list[str] = []
    monkeypatch.setattr(
        "apps.api.seat_sync.set_workspace",
        lambda seat, path, *a, **k: (sent.append(path), {"ok": True})[1],
    )

    project = _repo(tmp_path / "projects", "todo-api")
    desk = TestClient(create_app(tmp_path), headers=HOST)
    made = desk.post(
        "/api/projects", json={"kind": "local", "path": str(project), "name": "todo-api"}
    ).json()
    brief = desk.post(
        "/api/board", json={"title": "Ship it", "brief": "b", "project_id": made["id"]}
    ).json()
    sid = desk.post(
        "/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}
    ).json()["session"]["id"]

    res = desk.post(f"/api/sessions/{sid}/claim", json={"board_id": brief["id"]})
    assert res.status_code == 200, res.text
    assert sent == [f"{seats.GUEST_WORKSPACE}/todo-api"]
    assert str(project) not in sent
    # and the guest actually has the code to work on
    assert (seats.workspace_dir(sid) / "todo-api" / "README.md").is_file()


def test_claiming_on_one_pc_still_opens_the_project_itself(tmp_path, monkeypatch):
    """Static mode shares a filesystem, so cloning would only add a copy."""
    monkeypatch.delenv("BYOI_SEATS", raising=False)
    sent: list[str] = []
    monkeypatch.setattr(
        "apps.api.seat_sync.set_workspace",
        lambda seat, path, *a, **k: (sent.append(path), {"ok": True})[1],
    )

    project = _repo(tmp_path / "projects", "todo-api")
    desk = TestClient(create_app(tmp_path), headers=HOST)
    made = desk.post(
        "/api/projects", json={"kind": "local", "path": str(project), "name": "todo-api"}
    ).json()
    brief = desk.post(
        "/api/board", json={"title": "Ship it", "brief": "b", "project_id": made["id"]}
    ).json()
    sid = desk.post(
        "/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}
    ).json()["session"]["id"]

    desk.post(f"/api/sessions/{sid}/claim", json={"board_id": brief["id"]})
    assert sent == [str(project.resolve())]


# --- the grading account is not a seat account ------------------------------


def test_the_host_account_is_never_given_to_a_seat(tmp_path, monkeypatch):
    """It grades blind, so the guest's Claude must not be running as it.

    With a two-account pool this is the default outcome, not an edge case:
    `claude-host` sorts ahead of `claude-seat-1`, so it was allocated first and
    became the seat's BYOI_CLAUDE_ACCOUNT.
    """
    accounts = tmp_path / "accounts"
    for name in ("claude-host", "claude-seat-1"):
        (accounts / name).mkdir(parents=True)
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(accounts))

    store = Store(tmp_path / "salon.db")
    sess = store.check_in("seat-1", "Ada")
    labels = seats.allocate_accounts(store, sess["id"])

    assert labels == ["claude-seat-1"]
    assert "claude-host" not in labels


def test_the_host_account_alone_is_not_a_usable_pool(tmp_path, monkeypatch):
    accounts = tmp_path / "accounts"
    (accounts / "claude-host").mkdir(parents=True)
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(accounts))

    store = Store(tmp_path / "salon.db")
    sess = store.check_in("seat-1", "Ada")
    with pytest.raises(seats.SeatError) as err:
        seats.allocate_accounts(store, sess["id"])
    assert "reserved for grading" in str(err.value)


def test_the_reserved_label_follows_the_env(tmp_path, monkeypatch):
    accounts = tmp_path / "accounts"
    for name in ("claude-host", "grader"):
        (accounts / name).mkdir(parents=True)
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(accounts))
    monkeypatch.setenv("BYOI_HOST_CLAUDE_ACCOUNT", "grader")

    store = Store(tmp_path / "salon.db")
    sess = store.check_in("seat-1", "Ada")
    assert seats.allocate_accounts(store, sess["id"]) == ["claude-host"]


def test_the_seat_never_mounts_the_grading_credentials(tmp_path, monkeypatch):
    """Allocated accounts are bind-mounted, and the guest's Claude has Bash."""
    accounts = tmp_path / "accounts"
    for name in ("claude-host", "claude-seat-1"):
        (accounts / name).mkdir(parents=True)
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(accounts))

    store = Store(tmp_path / "salon.db")
    sess = store.check_in("seat-1", "Ada")
    labels = seats.allocate_accounts(store, sess["id"])
    joined = " ".join(
        _args(monkeypatch, tmp_path, labels=labels)
    )
    assert "claude-seat-1" in joined
    assert "claude-host" not in joined


def test_the_seat_is_told_where_the_desk_is(monkeypatch, tmp_path):
    """The guest's Sit button is /api/join, proxied through the seat to the desk.

    The default house URL is 127.0.0.1:8080 — the salon PC's desk, and nothing
    at all inside a container. Left unset, every guest got "request failed".
    """
    args = _args(monkeypatch, tmp_path)
    assert "BYOI_HOUSE_URL=http://byoi-desk:8080" in args


def test_the_house_url_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("BYOI_SEAT_HOUSE_URL", "http://desk.internal:9000")
    assert "BYOI_HOUSE_URL=http://desk.internal:9000" in _args(monkeypatch, tmp_path)
