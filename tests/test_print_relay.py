"""The printer stayed at the counter when the desk moved to the cloud."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import printing
from apps.api.main import create_app
from apps.api.store import Store

HOST = {"Authorization": "Bearer byoi-host"}
RELAY = {"Authorization": "Bearer relay-secret"}


@pytest.fixture
def desk(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("BYOI_PRINT_MODE", "relay")
    monkeypatch.setenv("BYOI_PRINT_RELAY_TOKEN", "relay-secret")
    return TestClient(create_app(tmp_path), headers=HOST)


def test_check_in_queues_a_slip_instead_of_printing_it(desk: TestClient):
    res = desk.post("/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"})
    assert res.status_code == 200
    printed = res.json()["print"]
    assert printed["mode"] == "relay"
    assert printed["status"] == "queued"
    assert Path(printed["png"]).is_file()


def test_a_missing_relay_does_not_block_a_check_in(desk: TestClient):
    """The QR is on screen either way. An offline printer costs a piece of
    paper, not the guest's visit."""
    res = desk.post("/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"})
    assert res.status_code == 200
    assert res.json()["seat_admitted"] is True
    assert desk.get("/api/print/status").json()["online"] is False


def test_claim_fetch_and_finish(desk: TestClient):
    desk.post("/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"})
    relay = TestClient(desk.app, headers=RELAY)

    job = relay.get("/api/print/next")
    assert job.status_code == 200
    body = job.json()
    assert body["kind"] == "check-in"
    assert body["payload"]["seat"]

    png = relay.get(body["png"])
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"

    done = relay.post(f"/api/print/{body['id']}/done", json={"ok": True})
    assert done.status_code == 200
    assert done.json()["job"]["status"] == "done"
    # Nothing left to hand out.
    assert relay.get("/api/print/next").status_code == 204


def test_a_failed_print_is_recorded_with_its_reason(desk: TestClient):
    desk.post("/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"})
    relay = TestClient(desk.app, headers=RELAY)
    job = relay.get("/api/print/next").json()
    relay.post(f"/api/print/{job['id']}/done", json={"ok": False, "error": "paper out"})
    status = desk.get("/api/print/status").json()
    assert status["failed"] == 1
    assert status["last"]["error"] == "paper out"


def test_an_abandoned_claim_is_handed_out_again(tmp_path):
    """The relay is a laptop at a counter: it gets closed mid-job. Reprinting a
    slip is cheap, never printing one is not."""
    store = Store(tmp_path / "salon.db")
    job_id = store.enqueue_print("check-in", {"otp": "x"}, str(tmp_path / "a.png"))
    assert store.claim_print_job()["id"] == job_id
    assert store.claim_print_job() is None
    # Rewind the claim past the staleness window.
    store.conn.execute(
        "UPDATE print_jobs SET claimed_at=? WHERE id=?", (time.time() - 600, job_id)
    )
    store.conn.commit()
    assert store.claim_print_job()["id"] == job_id


def test_the_queue_is_first_in_first_out(tmp_path):
    store = Store(tmp_path / "salon.db")
    first = store.enqueue_print("check-in", {"n": 1}, None)
    time.sleep(0.01)
    second = store.enqueue_print("check-in", {"n": 2}, None)
    assert store.claim_print_job()["id"] == first
    assert store.claim_print_job()["id"] == second


def test_the_relay_token_is_required(desk: TestClient):
    anon = TestClient(desk.app)
    assert anon.get("/api/print/next").status_code == 401
    assert anon.post("/api/print/deadbeef/done", json={"ok": True}).status_code == 401
    wrong = TestClient(desk.app, headers={"Authorization": "Bearer nope"})
    assert wrong.get("/api/print/next").status_code == 401


def test_polling_marks_the_printer_online(desk: TestClient):
    assert desk.get("/api/print/status").json()["online"] is False
    TestClient(desk.app, headers=RELAY).get("/api/print/next")
    status = desk.get("/api/print/status").json()
    assert status["online"] is True
    assert status["mode"] == "relay"


def test_local_mode_still_prints_inline(tmp_path, monkeypatch):
    """A salon PC with the printer on its own Bluetooth is unchanged."""
    monkeypatch.delenv("BYOI_PRINT_MODE", raising=False)
    monkeypatch.delenv("PERIPAGE_MAC", raising=False)
    client = TestClient(create_app(tmp_path), headers=HOST)
    printed = client.post(
        "/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}
    ).json()["print"]
    assert printed["mode"] == "dump"
    assert Path(printed["bin"]).is_file()


def test_print_mode_defaults_to_local(monkeypatch):
    monkeypatch.delenv("BYOI_PRINT_MODE", raising=False)
    assert printing.print_mode() == "local"
    monkeypatch.setenv("BYOI_PRINT_MODE", "nonsense")
    assert printing.print_mode() == "local"
