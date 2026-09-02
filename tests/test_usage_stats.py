import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apps.seat.usage_stats import usage_report


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _assistant(ts: datetime, *, input_tokens=10, output_tokens=20, cache_creation=0, cache_read=0) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            }
        },
    }


def test_usage_report_empty_config_dir(tmp_path: Path):
    report = usage_report(tmp_path / "nope")
    assert report["daily"] == []
    assert report["hourly"] == []
    assert report["totals"]["total_tokens"] == 0


def test_usage_report_buckets_by_day_and_hour(tmp_path: Path):
    now = datetime.now(timezone.utc)
    transcript = tmp_path / "projects" / "proj" / "session.jsonl"
    entries = [
        _assistant(now, input_tokens=10, output_tokens=20),
        _assistant(now + timedelta(minutes=5), input_tokens=5, output_tokens=5),
        _assistant(now - timedelta(days=1), input_tokens=100, output_tokens=200),
        {"type": "user", "timestamp": now.isoformat(), "message": {}},  # ignored: not assistant
        {"type": "assistant", "timestamp": now.isoformat(), "message": {}},  # ignored: no usage
    ]
    _write_transcript(transcript, entries)

    report = usage_report(tmp_path, days=5, hours=48)

    assert report["totals"]["input_tokens"] == 115
    assert report["totals"]["output_tokens"] == 225
    assert report["totals"]["messages"] == 3
    assert len(report["daily"]) == 2
    # Most recent day first.
    assert report["daily"][0]["messages"] == 2
    assert report["daily"][1]["messages"] == 1


def test_usage_report_caps_lines(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("apps.seat.usage_stats.MAX_LINES", 3)
    now = datetime.now(timezone.utc)
    transcript = tmp_path / "projects" / "proj" / "session.jsonl"
    entries = [_assistant(now + timedelta(seconds=i)) for i in range(10)]
    _write_transcript(transcript, entries)

    report = usage_report(tmp_path)
    assert report["totals"]["messages"] == 3
