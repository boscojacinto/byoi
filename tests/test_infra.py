import json
from pathlib import Path

import pytest

from apps.seat import infra


@pytest.fixture(autouse=True)
def _isolate_infra(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_INFRA_DIR", str(tmp_path / "infra"))


def test_compose_project_is_sanitised():
    assert infra.compose_project("ab/CD 1") == "byoi-abcd1"
    assert infra.compose_project("") == "byoi-seat"


def test_env_file_keeps_the_guests_own_vars(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / infra.ENV_FILE).write_text("MY_OWN=1\n")
    infra.write_env_file(proj, {"DATABASE_URL": "postgres://x", "_db_password": "secret"})
    body = (proj / infra.ENV_FILE).read_text()
    assert "MY_OWN=1" in body
    assert "DATABASE_URL=postgres://x" in body
    # Bookkeeping keys never reach the project.
    assert "_db_password" not in body


def test_env_file_replaces_only_the_managed_block(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    infra.write_env_file(proj, {"DATABASE_URL": "postgres://one"})
    (proj / infra.ENV_FILE).write_text(
        "AHEAD=1\n" + (proj / infra.ENV_FILE).read_text() + "BEHIND=1\n"
    )
    infra.write_env_file(proj, {"DATABASE_URL": "postgres://two"})
    body = (proj / infra.ENV_FILE).read_text()
    assert body.count(infra.BYOI_BLOCK_START) == 1
    assert "postgres://one" not in body
    assert "postgres://two" in body
    assert "AHEAD=1" in body and "BEHIND=1" in body


def test_public_env_drops_bookkeeping():
    out = infra.public_env({"A": "1", "_secret": "2"})
    assert out == {"A": "1"}


def test_status_without_a_stack_is_quiet():
    assert infra.status("sid-none") == {"up": False, "services": [], "env": {}}


def test_down_without_a_stack_is_a_noop():
    assert infra.down("sid-none") == {"ok": True, "removed": False}


def test_up_rejects_a_missing_project_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(infra, "_docker_ok", lambda: None)
    with pytest.raises(infra.InfraError, match="not a directory"):
        infra.up(session_id="sid", cwd=tmp_path / "nope")


def test_env_for_tolerates_garbage(tmp_path: Path):
    dest = infra.state_dir("sid-junk")
    (dest / "env.json").write_text("not json")
    assert infra.env_for("sid-junk") is None
