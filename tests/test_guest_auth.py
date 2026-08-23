"""A guest's own Claude account: isolation, hooks, billing boundary, teardown."""

import asyncio
import json
import stat
import time
from pathlib import Path

import pytest

from apps.seat import guest_auth
from apps.seat.accounts import Account, AccountPool, accounts_dir
from apps.seat.claude_chat import ClaudeChat

ROOT = Path(__file__).resolve().parents[1]
FAKE_CLAUDE = ROOT / "scripts" / "fake-claude.py"
GOOD_CODE = "good-code"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def guest_env(tmp_path, monkeypatch):
    """A throwaway runtime dir and the fake CLI, so no real login is ever attempted."""
    monkeypatch.setenv("BYOI_GUEST_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path / "pool"))
    monkeypatch.setenv("BYOI_HANDOFFS_DIR", str(tmp_path / "handoffs"))
    monkeypatch.setenv("BYOI_CLAUDE", str(FAKE_CLAUDE))
    monkeypatch.setenv("BYOI_FAKE_AUTH_CODE", GOOD_CODE)
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)
    guest_auth._LOGINS.clear()
    yield tmp_path
    for login in list(guest_auth._LOGINS.values()):
        guest_auth._close(login)
    guest_auth._LOGINS.clear()


def credentialed(label: str, root: Path) -> Account:
    path = root / label
    path.mkdir(parents=True, exist_ok=True)
    (path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "salon"}}), encoding="utf-8"
    )
    return Account(label=label, config_dir=path)


def signed_in(session_id: str = "sess-1") -> None:
    run(guest_auth.begin_login(session_id))
    run(guest_auth.submit_code(session_id, GOOD_CODE))


# --- isolation ---------------------------------------------------------------


def test_guest_dir_is_private_and_outside_the_account_pool(guest_env):
    path = guest_auth.ensure_guest_dir("sess-1")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o700, f"guest dir is {oct(mode)}, must not be readable by others"
    with pytest.raises(ValueError):
        path.resolve().relative_to(accounts_dir())


def test_the_pool_never_hands_out_a_guest_account(guest_env):
    """A guest dir under accounts_dir() could be picked for the *next* guest."""
    guest_auth.ensure_guest_dir("sess-1")
    credentialed("claude-seat-1", accounts_dir())
    labels = [a.label for a in AccountPool().discover()]
    assert labels == ["claude-seat-1"]
    assert guest_auth.GUEST_LABEL not in labels


def test_login_env_drops_desk_credentials_and_carries_the_scope(guest_env, monkeypatch):
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "super-secret")
    monkeypatch.setenv("BYOI_NEON_API_KEY", "also-secret")
    env = guest_auth.login_env(guest_auth.guest_dir("sess-1"))
    assert "BYOI_VERCEL_TOKEN" not in env
    assert "BYOI_NEON_API_KEY" not in env
    # Passed through, but `claude auth login` ignores it — see test_scope_is_not_narrowed.
    assert env["CLAUDE_CODE_OAUTH_SCOPES"] == "user:inference"
    # A browser here would sign in on the seat's own screen, or complete a
    # localhost callback without the guest ever touching it.
    assert "DISPLAY" not in env


def test_scope_is_overridable(guest_env, monkeypatch):
    monkeypatch.setenv("BYOI_GUEST_OAUTH_SCOPES", "user:inference user:profile")
    assert guest_auth.oauth_scopes() == "user:inference user:profile"


def test_scopes_are_read_from_the_url_not_from_what_we_asked_for(guest_env):
    """Verified against the real CLI: `auth login` ignores CLAUDE_CODE_OAUTH_SCOPES
    and always requests the full set. Reporting the env value would tell the guest
    their token is narrower than it is."""
    real = (
        "https://claude.com/cai/oauth/authorize?code=true&client_id=x&"
        "scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference+"
        "user%3Asessions%3Aclaude_code+user%3Amcp_servers+user%3Afile_upload&state=y"
    )
    scopes = guest_auth.requested_scopes(real)
    assert "org:create_api_key" in scopes
    assert scopes != [guest_auth.DEFAULT_SCOPES]


def test_the_guest_is_told_what_the_token_can_do(guest_env):
    powers = guest_auth.scope_powers(
        ["org:create_api_key", "user:profile", "user:inference", "user:file_upload"]
    )
    assert "create an API key on your account" in powers
    assert "read your profile" in powers
    # user:inference is the expected one; it does not need calling out.
    assert len(powers) == 3


def test_granted_scopes_come_from_the_issued_token(guest_env):
    config = guest_auth.ensure_guest_dir("sess-1")
    (config / ".credentials.json").write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "t", "scopes": ["user:inference", "user:profile"]}}
        ),
        encoding="utf-8",
    )
    assert guest_auth.granted_scopes(config) == ["user:inference", "user:profile"]
    assert guest_auth.granted_scopes(guest_auth.guest_dir("missing")) == []


# --- hooks (grading depends on these) ----------------------------------------


def test_a_guest_account_still_gets_the_seat_hooks(guest_env):
    """Without hooks the seat loses quota tracking and `I'm done` stops working."""
    session = ClaudeChat()
    session.assign_account(guest_auth.guest_account("sess-1"))
    config = session.config_dir
    assert session.byo is True
    assert (config / "settings.json").is_file()
    assert (config / "byoi-submit.sh").is_file()
    settings = json.loads((config / "settings.json").read_text(encoding="utf-8"))
    assert "UserPromptSubmit" in settings["hooks"]
    assert settings["statusLine"]["type"] == "command"


def test_the_operators_own_config_is_never_written_to(guest_env, tmp_path):
    """The pool falls back to ~/.claude; salon hooks must not land there."""
    elsewhere = tmp_path / "home-claude"
    elsewhere.mkdir()
    session = ClaudeChat()
    session.assign_account(Account(label="default", config_dir=elsewhere))
    assert session.byo is False
    assert not (elsewhere / "settings.json").exists()
    assert not (elsewhere / "byoi-submit.sh").exists()


def test_a_salon_account_is_not_marked_byo(guest_env):
    session = ClaudeChat()
    session.assign_account(credentialed("claude-seat-1", accounts_dir()))
    assert session.byo is False
    assert (session.config_dir / "settings.json").is_file()


# --- the billing boundary ----------------------------------------------------


def test_a_byo_session_never_fails_over_onto_a_salon_account(guest_env):
    """Their limit must not silently move their work onto the salon's billing."""
    credentialed("claude-seat-1", accounts_dir())
    credentialed("claude-seat-2", accounts_dir())
    session = ClaudeChat()
    session.assign_account(guest_auth.guest_account("sess-1"))
    events = [{"type": "error", "message": "You hit your usage limit · resets 3pm"}]
    plan = session.failover_plan(events)
    assert plan["action"] == "no_spare"
    assert plan.get("account") is None


def test_a_salon_session_still_fails_over(guest_env):
    credentialed("claude-seat-2", accounts_dir())
    session = ClaudeChat()
    session.assign_account(credentialed("claude-seat-1", accounts_dir()))
    events = [{"type": "error", "message": "You hit your usage limit · resets 3pm"}]
    plan = session.failover_plan(events)
    assert plan["action"] == "switch"
    assert plan["account"].label == "claude-seat-2"


# --- the relayed login -------------------------------------------------------


def test_login_relays_a_url_and_the_code_completes_it(guest_env):
    started = run(guest_auth.begin_login("sess-1"))
    assert started["auth_url"].startswith("https://")
    assert "oauth" in started["auth_url"]
    assert started["scopes"] == ["user:inference"]

    done = run(guest_auth.submit_code("sess-1", GOOD_CODE))
    assert done["ok"] is True
    assert done["email"] == "guest@example.test"
    assert guest_auth.credentials_ready(guest_auth.guest_dir("sess-1"))

    written = json.loads(
        (guest_auth.guest_dir("sess-1") / ".credentials.json").read_text(encoding="utf-8")
    )
    assert written["claudeAiOauth"]["scopes"] == ["user:inference"]


def test_a_wrong_code_is_refused_and_leaves_no_credential(guest_env):
    run(guest_auth.begin_login("sess-1"))
    with pytest.raises(RuntimeError):
        run(guest_auth.submit_code("sess-1", "not-the-code"))
    assert not guest_auth.credentials_ready(guest_auth.guest_dir("sess-1"))
    assert not guest_auth.pending("sess-1")


def test_a_code_with_no_login_waiting_is_a_lookup_error(guest_env):
    with pytest.raises(LookupError):
        run(guest_auth.submit_code("sess-1", GOOD_CODE))


def test_the_login_window_is_configurable(guest_env, monkeypatch):
    """Five minutes was measured too tight for sign-in plus 2FA on a phone."""
    assert guest_auth.login_timeout() == 600.0
    monkeypatch.setenv("BYOI_GUEST_LOGIN_TIMEOUT", "900")
    assert guest_auth.login_timeout() == 900.0
    monkeypatch.setenv("BYOI_GUEST_LOGIN_TIMEOUT", "not-a-number")
    assert guest_auth.login_timeout() == guest_auth.LOGIN_TIMEOUT


def test_an_abandoned_login_is_swept(guest_env):
    run(guest_auth.begin_login("sess-1"))
    assert guest_auth.pending("sess-1")
    dropped = guest_auth.sweep(now=time.time() + guest_auth.login_timeout() + 1)
    assert dropped == ["sess-1"]
    assert not guest_auth.pending("sess-1")


# --- teardown ----------------------------------------------------------------


def test_teardown_revokes_before_it_unlinks(guest_env, monkeypatch):
    """Unlinking first would leave a live refresh token in Anthropic's records."""
    order: list[str] = []
    real_logout = guest_auth._logout
    real_wipe = guest_auth._wipe

    async def spy_logout(config_dir):
        order.append("logout")
        return await real_logout(config_dir)

    def spy_wipe(path):
        order.append("wipe")
        return real_wipe(path)

    signed_in("sess-1")
    monkeypatch.setattr(guest_auth, "_logout", spy_logout)
    monkeypatch.setattr(guest_auth, "_wipe", spy_wipe)
    result = run(guest_auth.teardown("sess-1"))

    assert order == ["logout", "wipe"]
    assert result["revoked"] is True
    assert result["removed"] is True
    assert not guest_auth.guest_dir("sess-1").exists()


def test_teardown_is_idempotent(guest_env):
    signed_in("sess-1")
    run(guest_auth.teardown("sess-1"))
    again = run(guest_auth.teardown("sess-1"))
    assert again["removed"] is False
    assert "error" not in again


def test_teardown_never_raises_so_a_seat_can_always_be_freed(guest_env, monkeypatch):
    signed_in("sess-1")

    async def boom(config_dir):
        raise OSError("logout exploded")

    monkeypatch.setattr(guest_auth, "_logout", boom)
    result = run(guest_auth.teardown("sess-1"))
    assert "error" in result
    assert result["revoked"] is False


def test_teardown_also_drops_the_on_disk_handoff(guest_env):
    from apps.seat.accounts import handoffs_dir, write_handoff

    write_handoff("sess-1", "# what the guest was working on")
    assert (handoffs_dir() / "sess-1.md").is_file()
    result = run(guest_auth.teardown("sess-1"))
    assert result["handoff"] is True
    assert not (handoffs_dir() / "sess-1.md").exists()


def test_teardown_sync_works_without_a_running_loop(guest_env):
    guest_auth.ensure_guest_dir("sess-1")
    result = guest_auth.teardown_sync("sess-1")
    assert result["removed"] is True


# --- the routes --------------------------------------------------------------


def seat_client(session_id: str = "sess-1"):
    from fastapi.testclient import TestClient

    from apps.seat.gate import gate
    from apps.seat.main import app

    gate.admit(otp="123456", session_id=session_id, coder_name="Ada", seat_id="seat-1")
    ticket = gate.unlock("123456")
    return TestClient(app), ticket


def test_byo_routes_refuse_a_guest_without_a_ticket(guest_env):
    client, _ = seat_client()
    for path in ("/local/byo/start", "/local/byo/code", "/local/byo/cancel"):
        res = client.post(path, json={})
        assert res.status_code == 403, f"{path} let an unticketed caller through"


def test_byo_start_and_code_move_the_seat_onto_the_guest_account(guest_env):
    from apps.seat.claude_chat import session as chat_session

    client, ticket = seat_client()
    started = client.post("/local/byo/start", json={"ticket": ticket})
    assert started.status_code == 200
    assert "oauth" in started.json()["auth_url"]

    done = client.post("/local/byo/code", json={"ticket": ticket, "code": GOOD_CODE})
    assert done.status_code == 200
    assert done.json()["byo"] is True
    assert chat_session.byo is True
    assert chat_session.config_dir == guest_auth.guest_dir("sess-1")


def test_byo_cancel_returns_the_seat_to_a_salon_account(guest_env):
    from apps.seat.claude_chat import session as chat_session

    credentialed("claude-seat-1", accounts_dir())
    client, ticket = seat_client()
    client.post("/local/byo/start", json={"ticket": ticket})
    client.post("/local/byo/code", json={"ticket": ticket, "code": GOOD_CODE})

    res = client.post("/local/byo/cancel", json={"ticket": ticket})
    assert res.status_code == 200
    body = res.json()
    assert body["byo"] is False
    assert body["account"] == "claude-seat-1"
    assert body["removed"] is True
    assert chat_session.byo is False
    assert not guest_auth.guest_dir("sess-1").exists()


def test_a_bad_code_reaches_the_guest_as_an_error_not_a_crash(guest_env):
    client, ticket = seat_client()
    client.post("/local/byo/start", json={"ticket": ticket})
    res = client.post("/local/byo/code", json={"ticket": ticket, "code": "nope"})
    assert res.status_code == 502
    assert "not accepted" in res.json()["detail"]


def test_freeing_the_seat_revokes_and_erases_the_guest_account(guest_env):
    """/local/revoke is the checkout path — it must take the credential with it."""
    from fastapi.testclient import TestClient

    from apps.seat.control import app as control_app
    from apps.seat.gate import gate

    client, ticket = seat_client()
    client.post("/local/byo/start", json={"ticket": ticket})
    client.post("/local/byo/code", json={"ticket": ticket, "code": GOOD_CODE})
    assert guest_auth.credentials_ready(guest_auth.guest_dir("sess-1"))

    control = TestClient(control_app)
    res = control.post("/local/revoke", headers={"Authorization": "Bearer byoi-host"})
    assert res.status_code == 200
    assert res.json()["byo"]["revoked"] is True
    assert not guest_auth.guest_dir("sess-1").exists()
    assert gate.snapshot()["admitted"] is False


# --- storage honesty ---------------------------------------------------------


def test_a_non_tmpfs_fallback_is_reported_not_hidden(guest_env, monkeypatch):
    monkeypatch.setattr(guest_auth, "_runtime_root", lambda: None)
    notes = guest_auth.storage_warnings()
    assert any("fall back" in note for note in notes)


def test_credentials_ready_is_false_after_a_logout_blanks_the_file(guest_env):
    path = guest_auth.ensure_guest_dir("sess-1") / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "live"}}), encoding="utf-8")
    assert guest_auth.credentials_ready(path.parent)
    path.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}),
        encoding="utf-8",
    )
    assert not guest_auth.credentials_ready(path.parent)
