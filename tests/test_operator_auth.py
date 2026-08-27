"""The desk is on the public internet now, so its front door has to hold."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import operator
from apps.api.main import create_app


@pytest.fixture(autouse=True)
def _reset_throttle():
    operator.throttle.reset()
    yield
    operator.throttle.reset()


@pytest.fixture
def desk(tmp_path: Path, monkeypatch) -> TestClient:
    # TestClient speaks http://testserver, and a Secure cookie is never sent
    # back over http. Production is https behind Caddy, where Secure is right —
    # test_cookie_flags_are_locked_down below holds that default.
    monkeypatch.setenv("BYOI_COOKIE_SECURE", "0")
    operator.set_password("open-sesame")
    return TestClient(create_app(tmp_path))


def test_cookie_flags_are_locked_down(monkeypatch):
    monkeypatch.delenv("BYOI_COOKIE_SECURE", raising=False)
    flags = operator.cookie_kwargs()
    assert flags["httponly"] is True
    assert flags["secure"] is True
    assert flags["samesite"] == "lax"


def test_password_round_trip(tmp_path: Path):
    operator.set_password("correct horse battery")
    assert operator.password_is_set()
    assert operator.verify_password("correct horse battery")
    assert not operator.verify_password("correct horse batter")


def test_a_short_password_is_refused(tmp_path: Path):
    with pytest.raises(operator.OperatorError):
        operator.set_password("short")


def test_verify_without_a_hash_file_is_false(tmp_path: Path):
    assert not operator.password_is_set()
    assert not operator.verify_password("anything")


def test_login_sets_a_cookie_that_opens_the_floor(desk: TestClient):
    assert desk.post("/api/board", json={"title": "x", "brief": "y"}).status_code == 401
    res = desk.post("/api/login", json={"password": "open-sesame"})
    assert res.status_code == 200
    assert operator.COOKIE_NAME in res.cookies
    posted = desk.post("/api/board", json={"title": "Steam the wand", "brief": "ritual"})
    assert posted.status_code == 200


def test_logout_closes_it_again(desk: TestClient):
    desk.post("/api/login", json={"password": "open-sesame"})
    assert desk.get("/api/session").json()["signed_in"] is True
    desk.post("/api/logout")
    assert desk.get("/api/session").json()["signed_in"] is False
    assert desk.post("/api/board", json={"title": "x", "brief": "y"}).status_code == 401


def test_wrong_password_locks_out_after_enough_tries(desk: TestClient):
    for _ in range(operator.MAX_FAILURES):
        assert desk.post("/api/login", json={"password": "nope"}).status_code == 401
    # Even the right password is refused while the lockout stands.
    res = desk.post("/api/login", json={"password": "open-sesame"})
    assert res.status_code == 429
    assert "try again" in res.json()["detail"]


def test_a_forged_cookie_is_refused(desk: TestClient):
    body, _, signature = operator.issue_cookie().partition(".")
    forged = f"{body}.{'a' * len(signature)}"
    desk.cookies.set(operator.COOKIE_NAME, forged)
    assert desk.post("/api/board", json={"title": "x", "brief": "y"}).status_code == 401


def test_an_expired_cookie_is_refused():
    old = operator.issue_cookie(now=time.time() - operator.SESSION_TTL - 1)
    assert operator.read_cookie(old) is None


def test_an_idle_cookie_is_refused():
    """Valid until its absolute deadline, but untouched for longer than the
    idle window — a desk left open in a cafe should not stay open."""
    stale = operator.issue_cookie(now=time.time() - operator.IDLE_TTL - 1)
    assert operator.read_cookie(stale) is None


def test_refresh_slides_idle_without_extending_the_deadline():
    issued = time.time() - 600
    cookie = operator.issue_cookie(now=issued)
    claims = operator.read_cookie(cookie)
    assert claims is not None and claims.stale()
    refreshed = operator.read_cookie(operator.refresh_cookie(claims))
    assert refreshed is not None
    assert refreshed.expires_at == pytest.approx(claims.expires_at)
    assert refreshed.seen_at > claims.seen_at
    assert not refreshed.stale()


def test_the_host_token_still_works_for_machine_callers(tmp_path: Path):
    """The seat, the print relay, and the desk's own tooling have no browser."""
    client = TestClient(create_app(tmp_path))
    res = client.post(
        "/api/board",
        json={"title": "x", "brief": "y"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    assert res.status_code == 200


def test_login_without_a_password_configured_says_so(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    res = client.post("/api/login", json={"password": "anything"})
    assert res.status_code == 503
    assert "salon-secrets.sh operator" in res.json()["detail"]
    assert client.get("/api/session").json()["password_set"] is False
