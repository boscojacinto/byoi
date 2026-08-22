import pytest
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


def test_root_is_seat_pc_ui_and_join_goes_to_guest_pwa():
    client = TestClient(app)
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 200
    assert "This table" in root.text
    assert "/seat/assets/seat.js" in root.text
    css = client.get("/seat/assets/seat.css")
    assert css.status_code == 200
    join = client.get("/join?otp=deadbeef", follow_redirects=False)
    assert join.headers["location"].startswith("/guest/")
    assert "otp=deadbeef" in join.headers["location"]
    page = client.get("/guest/", follow_redirects=False)
    assert page.status_code == 200
    assert "BYOI Guest" in page.text
    assert "manifest.json" in page.text
    manifest = client.get("/guest/manifest.json")
    assert manifest.status_code == 200
    assert manifest.json()["display"] == "standalone"


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
    assert body["chat"].startswith("/chat?ticket=")
    assert "view=chat" in body["guest"]
    assert body["term"].startswith("/term?ticket=")
    assert "tmux attach -t claude-guest" == body["tmux"]
    assert gate.check_ticket(body["ticket"]) is True
    assert gate.check_ticket("wrong") is False


def test_control_live_mirrors_chat_history():
    control = TestClient(control_app)
    control.post(
        "/local/admit",
        json={"otp": "deadbeef", "session_id": "abc", "coder_name": "Ada"},
        headers=HOST,
    )
    from apps.seat.claude_chat import session as chat_session

    chat_session._history.append({"type": "user", "text": "hello from the phone"})
    denied = control.get("/local/live")
    assert denied.status_code == 401
    res = control.get("/local/live", headers=HOST)
    assert res.status_code == 200
    body = res.json()
    assert body["gate"]["coder_name"] == "Ada"
    assert body["history"][0]["text"] == "hello from the phone"
    assert body["seat_id"]
    assert "accounts" in body
    assert "account" in body


def test_handoff_requires_ticket_then_404():
    control = TestClient(control_app)
    guest = TestClient(app)
    control.post(
        "/local/admit",
        json={"otp": "deadbeef", "session_id": "abc", "coder_name": "Ada"},
        headers=HOST,
    )
    assert guest.get("/local/handoff?ticket=nope").status_code == 403
    ticket = guest.post("/local/unlock", json={"otp": "deadbeef"}).json()["ticket"]
    assert guest.get(f"/local/handoff?ticket={ticket}").status_code == 404


def test_workspace_requires_ticket():
    client = TestClient(app)
    assert client.get("/local/workspace").status_code == 422
    assert client.get("/local/workspace?ticket=nope").status_code == 403


def test_chat_websocket_requires_ticket():
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/chat"):
            pass


def test_chat_websocket_replays_ready_snapshot():
    from apps.seat.claude_chat import session as chat_session

    control = TestClient(control_app)
    guest = TestClient(app)
    control.post(
        "/local/admit",
        json={"otp": "deadbeef", "session_id": "abc", "coder_name": "Ada"},
        headers=HOST,
    )
    ticket = guest.post("/local/unlock", json={"otp": "DEADBEEF"}).json()["ticket"]

    async def fake_attach(ws):
        await ws.send_json(chat_session.snapshot())
        await ws.close()

    chat_session.translator.model = "test-model"
    original = chat_session.attach_client
    chat_session.attach_client = fake_attach
    try:
        with guest.websocket_connect(f"/chat?ticket={ticket}") as ws:
            ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["model"] == "test-model"
        assert ready["history"] == []
    finally:
        chat_session.attach_client = original


def test_revoke_drops_otp():
    control = TestClient(control_app)
    guest = TestClient(app)
    control.post("/local/admit", json={"otp": "cafebabe", "session_id": "x"}, headers=HOST)
    control.post("/local/revoke", headers=HOST)
    assert guest.get("/local/status").json()["admitted"] is False
    assert guest.post("/local/unlock", json={"otp": "cafebabe"}).status_code == 403
