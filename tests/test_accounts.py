import json
import time
from pathlib import Path

from apps.seat.accounts import (
    AccountPool,
    collect_summary,
    extract_compact_summary,
    history_fallback,
    parse_limit_error,
    parse_usage_payload,
    preferred_label,
    quota_over_threshold,
    read_handoff,
    read_usage_file,
    write_handoff,
)


def test_discover_skips_dirs_without_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    (tmp_path / "claude-seat-1").mkdir()
    spare = tmp_path / "claude-seat-2"
    spare.mkdir()
    (spare / ".credentials.json").write_text("{}", encoding="utf-8")
    pool = AccountPool()
    found = {a.label: a for a in pool.discover()}
    assert found["claude-seat-1"].credentialed is False
    assert found["claude-seat-2"].credentialed is True
    assert pool.pick() is not None
    assert pool.pick().label == "claude-seat-2"
    assert pool.pick(excluding="claude-seat-2") is None


def test_pick_prefers_named_account_then_skips_limited(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    for name in ("claude-seat-1", "claude-seat-2"):
        dest = tmp_path / name
        dest.mkdir()
        (dest / ".credentials.json").write_text("{}", encoding="utf-8")
    pool = AccountPool()
    assert pool.pick(preferred="claude-seat-2").label == "claude-seat-2"
    pool.mark_limited("claude-seat-1", time.time() + 3600)
    assert pool.pick(preferred="claude-seat-1").label == "claude-seat-2"
    pool.mark_limited("claude-seat-2", time.time() + 3600)
    assert pool.pick() is None


def test_ensure_hooks_writes_statusline_and_settings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    dest = tmp_path / "claude-seat-1"
    dest.mkdir()
    (dest / ".credentials.json").write_text("{}", encoding="utf-8")
    (dest / "settings.json").write_text('{"theme": "dark"}\n', encoding="utf-8")
    pool = AccountPool()
    acct = pool.get("claude-seat-1")
    pool.ensure_hooks(acct)
    assert (dest / "byoi-statusline.sh").is_file()
    settings = json.loads((dest / "settings.json").read_text(encoding="utf-8"))
    assert settings["theme"] == "dark"
    assert "byoi-statusline.sh" in settings["statusLine"]["command"]
    assert settings["hooks"]["PostCompact"]


def test_parse_usage_payload_and_threshold():
    assert parse_usage_payload({}) is None
    empty = parse_usage_payload({"rate_limits": {}})
    assert empty["five_hour"] is None
    quota = parse_usage_payload(
        {
            "transcript_path": "/tmp/s.jsonl",
            "rate_limits": {
                "five_hour": {"used_percentage": 79, "resets_at": 100},
                "seven_day": {"used_percentage": 10},
            },
        }
    )
    assert quota_over_threshold(quota, 80) is None
    quota["five_hour"] = 80
    assert quota_over_threshold(quota, 80) == "five_hour"
    quota["five_hour"] = 10
    quota["seven_day"] = 95
    assert quota_over_threshold(quota, 80) == "seven_day"


def test_read_usage_file(tmp_path: Path):
    path = tmp_path / "last-usage.json"
    assert read_usage_file(path) is None
    path.write_text(
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": 12.5}}}),
        encoding="utf-8",
    )
    assert read_usage_file(path)["five_hour"] == 12.5


def test_parse_limit_error_kinds():
    session = parse_limit_error("You've hit your session limit   resets 3:45pm")
    assert session and session.kind == "session"
    assert session.resets_at and session.resets_at > time.time()
    weekly = parse_limit_error("You've hit your weekly limit   resets Mon 12:00am")
    assert weekly and weekly.kind == "weekly"
    opus = parse_limit_error("You've hit your Opus limit   resets 3:45pm")
    assert opus and opus.kind == "opus"
    assert parse_limit_error("API Error: 429 retrying") is None
    assert parse_limit_error("Prompt is too long") is None
    assert parse_limit_error("Context limit reached · /compact or /clear") is None


def test_extract_compact_summary_from_jsonl(tmp_path: Path):
    path = tmp_path / "sess.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}),
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "isCompactSummary": True,
                        "summary": "Kept auth work and the failing QR test.",
                    }
                ),
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}),
            ]
        ),
        encoding="utf-8",
    )
    assert extract_compact_summary(path) == "Kept auth work and the failing QR test."


def test_history_fallback_and_collect_summary(tmp_path: Path):
    history = [
        {"type": "user", "text": "fix the slip"},
        {"type": "tool", "text": "Read"},
        {"type": "assistant", "text": "contrast bumped"},
    ]
    text = history_fallback(history)
    assert "user: fix the slip" in text
    assert "assistant: contrast bumped" in text
    assert "tool:" not in text
    assert collect_summary(tmp_path, history) == text


def test_handoff_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BYOI_HANDOFFS_DIR", str(tmp_path))
    path = write_handoff("abc123", "# summary\nfiles: slip.py")
    assert path.name == "abc123.md"
    assert read_handoff("abc123") == "# summary\nfiles: slip.py"
    assert read_handoff("missing") is None


def test_preferred_label(monkeypatch):
    monkeypatch.delenv("BYOI_CLAUDE_ACCOUNT", raising=False)
    monkeypatch.delenv("BYOI_SEAT_ID", raising=False)
    assert preferred_label("seat-1") == "claude-seat-1"
    assert preferred_label("claude-seat-2") == "claude-seat-2"
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNT", "spare")
    assert preferred_label("seat-1") == "spare"
