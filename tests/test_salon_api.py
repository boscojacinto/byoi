from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path))


def test_desk_claude_accounts_and_missing_handoff(tmp_path: Path):
    client = _client(tmp_path)
    headers = {"Authorization": "Bearer byoi-host"}
    accounts = client.get("/api/claude-accounts", headers=headers)
    assert accounts.status_code == 200
    assert "accounts" in accounts.json()
    check = client.post(
        "/api/sessions/check-in",
        json={"seat_id": "seat-1", "coder_name": "Ada"},
        headers=headers,
    )
    sid = check.json()["session"]["id"]
    assert client.get(f"/api/sessions/{sid}/handoff").status_code == 404


def test_health_and_seed_board(tmp_path: Path):
    client = _client(tmp_path)
    assert client.get("/api/health").json()["ok"] is True
    items = client.get("/api/board").json()["items"]
    assert len(items) >= 3
    seats = client.get("/api/seats").json()["seats"]
    assert {s["id"] for s in seats} >= {"seat-1", "seat-2", "seat-3"}


def test_loopback_is_not_trusted(tmp_path: Path):
    """A request that appears to come from 127.0.0.1 proves nothing.

    The desk sits behind a reverse proxy, so that is what every guest request
    looks like. Trusting it would hand the floor to the internet.
    """
    desk = TestClient(create_app(tmp_path), client=("127.0.0.1", 50000))
    res = desk.post("/api/board", json={"title": "Loopback brief", "brief": "from the desk PC"})
    assert res.status_code == 401


def test_host_can_add_brief(tmp_path: Path):
    client = _client(tmp_path)
    assert client.post("/api/board", json={"title": "x", "brief": "y"}).status_code == 401
    res = client.post(
        "/api/board",
        json={"title": "Steam the wand", "brief": "document the espresso ritual"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    assert res.status_code == 200
    assert res.json()["title"] == "Steam the wand"


def test_checkin_claim_complete_and_slip_dump(tmp_path: Path):
    client = _client(tmp_path)
    check = client.post(
        "/api/sessions/check-in",
        json={"seat_id": "seat-1", "coder_name": "Ada"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    assert check.status_code == 200
    body = check.json()
    assert body["print"]["mode"] == "dump"
    assert Path(body["print"]["png"]).is_file()
    assert Path(body["qr"]).is_file()
    qr = client.get("/last-qr.png")
    assert qr.status_code == 200
    assert qr.headers["content-type"].startswith("image/png")
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(qr.content))
    assert img.size[0] == img.size[1]
    assert img.size[0] >= 200
    assert "otp=" in body["join"]
    assert body["otp"]
    assert f"otp={body['otp']}" in body["join"]
    assert body["seat_admitted"] is True
    assert "192.168.44.1" not in body["join"]
    assert "192.168.44.1" not in body["ssh"]
    assert body["ssh"].startswith("ssh guest@")
    sid = body["session"]["id"]
    board_id = client.get("/api/board").json()["items"][0]["id"]
    claimed = client.post(f"/api/sessions/{sid}/claim", json={"board_id": board_id})
    assert claimed.status_code == 200
    assert claimed.json()["session"]["status"] == "active"
    done = client.post(f"/api/sessions/{sid}/complete")
    assert done.json()["session"]["status"] == "done"
    seats = client.get("/api/seats").json()["seats"]
    assert next(s for s in seats if s["id"] == "seat-1")["status"] == "idle"


def test_host_can_free_occupied_seat(tmp_path: Path):
    client = _client(tmp_path)
    headers = {"Authorization": "Bearer byoi-host"}
    assert client.post("/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}, headers=headers).status_code == 200
    freed = client.post("/api/seats/seat-1/free", headers=headers)
    assert freed.status_code == 200
    assert freed.json()["seat"]["status"] == "idle"
    seats = client.get("/api/seats").json()["seats"]
    assert next(s for s in seats if s["id"] == "seat-1")["session"] is None


def test_stale_occupied_column_does_not_block_floor(tmp_path: Path):
    """A leftover seats.status=occupied with no live session must show idle."""
    import sqlite3

    client = _client(tmp_path)
    conn = sqlite3.connect(tmp_path / "salon.db")
    conn.execute("UPDATE seats SET status='occupied'")
    conn.commit()
    conn.close()

    seats = client.get("/api/seats").json()["seats"]
    assert all(s["status"] == "idle" for s in seats)
    assert all(s["session"] is None for s in seats)
    headers = {"Authorization": "Bearer byoi-host"}
    assert client.post("/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}, headers=headers).status_code == 200


def test_host_can_free_all_seats(tmp_path: Path):
    client = _client(tmp_path)
    headers = {"Authorization": "Bearer byoi-host"}
    for seat in ("seat-1", "seat-2", "seat-3"):
        assert client.post("/api/sessions/check-in", json={"seat_id": seat, "coder_name": "x"}, headers=headers).status_code == 200
    assert all(s["status"] == "occupied" for s in client.get("/api/seats").json()["seats"])
    freed = client.post("/api/seats/free-all", headers=headers)
    assert freed.status_code == 200
    seats = freed.json()["seats"]
    assert all(s["status"] == "idle" for s in seats)
    assert all(s["session"] is None for s in seats)


def test_occupied_seat_conflicts(tmp_path: Path):
    client = _client(tmp_path)
    headers = {"Authorization": "Bearer byoi-host"}
    assert client.post("/api/sessions/check-in", json={"seat_id": "seat-2", "coder_name": "A"}, headers=headers).status_code == 200
    assert client.post("/api/sessions/check-in", json={"seat_id": "seat-2", "coder_name": "B"}, headers=headers).status_code == 409


def test_join_otp(tmp_path: Path):
    client = _client(tmp_path)
    check = client.post(
        "/api/sessions/check-in",
        json={"seat_id": "seat-3", "coder_name": "Lin"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    otp = check.json()["session"]["unlock_otp"]
    joined = client.get(f"/api/join?otp={otp}")
    assert joined.status_code == 200
    body = joined.json()
    assert body["session"]["coder_name"] == "Lin"
    assert body["wifi_ssid"]
    assert "192.168.44.1" not in (body.get("seat_agent") or "")


def test_desk_tabs_floor_solutions_live(tmp_path: Path):
    html = _client(tmp_path).get("/").text
    assert 'data-pane="floor"' in html
    assert 'data-pane="solutions"' in html
    assert 'data-pane="live"' in html
    assert "Sit a guest" in html
    assert "Wellness" not in html
    assert ">Queue<" not in html
    assert 'id="sitModal"' in html
    assert 'id="qr"' in html


def test_api_live_mirrors_guest_history(tmp_path: Path, monkeypatch):
    client = _client(tmp_path)
    headers = {"Authorization": "Bearer byoi-host"}
    monkeypatch.setattr(
        "apps.api.seat_sync.live_snapshot",
        lambda seat: {
            "history": [{"type": "user", "text": "ls"}, {"type": "assistant", "text": "apps/"}],
            "cwd": "/tmp/salon",
            "busy": False,
            "model": "opus",
        },
    )
    empty = client.get("/api/live", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["sessions"] == []
    assert client.post(
        "/api/sessions/check-in",
        json={"seat_id": "seat-1", "coder_name": "Ada"},
        headers=headers,
    ).status_code == 200
    live = client.get("/api/live", headers=headers)
    assert live.status_code == 200
    sessions = live.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["seat"]["id"] == "seat-1"
    assert sessions[0]["live"]["history"][0]["text"] == "ls"
    assert client.get("/api/live").status_code == 401
