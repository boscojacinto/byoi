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


# ------------------------------------------------------- deployment protection


def test_project_info_reads_what_vercel_wrote(tmp_path):
    (tmp_path / ".vercel").mkdir()
    (tmp_path / ".vercel" / "project.json").write_text('{"projectId":"prj_1","orgId":"org_1"}')
    assert deploy.project_info(tmp_path) == {"projectId": "prj_1", "orgId": "org_1"}


def test_project_info_tolerates_a_missing_file(tmp_path):
    assert deploy.project_info(tmp_path) == {}


def test_make_public_turns_off_both_protections(monkeypatch):
    seen: dict = {}

    class Res:
        status_code = 200
        text = ""

    def fake(method, path, *, token, json_body=None):
        seen.update({"method": method, "path": path, "body": json_body})
        return Res()

    monkeypatch.setattr(deploy, "_api", fake)
    assert deploy.make_public("prj_1", token="t") is None
    assert seen["method"] == "PATCH"
    assert seen["path"] == "/v9/projects/prj_1"
    # Previews are SSO-gated by default; the guest must be able to open their own URL.
    assert seen["body"] == {"ssoProtection": None, "passwordProtection": None}


def test_make_public_reports_rather_than_raises(monkeypatch):
    class Res:
        status_code = 403
        text = "forbidden"

    monkeypatch.setattr(deploy, "_api", lambda *a, **k: Res())
    note = deploy.make_public("prj_1", token="t")
    assert note and "could not disable deployment protection" in note


def test_make_public_without_a_project_id_is_a_note_not_a_crash():
    assert "no project id" in (deploy.make_public("", token="t") or "")


def test_deploy_records_the_project_and_unprotects_it(tmp_path, monkeypatch):
    fake = _fake_bin(tmp_path, FAKE_VERCEL)
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "tok")
    monkeypatch.setenv("BYOI_VERCEL", str(fake))
    monkeypatch.setenv("BYOI_DEPLOY_RUNS_DIR", str(tmp_path / "runs"))
    src = _repo(tmp_path / "proj", {"byoi.json": '{"framework":"nextjs","needs":[]}'})
    from apps.seat.submission import capture

    info = capture(cwd=src, session_id="sid", kind="deploy")
    # Stand in for what the real CLI writes after a deploy.
    dest = tmp_path / "runs" / "sid"
    monkeypatch.setattr(
        deploy, "project_info", lambda d: {"projectId": "prj_9", "orgId": "org_9"}
    )
    unprotected: list[str] = []
    monkeypatch.setattr(
        deploy, "make_public", lambda pid, token: unprotected.append(pid) or None
    )
    result = deploy.run(session_id="sid", source=info["toplevel"], ref=info["ref"])
    assert unprotected == ["prj_9"]
    # The project id/org id come back on the result so the caller can persist
    # them onto the desk project — but nothing in `resources` marks the
    # project itself for teardown, since it now outlives this one deploy.
    assert result["vercel_project_id"] == "prj_9"
    assert result["vercel_org_id"] == "org_9"
    assert result["resources"] == []


def test_deploy_reuses_a_known_vercel_project(tmp_path, monkeypatch):
    fake = _fake_bin(tmp_path, FAKE_VERCEL)
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "tok")
    monkeypatch.setenv("BYOI_VERCEL", str(fake))
    monkeypatch.setenv("BYOI_DEPLOY_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("BYOI_VERCEL_PUBLIC", "0")
    src = _repo(tmp_path / "proj", {"byoi.json": '{"framework":"nextjs","needs":[]}'})
    from apps.seat.submission import capture

    info = capture(cwd=src, session_id="sid", kind="deploy")
    result = deploy.run(
        session_id="sid",
        source=info["toplevel"],
        ref=info["ref"],
        vercel_project_id="prj_known",
        vercel_org_id="org_known",
    )
    # .vercel/project.json is pre-seeded before `vercel deploy` runs, so a
    # second solution on the same desk project lands on the same Vercel
    # project instead of the CLI minting a new one.
    dest = tmp_path / "runs" / "sid"
    assert json.loads((dest / ".vercel" / "project.json").read_text()) == {
        "projectId": "prj_known",
        "orgId": "org_known",
    }
    assert result["vercel_project_id"] == "prj_known"
    assert result["vercel_org_id"] == "org_known"


def test_public_can_be_switched_off(tmp_path, monkeypatch):
    fake = _fake_bin(tmp_path, FAKE_VERCEL)
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "tok")
    monkeypatch.setenv("BYOI_VERCEL", str(fake))
    monkeypatch.setenv("BYOI_DEPLOY_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("BYOI_VERCEL_PUBLIC", "0")
    src = _repo(tmp_path / "proj", {"byoi.json": '{"framework":"nextjs","needs":[]}'})
    from apps.seat.submission import capture

    info = capture(cwd=src, session_id="sid", kind="deploy")
    monkeypatch.setattr(deploy, "project_info", lambda d: {"projectId": "prj_9"})
    called: list[str] = []
    monkeypatch.setattr(deploy, "make_public", lambda pid, token: called.append(pid))
    deploy.run(session_id="sid", source=info["toplevel"], ref=info["ref"])
    assert called == []


def test_teardown_removes_the_deployment_but_leaves_the_shared_project(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_VERCEL_TOKEN", "tok")
    monkeypatch.setenv("BYOI_VERCEL", str(_fake_bin(tmp_path, FAKE_VERCEL)))
    out = deploy.teardown(
        {"url": "https://x.vercel.app", "resources": [{"kind": "vercel_project", "id": "prj_9"}]}
    )
    assert out["ok"] is True
    argv = (tmp_path / "argv.log").read_text()
    assert "remove https://x.vercel.app" in argv
    # No project-delete call: the desk project's Vercel project is shared
    # across guests now, so freeing a seat must not take it down.
    assert "prj_9" not in argv
