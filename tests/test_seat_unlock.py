from fastapi.testclient import TestClient

from apps.seat.control import app as control_app
from apps.seat.gate import gate
from apps.seat.main import app, client_allowed
from apps.seat.rfcomm_tty import parse_adapter_list

HOST = {"Authorization": "Bearer byoi-host"}


def test_parse_bluetoothctl_adapter():
    text = "Controller 3C:A0:67:88:FC:12 BYOI-Seat-1 [default]\n"
    assert parse_adapter_list(text) == "3C:A0:67:88:FC:12"


def test_client_allowed_same_wifi():
    assert client_allowed("testclient") is True
    assert client_allowed("127.0.0.1") is True
    assert client_allowed("192.168.1.40") is True
    assert client_allowed("10.0.0.8") is True
    assert client_allowed("8.8.8.8") is False
    assert client_allowed(None) is False
    assert client_allowed("192.168.1.40", lan_cidr="192.168.1.0/24") is True
    assert client_allowed("192.168.2.40", lan_cidr="192.168.1.0/24") is False


def test_tty_page_served():
    client = TestClient(app)
    res = client.get("/tty")
    assert res.status_code == 200
    assert "xterm" in res.text.lower()


def test_root_redirects_to_coder():
    client = TestClient(app)
    res = client.get("/", follow_redirects=False)
    assert res.status_code in (302, 307)
    assert res.headers["location"].startswith("/coder")


def test_status_otp_gate_closed_until_admit():
    client = TestClient(app)
    status = client.get("/local/status").json()
    assert status["transport"] == "wifi"
    assert status["otp_gate"] is True
    assert status["admitted"] is False
    denied = client.post("/local/unlock", json={})
    assert denied.status_code == 403


def test_guest_http_cannot_admit():
    client = TestClient(app)
    res = client.post("/local/admit", json={"otp": "deadbeef", "session_id": "abc"}, headers=HOST)
    assert res.status_code == 404


def test_admit_unlock_issues_ticket():
    control = TestClient(control_app)
    guest = TestClient(app)
    assert control.post("/local/admit", json={"otp": "deadbeef", "session_id": "abc"}).status_code == 401
    admitted = control.post(
        "/local/admit",
        json={"otp": "deadbeef", "session_id": "abc", "coder_name": "Ada"},
        headers=HOST,
    )
    assert admitted.status_code == 200
    assert guest.get("/local/status").json()["admitted"] is True
    assert guest.post("/local/unlock", json={"otp": "nope"}).status_code == 403
    unlocked = guest.post("/local/unlock", json={"otp": "DEADBEEF"})
    assert unlocked.status_code == 200
    body = unlocked.json()
    assert body["ok"] is True
    assert body["via"] == "wifi"
    assert body["ticket"]
    assert body["term"].startswith("/term?ticket=")
    assert "tmux attach -t claude-guest" == body["tmux"]
    assert gate.check_ticket(body["ticket"]) is True
    assert gate.check_ticket("wrong") is False


def test_revoke_drops_otp():
    control = TestClient(control_app)
    guest = TestClient(app)
    control.post("/local/admit", json={"otp": "cafebabe", "session_id": "x"}, headers=HOST)
    control.post("/local/revoke", headers=HOST)
    assert guest.get("/local/status").json()["admitted"] is False
    assert guest.post("/local/unlock", json={"otp": "cafebabe"}).status_code == 403
