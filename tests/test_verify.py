from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.seat.verify import parse_verify_output, verify_prompt


def test_parse_structured_output():
    raw = """
{"type":"result","structured_output":{"summary":"QR contrast is good","passed":2,"failed":1,"cases":[
  {"name":"quiet zone","pass":true,"detail":"8 modules"},
  {"name":"contrast","pass":true},
  {"name":"scan in low light","pass":false,"detail":"threshold too light"}
]}}
"""
    report = parse_verify_output(raw)
    assert report["passed"] == 2
    assert report["failed"] == 1
    assert report["cases"][2]["name"] == "scan in low light"
    assert report["cases"][2]["pass"] is False


def test_parse_nested_result_string():
    inner = '{"summary":"ok","cases":[{"name":"builds","pass":true}]}'
    raw = '{"type":"result","result":' + inner + "}"
    report = parse_verify_output(raw)
    assert report["cases"][0]["name"] == "builds"
    assert report["failed"] == 0


def test_verify_prompt_includes_spec():
    text = verify_prompt(title="Slip QR", spec="- contrast >= 4.5\n- quiet zone 4 modules")
    assert "Slip QR" in text
    assert "contrast >= 4.5" in text


def test_complete_without_spec_does_not_test(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    check = client.post(
        "/api/sessions/check-in",
        json={"seat_id": "seat-1", "coder_name": "Ada"},
        headers={"Authorization": "Bearer byoi-host"},
    )
    sid = check.json()["session"]["id"]
    done = client.post(f"/api/sessions/{sid}/complete")
    assert done.status_code == 200
    assert done.json()["testing"] is False
    assert done.json()["session"]["status"] == "done"


def test_complete_with_spec_runs_verifier(tmp_path: Path, monkeypatch):
    report = {
        "summary": "1 failed",
        "passed": 1,
        "failed": 1,
        "cases": [
            {"name": "quiet zone", "pass": True, "detail": ""},
            {"name": "low light", "pass": False, "detail": "still too faint"},
        ],
    }
    monkeypatch.setattr("apps.api.seat_sync.verify_solution", lambda *a, **k: report)
    desk = TestClient(create_app(tmp_path), headers={"Authorization": "Bearer byoi-host"})
    brief = desk.post(
        "/api/board",
        json={"title": "QR", "brief": "scan", "spec": "- quiet zone\n- low light"},
    ).json()
    sid = desk.post("/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}).json()["session"]["id"]
    desk.post(f"/api/sessions/{sid}/claim", json={"board_id": brief["id"]})
    done = desk.post(f"/api/sessions/{sid}/complete")
    assert done.json()["testing"] is True
    tests = desk.get(f"/api/sessions/{sid}/tests").json()
    assert tests["test_status"] == "failed"
    assert tests["test_report"]["cases"][1]["pass"] is False
