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


def test_control_config_does_not_silence_guest_logs():
    from apps.seat.control_server import control_config

    cfg = control_config(host="127.0.0.1", port=18788)
    assert cfg.log_config is None
    assert cfg.log_level is None


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


def test_submit_requires_the_host_token(monkeypatch):
    client = TestClient(control_app)
    res = client.post("/local/submit", json={"session_id": "sid1"})
    assert res.status_code == 401


def test_submit_pins_a_ref_and_reports_the_hook(tmp_path, monkeypatch):
    import subprocess

    from apps.seat.claude_chat import session as chat_session

    repo = tmp_path / "proj"
    repo.mkdir()
    for args in (("init", "-q", "."), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    (repo / "app.py").write_text("VALUE = 42\n")

    async def fake_signal(session_id, **kwargs):
        return {"cwd": str(repo), "transcript_path": "/t.jsonl"}

    monkeypatch.setattr(chat_session, "signal_submit", fake_signal)
    client = TestClient(control_app)
    res = client.post(
        "/local/submit",
        json={"session_id": "sid1"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["hooked"] is True
    assert body["ref"] == "refs/byoi/submissions/sid1"
    assert body["transcript_path"] == "/t.jsonl"
    assert body["pushed"] is False
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", body["ref"]],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert "app.py" in listed.stdout


def test_submit_outside_a_repo_is_a_conflict(tmp_path, monkeypatch):
    from apps.seat.claude_chat import session as chat_session

    plain = tmp_path / "plain"
    plain.mkdir()

    async def fake_signal(session_id, **kwargs):
        return {"cwd": str(plain)}

    monkeypatch.setattr(chat_session, "signal_submit", fake_signal)
    client = TestClient(control_app)
    res = client.post(
        "/local/submit",
        json={"session_id": "sid1"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    assert res.status_code == 409
    assert "not a git repository" in res.json()["detail"]


def test_submit_without_a_workspace_is_a_conflict(monkeypatch):
    from apps.seat.claude_chat import session as chat_session

    async def fake_signal(session_id, **kwargs):
        return None

    monkeypatch.setattr(chat_session, "signal_submit", fake_signal)
    client = TestClient(control_app)
    res = client.post(
        "/local/submit",
        json={"session_id": "sid1"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    assert res.status_code == 409
    assert "no workspace" in res.json()["detail"]


def test_infra_endpoints_require_the_host_token():
    client = TestClient(control_app)
    assert client.post("/local/infra/up", json={"session_id": "s"}).status_code == 401
    assert client.post("/local/infra/down", json={"session_id": "s"}).status_code == 401
    assert client.get("/local/infra", params={"session_id": "s"}).status_code == 401


def test_infra_status_is_quiet_when_nothing_is_up(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_INFRA_DIR", str(tmp_path))
    client = TestClient(control_app)
    res = client.get(
        "/local/infra",
        params={"session_id": "sid-none"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    assert res.status_code == 200
    assert res.json()["up"] is False


def test_infra_up_without_a_workspace_is_a_conflict():
    client = TestClient(control_app)
    res = client.post(
        "/local/infra/up",
        json={"session_id": "sid"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    assert res.status_code == 409
    assert "no workspace" in res.json()["detail"]


def test_revoke_brings_the_local_stack_down(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_INFRA_DIR", str(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(
        "apps.seat.infra.down", lambda sid, **k: calls.append(sid) or {"ok": True, "removed": True}
    )
    client = TestClient(control_app)
    client.post(
        "/local/admit",
        json={"otp": "abc-123", "session_id": "sid-live", "seat_id": "seat-1"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    res = client.post("/local/revoke", headers={"Authorization": "Bearer byoi-host"})
    assert res.status_code == 200
    assert calls == ["sid-live"]
    assert res.json()["infra"]["removed"] is True


def test_revoke_survives_a_broken_docker(monkeypatch):
    def boom(sid, **k):
        raise RuntimeError("docker daemon is gone")

    monkeypatch.setattr("apps.seat.infra.down", boom)
    client = TestClient(control_app)
    client.post(
        "/local/admit",
        json={"otp": "abc-123", "session_id": "sid-live", "seat_id": "seat-1"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    res = client.post("/local/revoke", headers={"Authorization": "Bearer byoi-host"})
    # Freeing the seat must never depend on Docker being healthy.
    assert res.status_code == 200
    assert res.json()["admitted"] is False
    assert "docker daemon is gone" in res.json()["infra"]["detail"]


def test_submit_kind_selects_the_deploy_ref(tmp_path, monkeypatch):
    import subprocess

    from apps.seat.claude_chat import session as chat_session

    repo = tmp_path / "proj"
    repo.mkdir()
    for args in (("init", "-q", "."), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    (repo / "app.py").write_text("x\n")

    async def fake_signal(session_id, **kwargs):
        return {"cwd": str(repo)}

    monkeypatch.setattr(chat_session, "signal_submit", fake_signal)
    client = TestClient(control_app)
    res = client.post(
        "/local/submit",
        json={"session_id": "sid1", "kind": "deploy"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    assert res.json()["ref"] == "refs/byoi/deploys/sid1"


@pytest.mark.skipif(os.system("command -v openssl >/dev/null") != 0, reason="openssl required")
def test_cloud_seat_cert_carries_a_name_not_an_address(tmp_path, monkeypatch):
    """In the cloud the seat only speaks TLS to the desk, on a container name.

    Baking LAN IPs in would mean reissuing whenever an address moved — the very
    thing the salon had to do on cafe DHCP.
    """
    import subprocess

    from apps.tls import TlsPaths, issue_seat_cert

    monkeypatch.setenv("BYOI_TLS_DIR", str(tmp_path))
    monkeypatch.setenv("BYOI_GUEST_TLS", "0")
    ca = generate(tmp_path)
    out = issue_seat_cert(ca, TlsPaths(tmp_path / "seats" / "abc123"), name="byoi-seat-abc123")
    text = subprocess.check_output(
        ["openssl", "x509", "-in", str(out.seat_cert), "-noout", "-text"], text=True
    )
    assert "DNS:byoi-seat-abc123" in text
    assert "IP Address" not in text
    assert out.seat_key.is_file() and out.ca.is_file()
    # The per-seat key is its own, not a copy of the salon PC's.
    assert out.seat_key.read_bytes() != ca.seat_key.read_bytes()
