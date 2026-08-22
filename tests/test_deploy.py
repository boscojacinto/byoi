import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import deploy, provision
from apps.api.deploy import DeployError
from apps.api.main import create_app

FAKE_VERCEL = """#!/bin/sh
echo "$@" >> "$(dirname "$0")/argv.log"
case "$1" in
  deploy) echo "https://byoi-preview-abc.vercel.app" ;;
  remove) echo "Removed 1 deployment." ;;
  *) exit 1 ;;
esac
"""

# Leaks the token value ($4 in `deploy --yes --token <tok> ...`) into its error,
# the way a real CLI can when it echoes the command it ran.
FAILING_VERCEL = """#!/bin/sh
echo "Error: build failed, ran with token $4" >&2
exit 1
"""


def _fake_bin(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "vercel"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    for args in (("init", "-q", "."), ("config", "user.email", "t@t"),
                 ("config", "user.name", "t"), ("add", "-A"), ("commit", "-qm", "x")):
        subprocess.run(["git", *args], cwd=str(path), check=True, capture_output=True)
    return path


# ------------------------------------------------------------------ credentials


def test_deploy_without_a_token_is_a_precondition_error(monkeypatch):
    monkeypatch.delenv("BYOI_VERCEL_TOKEN", raising=False)
    with pytest.raises(DeployError, match="no Vercel token"):
        deploy._token()


def test_the_token_never_reaches_the_child_environment(monkeypatch):
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "tok")
    monkeypatch.setenv("BYOI_HOST_TOKEN", "salon-secret")
    env = deploy._clean_env()
    assert "BYOI_VERCEL_TOKEN" not in env
    assert "BYOI_HOST_TOKEN" not in env
    assert set(env) == {"PATH", "HOME", "LANG", "VERCEL_TELEMETRY_DISABLED"}


def test_failures_are_redacted_before_they_are_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "tok-secret")
    monkeypatch.setenv("BYOI_VERCEL", str(_fake_bin(tmp_path, FAILING_VERCEL)))
    monkeypatch.setenv("BYOI_DEPLOY_RUNS_DIR", str(tmp_path / "runs"))
    src = _repo(tmp_path / "proj", {"byoi.json": '{"framework":"nextjs","needs":[]}'})
    from apps.seat.submission import capture

    info = capture(cwd=src, session_id="sid", kind="deploy")
    with pytest.raises(DeployError) as err:
        deploy.run(session_id="sid", source=info["toplevel"], ref=info["ref"])
    assert "tok-secret" not in str(err.value)
    assert "***" in str(err.value)


# ------------------------------------------------------------------ deploy argv


def test_env_is_injected_at_build_and_runtime(tmp_path):
    argv = deploy.deploy_argv(tmp_path, {"DATABASE_URL": "postgres://x"}, token="t")
    assert "--build-env" in argv and "--env" in argv
    assert argv.count("DATABASE_URL=postgres://x") == 2
    assert "--target=preview" in argv


def test_production_flag_switches_target(tmp_path):
    argv = deploy.deploy_argv(tmp_path, {}, token="t", production=True)
    assert "--prod" in argv
    assert "--target=preview" not in argv


# --------------------------------------------------------------------- pipeline


def test_deploy_end_to_end(tmp_path, monkeypatch):
    fake = _fake_bin(tmp_path, FAKE_VERCEL)
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "tok")
    monkeypatch.setenv("BYOI_VERCEL", str(fake))
    monkeypatch.setenv("BYOI_DEPLOY_RUNS_DIR", str(tmp_path / "runs"))
    src = _repo(tmp_path / "proj", {
        "byoi.json": '{"framework":"nextjs","needs":["auth"]}',
        "app/page.tsx": "export default function P(){return null}",
    })
    from apps.seat.submission import capture

    info = capture(cwd=src, session_id="sid", kind="deploy")
    assert info["ref"] == "refs/byoi/deploys/sid"

    result = deploy.run(session_id="sid", source=info["toplevel"], ref=info["ref"])
    assert result["url"] == "https://byoi-preview-abc.vercel.app"
    assert result["framework"] == "nextjs"
    assert [r["kind"] for r in result["resources"]] == ["auth"]
    # The guest's file came along with the ref.
    assert (tmp_path / "runs" / "sid" / "app" / "page.tsx").is_file()


def test_a_non_deployable_project_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "tok")
    monkeypatch.setenv("BYOI_VERCEL", str(_fake_bin(tmp_path, FAKE_VERCEL)))
    monkeypatch.setenv("BYOI_DEPLOY_RUNS_DIR", str(tmp_path / "runs"))
    src = _repo(tmp_path / "pyproj", {"pyproject.toml": "[project]\nname='x'\n"})
    from apps.seat.submission import capture

    info = capture(cwd=src, session_id="sid", kind="deploy")
    with pytest.raises(DeployError, match="does not look deployable"):
        deploy.run(session_id="sid", source=info["toplevel"], ref=info["ref"])


def test_teardown_removes_the_deployment(tmp_path, monkeypatch):
    fake = _fake_bin(tmp_path, FAKE_VERCEL)
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "tok")
    monkeypatch.setenv("BYOI_VERCEL", str(fake))
    out = deploy.teardown({"url": "https://x.vercel.app", "resources": []})
    assert out["ok"] is True
    assert "remove https://x.vercel.app" in (tmp_path / "argv.log").read_text()


def test_teardown_without_a_token_reports_rather_than_raises(monkeypatch):
    monkeypatch.delenv("BYOI_VERCEL_TOKEN", raising=False)
    out = deploy.teardown({"url": "https://x.vercel.app", "resources": []})
    assert out["ok"] is False
    assert "no Vercel token" in out["problems"][0]


# ----------------------------------------------------------------- provisioning


def test_provision_degrades_without_credentials(monkeypatch):
    for name in ("BYOI_NEON_API_KEY", "BYOI_UPSTASH_EMAIL", "BYOI_UPSTASH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    resources, notes = provision.provision(session_id="sid", needs=["postgres", "redis", "auth"])
    # Auth needs no vendor, so it is always available.
    assert [r["kind"] for r in resources] == ["auth"]
    assert any("postgres" in n for n in notes)
    assert any("redis" in n for n in notes)


def test_auth_secret_is_unique_per_session():
    a = provision.provision_auth("sid1")["env"]["AUTH_SECRET"]
    b = provision.provision_auth("sid1")["env"]["AUTH_SECRET"]
    assert a != b


def test_resource_name_is_safe():
    assert provision.resource_name("AB/cd-12") == "byoi-abcd12"
    assert provision.resource_name("") == "byoi-seat"


def test_destroy_is_best_effort(monkeypatch):
    def boom(_r):
        raise RuntimeError("api down")

    monkeypatch.setitem(provision.DESTROYERS, "postgres", boom)
    problems = provision.destroy([{"kind": "postgres", "id": "x"}, {"kind": "auth"}])
    assert problems == ["postgres: api down"]
