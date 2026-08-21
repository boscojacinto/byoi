import pytest

from apps.api.seat_sync import SeatSyncError
from apps.seat.gate import Gate, hash_otp, normalize_otp
from fastapi.testclient import TestClient

from apps.api.main import create_app


def test_normalize_and_hash_are_casefold():
    assert normalize_otp(" AbC123 ") == "abc123"
    assert hash_otp("AbC123") == hash_otp("abc123")


def test_gate_unlock_and_lockout():
    g = Gate(max_failures=3)
    g.admit(otp="opensesame", session_id="s1", coder_name="Ada", seat_id="seat-1")
    with pytest.raises(PermissionError, match="invalid otp"):
        g.unlock("nope")
    ticket = g.unlock("OPENSESAME")
    assert g.check_ticket(ticket)
    g.reset()
    g.admit(otp="opensesame", session_id="s1", coder_name="Ada", seat_id="seat-1")
    for _ in range(2):
        with pytest.raises(PermissionError, match="invalid otp"):
            g.unlock("nope")
    with pytest.raises(PermissionError, match="too many"):
        g.unlock("nope")
    with pytest.raises(PermissionError, match="no live OTP"):
        g.unlock("opensesame")


def test_checkin_pushes_otp_and_prints_it_in_join_url(tmp_path):
    client = TestClient(create_app(tmp_path))
    check = client.post(
        "/api/sessions/check-in",
        json={"seat_id": "seat-1", "coder_name": "Ada"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    assert check.status_code == 200
    body = check.json()
    otp = body["otp"]
    assert len(otp) >= 6
    assert f"otp={otp}" in body["join"]
    assert body["seat_admitted"] is True
    assert body["session"]["unlock_otp"] == otp


def test_checkin_rolls_back_if_seat_rejects_otp(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise SeatSyncError(502, "seat down")

    monkeypatch.setattr("apps.api.seat_sync.admit_session", boom)
    client = TestClient(create_app(tmp_path))
    check = client.post(
        "/api/sessions/check-in",
        json={"seat_id": "seat-1", "coder_name": "Ada"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    assert check.status_code == 502
    seats = client.get("/api/seats").json()["seats"]
    seat = next(s for s in seats if s["id"] == "seat-1")
    assert seat["status"] == "idle"
    assert seat["session"] is None
