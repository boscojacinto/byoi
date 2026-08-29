import os

import pytest

from apps.api import seed_board
from apps.api.seat_sync import SeatSyncError
from apps.seat.claude_chat import session as chat_session
from apps.seat.gate import gate


@pytest.fixture(autouse=True)
def _reset_seat_gate():
    gate.reset()
    chat_session.reset()
    chat_session.workspace_path = None
    chat_session.account_label = None
    chat_session.config_dir = None
    chat_session.byo = False
    yield
    gate.reset()
    chat_session.reset()
    chat_session.workspace_path = None
    chat_session.account_label = None
    chat_session.config_dir = None
    chat_session.byo = False


@pytest.fixture(autouse=True)
def _seat_sync_ok(monkeypatch):
    monkeypatch.setattr("apps.api.seat_sync.admit_session", lambda *a, **k: {"ok": True})
    monkeypatch.setattr("apps.api.seat_sync.revoke_session", lambda *a, **k: {"ok": True})
    monkeypatch.setattr("apps.api.seat_sync.set_workspace", lambda *a, **k: {"ok": True})
    def _no_submit(*a, **k):
        # Default: no host pipeline, so _verify_job falls back to the seat verifier.
        raise SeatSyncError(503, "seat submit stubbed off")

    monkeypatch.setattr("apps.api.seat_sync.submit_solution", _no_submit)
    monkeypatch.setattr(
        "apps.api.seat_sync.verify_solution",
        lambda *a, **k: {"summary": "stub", "passed": 0, "failed": 0, "cases": []},
    )
    monkeypatch.setattr(
        "apps.api.seat_sync.live_snapshot",
        lambda *a, **k: {"history": [], "busy": False, "cwd": "/tmp", "model": ""},
    )
    monkeypatch.setattr(
        "apps.api.seat_sync.list_accounts",
        lambda *a, **k: {"accounts": [], "account": None, "quota": None, "handoff": False},
    )


@pytest.fixture(autouse=True)
def _isolate_deploy_credentials(tmp_path_factory, monkeypatch):
    """No test may see the operator's real credentials.

    Without this a test can reach a live provider API with a real token, and a
    failing assertion can print the secret into the test log.
    """
    from apps.secrets import SECRETS

    empty = tmp_path_factory.mktemp("secrets")
    monkeypatch.setenv("BYOI_SECRETS_DIR", str(empty))
    monkeypatch.setenv("BYOI_ENV_FILE", str(empty / "absent.env"))
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)


@pytest.fixture(autouse=True)
def _isolate_host_token(monkeypatch):
    """Don't pick up data/tls/host.token from a live salon box."""
    monkeypatch.setenv("BYOI_HOST_TOKEN", "byoi-host")
    monkeypatch.delenv("BYOI_HOST_TOKEN_FILE", raising=False)
    monkeypatch.delenv("BYOI_TLS_DIR", raising=False)


@pytest.fixture(autouse=True)
def _isolate_claude_accounts(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path_factory.mktemp("claude-accounts")))
    monkeypatch.setenv("BYOI_HANDOFFS_DIR", str(tmp_path_factory.mktemp("handoffs")))


@pytest.fixture(autouse=True)
def _projects_in_tmp(tmp_path_factory, monkeypatch):
    """The seeded board points at a real repo. Give it a folder so no test clones."""
    root = tmp_path_factory.mktemp("projects")
    monkeypatch.setenv("BYOI_PROJECTS_DIR", str(root))
    (root / seed_board.SEED_PROJECT["slug"]).mkdir()


@pytest.fixture(autouse=True)
def _no_real_docker(tmp_path_factory, monkeypatch):
    """No test may drive the machine's real Docker.

    Provisioning and grading both shell out to `docker`, and a test that
    reaches the host daemon can pull images, start containers, and leave them
    running. Tests that need one provide their own fake earlier on PATH.
    """
    blocker = tmp_path_factory.mktemp("no-docker")
    stub = blocker / "docker"
    stub.write_text(
        "#!/bin/sh\n"
        'echo "refusing to reach the real docker daemon from a test" >&2\n'
        "exit 127\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{blocker}:{os.environ['PATH']}")
