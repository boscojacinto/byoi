import os
import socket
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.seat_sync import control_base
from apps.seat.control import app as control_app
from apps.seat.control_server import _QuietServer, control_config
from apps.tls import generate, host_ssl_context

HOST = {"Authorization": "Bearer byoi-host"}


@pytest.mark.skipif(os.system("command -v openssl >/dev/null") != 0, reason="openssl required")
def test_seat_cert_lists_loopback_ip(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_TLS_DIR", str(tmp_path))
    p = generate(tmp_path)
    text = __import__("subprocess").check_output(
        ["openssl", "x509", "-in", str(p.seat_cert), "-noout", "-text"], text=True
    )
    assert "IP Address:127.0.0.1" in text or "IP:127.0.0.1" in text


def test_control_base_is_https_8788():
    assert control_base({"agent_url": "http://10.1.2.3:8787"}) == "https://10.1.2.3:8788"
    assert control_base({"agent_url": "http://127.0.0.1:8787"}) == "https://127.0.0.1:8788"


def test_control_base_override_adds_https(monkeypatch):
    monkeypatch.setenv("BYOI_SEAT_CONTROL_URL", "10.9.9.9:8788")
    assert control_base({}) == "https://10.9.9.9:8788"


def test_host_ips_lock(monkeypatch):
    monkeypatch.setenv("BYOI_HOST_IPS", "203.0.113.9")
    client = TestClient(control_app)
    # TestClient presents as testclient / 127.0.0.1, which the allowlist still accepts as loopback.
    res = client.post("/local/admit", json={"otp": "abcdef", "session_id": "s"}, headers=HOST)
    assert res.status_code == 200


def test_weak_token_rejected_when_defaults_disallowed(monkeypatch):
    monkeypatch.setattr("apps.seat.control.token_is_weak", lambda: True)
    client = TestClient(control_app)
    res = client.post("/local/admit", json={"otp": "abcdef", "session_id": "s"}, headers=HOST)
    assert res.status_code == 503


@pytest.mark.skipif(os.system("command -v openssl >/dev/null") != 0, reason="openssl required")
def test_mtls_required_for_admit(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_TLS_DIR", str(tmp_path))
    p = generate(tmp_path)
    token = p.token.read_text().strip()
    monkeypatch.setenv("BYOI_HOST_TOKEN", token)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = _QuietServer(control_config(host="127.0.0.1", port=port))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"https://127.0.0.1:{port}/local/admit"
    payload = {"otp": "abcdef01", "session_id": "sess-1"}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        for _ in range(50):
            if server.started:
                break
            time.sleep(0.05)
        else:
            pytest.fail("control server did not start")
        with httpx.Client(verify=host_ssl_context(), timeout=5.0) as client:
            res = client.post(url, json=payload, headers=headers)
        assert res.status_code == 200
        with pytest.raises(httpx.TransportError):
            httpx.post(url, json=payload, headers=headers, verify=False, timeout=5.0)
        import ssl

        ca_only = ssl.create_default_context(cafile=str(p.ca))
        with pytest.raises(httpx.TransportError):
            httpx.post(url, json=payload, headers=headers, verify=ca_only, timeout=5.0)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
