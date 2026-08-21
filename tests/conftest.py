import pytest

from apps.seat.gate import gate


@pytest.fixture(autouse=True)
def _reset_seat_gate():
    gate.reset()
    yield
    gate.reset()


@pytest.fixture(autouse=True)
def _seat_sync_ok(monkeypatch):
    monkeypatch.setattr("apps.api.seat_sync.admit_session", lambda *a, **k: {"ok": True})
    monkeypatch.setattr("apps.api.seat_sync.revoke_session", lambda *a, **k: {"ok": True})
