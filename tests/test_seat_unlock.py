from fastapi.testclient import TestClient

from apps.seat.main import app, client_allowed
from apps.seat.rfcomm_tty import parse_adapter_list


def test_parse_bluetoothctl_adapter():
    text = "Controller 3C:A0:67:88:FC:12 BYOI-Seat-1 [default]\n"
    assert parse_adapter_list(text) == "3C:A0:67:88:FC:12"


def test_client_allowed_same_wifi():
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


def test_status_and_dev_unlock():
    client = TestClient(app)
    status = client.get("/local/status").json()
    assert status["transport"] == "wifi"
    assert status["wifi"] is True
    assert "rfcomm" not in status
    unlocked = client.post("/local/unlock", json={})
    assert unlocked.status_code == 200
    body = unlocked.json()
    assert body["ok"] is True
    assert body["via"] == "wifi"
    assert "rfcomm" not in body
    assert "tmux attach -t claude-guest" == body["tmux"]
    assert "rc_url" not in body
    assert body["ssh"].startswith("ssh guest@")
    assert "192.168.44.1" not in body["ssh"]
