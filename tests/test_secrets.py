import os
from pathlib import Path

import pytest

from apps import secrets


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # conftest already blanks the real credentials; point at this test's dir.
    monkeypatch.setenv("BYOI_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("BYOI_ENV_FILE", str(tmp_path / "absent.env"))
    return tmp_path


def test_missing_secret_is_none():
    assert secrets.read_secret("BYOI_VERCEL_TOKEN") is None


def test_env_wins_over_file(tmp_path, monkeypatch):
    (tmp_path / "vercel.token").write_text("from-file")
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "from-env")
    assert secrets.read_secret("BYOI_VERCEL_TOKEN") == "from-env"


def test_file_is_used_when_the_env_is_empty(tmp_path):
    (tmp_path / "vercel.token").write_text("from-file\n")
    assert secrets.read_secret("BYOI_VERCEL_TOKEN") == "from-file"


def test_a_trailing_newline_is_not_part_of_the_token(tmp_path):
    (tmp_path / "neon.token").write_text("abc123\n\n")
    assert secrets.read_secret("BYOI_NEON_API_KEY") == "abc123"


def test_an_empty_file_counts_as_unset(tmp_path):
    (tmp_path / "vercel.token").write_text("\n")
    assert secrets.read_secret("BYOI_VERCEL_TOKEN") is None


def test_an_explicit_file_override_wins(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere.token"
    other.write_text("over-here")
    monkeypatch.setenv("BYOI_VERCEL_TOKEN_FILE", str(other))
    assert secrets.read_secret("BYOI_VERCEL_TOKEN") == "over-here"


def test_status_never_exposes_a_value(tmp_path):
    (tmp_path / "vercel.token").write_text("super-secret")
    rows = {r["name"]: r for r in secrets.status()}
    assert rows["BYOI_VERCEL_TOKEN"]["configured"] is True
    assert rows["BYOI_VERCEL_TOKEN"]["source"] == "file"
    assert "super-secret" not in str(rows)


def test_status_flags_a_world_readable_secret(tmp_path):
    path = tmp_path / "vercel.token"
    path.write_text("x")
    path.chmod(0o644)
    rows = {r["name"]: r for r in secrets.status()}
    assert rows["BYOI_VERCEL_TOKEN"]["world_readable"] is True
    path.chmod(0o600)
    rows = {r["name"]: r for r in secrets.status()}
    assert rows["BYOI_VERCEL_TOKEN"]["world_readable"] is False


def test_status_does_not_report_a_path_for_an_env_secret(monkeypatch):
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "x")
    rows = {r["name"]: r for r in secrets.status()}
    assert rows["BYOI_VERCEL_TOKEN"]["source"] == "env"
    assert rows["BYOI_VERCEL_TOKEN"]["path"] is None


def test_deploy_reads_the_token_from_a_file(tmp_path):
    from apps.api.deploy import _token

    (tmp_path / "vercel.token").write_text("tok-from-file")
    assert _token() == "tok-from-file"


def test_deploy_error_points_at_the_helper_script():
    from apps.api.deploy import DeployError, _token

    with pytest.raises(DeployError, match="salon-secrets.sh vercel"):
        _token()


def test_provisioning_reads_credentials_from_files(tmp_path):
    from apps.api import provision

    (tmp_path / "upstash.email").write_text("me@example.com")
    (tmp_path / "upstash.token").write_text("up-key")
    assert provision._token("BYOI_UPSTASH_EMAIL") == "me@example.com"
    assert provision._token("BYOI_UPSTASH_API_KEY") == "up-key"


# ------------------------------------------------------------------ .env support


def test_dotenv_is_read(tmp_path, monkeypatch):
    envf = tmp_path / "dot.env"
    envf.write_text("BYOI_VERCEL_TOKEN=from-dotenv\n")
    monkeypatch.setenv("BYOI_ENV_FILE", str(envf))
    assert secrets.read_secret("BYOI_VERCEL_TOKEN") == "from-dotenv"
    assert secrets.source_of("BYOI_VERCEL_TOKEN") == "dotenv"


def test_dotenv_parsing_handles_comments_quotes_and_export(tmp_path, monkeypatch):
    envf = tmp_path / "dot.env"
    envf.write_text(
        "# a comment\n"
        "\n"
        "export BYOI_VERCEL_TOKEN='quoted-value'\n"
        'BYOI_NEON_API_KEY="double"\n'
        "not-a-pair\n"
        "BYOI_UPSTASH_EMAIL = spaced@example.com \n"
    )
    monkeypatch.setenv("BYOI_ENV_FILE", str(envf))
    values = secrets.dotenv_values()
    assert values["BYOI_VERCEL_TOKEN"] == "quoted-value"
    assert values["BYOI_NEON_API_KEY"] == "double"
    assert values["BYOI_UPSTASH_EMAIL"] == "spaced@example.com"


def test_dotenv_missing_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_ENV_FILE", str(tmp_path / "nope.env"))
    assert secrets.dotenv_values() == {}


def test_managed_secret_beats_a_stale_dotenv(tmp_path, monkeypatch):
    """Rotating with salon-secrets.sh must not be silently undone by an old .env."""
    envf = tmp_path / "dot.env"
    envf.write_text("BYOI_VERCEL_TOKEN=stale\n")
    monkeypatch.setenv("BYOI_ENV_FILE", str(envf))
    (tmp_path / "vercel.token").write_text("rotated")
    assert secrets.read_secret("BYOI_VERCEL_TOKEN") == "rotated"


def test_env_beats_everything(tmp_path, monkeypatch):
    envf = tmp_path / "dot.env"
    envf.write_text("BYOI_VERCEL_TOKEN=stale\n")
    monkeypatch.setenv("BYOI_ENV_FILE", str(envf))
    (tmp_path / "vercel.token").write_text("managed")
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "explicit")
    assert secrets.read_secret("BYOI_VERCEL_TOKEN") == "explicit"


# ------------------------------------------------- keeping secrets off the seat


def test_scrub_removes_every_desk_only_credential():
    env = {name: "secret" for name in secrets.SECRETS}
    env["PATH"] = "/usr/bin"
    env["CLAUDE_CONFIG_DIR"] = "/x"
    cleaned = secrets.scrub(dict(env))
    assert not any(name in cleaned for name in secrets.SECRETS)
    assert cleaned["PATH"] == "/usr/bin"
    assert cleaned["CLAUDE_CONFIG_DIR"] == "/x"


def test_scrub_is_safe_when_nothing_is_set():
    assert secrets.scrub({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}


def test_the_guests_claude_never_inherits_a_deploy_token(tmp_path, monkeypatch):
    """The guest has Bash and inherits the seat env — the token must not be in it."""
    import asyncio

    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "tok-must-not-leak")
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    from apps.seat.claude_chat import ClaudeChat

    seen: dict[str, str] = {}

    async def spawn(env=None):
        seen.update(env or {})

        class Proc:
            returncode = None
            stdin = None
            stdout = asyncio.StreamReader()
            stderr = asyncio.StreamReader()

            def kill(self):
                self.returncode = -9
                self.stdout.feed_eof()
                self.stderr.feed_eof()

        return Proc()

    async def run():
        chat = ClaudeChat(spawn=spawn)
        await chat.ensure()
        chat._stop_process()

    asyncio.run(run())
    assert seen, "spawn was never called"
    assert "BYOI_VERCEL_TOKEN" not in seen
    assert "PATH" in seen  # the rest of the environment is intact
