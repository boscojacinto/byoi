"""Raising and destroying a seat container per visit.

Docker and Caddy are stubbed. What is being tested is the shape of what the
desk asks for — particularly the things a seat must *not* be given, since that
is the whole reason provisioning lives on the desk.
"""

import json
import os
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

    result = seats.teardown(store, sess, store.seat("seat-1"))
    assert result["ok"], result["problems"]
    log = fake_docker.read_text()
    assert f"rm -f byoi-seat-{sess['id']}" in log
    assert f"volume rm -f byoi-workspace-{sess['id']}" in log
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
