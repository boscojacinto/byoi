import os
from pathlib import Path

import pytest

from apps import secrets


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_SECRETS_DIR", str(tmp_path))
    for name in secrets.SECRETS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
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
