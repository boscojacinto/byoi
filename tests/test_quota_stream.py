"""Usage tracking from the stream, because the status-line hook never fires.

`statusLine` is not invoked under `-p --output-format stream-json`, which is how
the seat runs Claude Code — verified against 2.1.241 by arming a marker in the
hook and watching it never run. So `last-usage.json` is never written on a real
CLI and `rate_limit_event` is the only usage signal the seat receives. Every
payload below is copied from a live run.
"""

import json

from apps.seat.accounts import (
    RATE_LIMIT_WARNING,
    parse_rate_limit_event,
    quota_over_threshold,
)
from apps.seat.claude_chat import GuestTranslator

LIVE_EVENT = {
    "type": "rate_limit_event",
    "session_id": "abc",
    "uuid": "def",
    "rate_limit_info": {
        "status": "allowed",
        "resetsAt": 1787507400,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled_until",
        "isUsingOverage": False,
    },
}


def test_the_live_event_parses_into_the_quota_shape():
    quota = parse_rate_limit_event(LIVE_EVENT["rate_limit_info"])
    assert quota["status"] == "allowed"
    assert quota["window"] == "five_hour"
    assert quota["five_hour_resets"] == 1787507400
    # No percentage on this event. Inventing one would be worse than None.
    assert quota["five_hour"] is None
    assert quota["seven_day"] is None
    assert quota["using_overage"] is False


def test_opus_and_sonnet_windows_fold_into_seven_day():
    for raw in ("seven_day_opus", "seven_day_sonnet", "seven_day_overage_included"):
        quota = parse_rate_limit_event({"status": "allowed", "rateLimitType": raw})
        assert quota["window"] == "seven_day", raw


def test_a_warning_status_trips_the_threshold_without_any_percentage():
    allowed = parse_rate_limit_event({"status": "allowed", "rateLimitType": "five_hour"})
    assert quota_over_threshold(allowed) is None

    warning = parse_rate_limit_event({"status": RATE_LIMIT_WARNING, "rateLimitType": "five_hour"})
    assert quota_over_threshold(warning) == "five_hour"

    rejected = parse_rate_limit_event({"status": "rejected", "rateLimitType": "seven_day"})
    assert quota_over_threshold(rejected) == "seven_day"


def test_percentages_still_work_for_the_status_line_file():
    """The tmux side door and the test fake still write last-usage.json."""
    assert quota_over_threshold({"five_hour": 81.0}) == "five_hour"
    assert quota_over_threshold({"five_hour": 12.0}) is None


def test_junk_payloads_are_ignored():
    assert parse_rate_limit_event(None) is None
    assert parse_rate_limit_event({}) is None
    assert parse_rate_limit_event("nope") is None


def test_the_translator_turns_the_event_into_a_quota_frame():
    events = GuestTranslator().feed(LIVE_EVENT)
    assert len(events) == 1
    assert events[0]["type"] == "quota"
    assert events[0]["window"] == "five_hour"
    # The phone renders this frame, so it has to survive a JSON round trip.
    json.dumps(events[0])


def test_an_unparseable_event_yields_no_frame():
    assert GuestTranslator().feed({"type": "rate_limit_event"}) == []
    assert GuestTranslator().feed({"type": "rate_limit_event", "rate_limit_info": {}}) == []


def test_a_warning_on_the_stream_drives_the_compact_then_switch_plan(tmp_path, monkeypatch):
    """The whole point: this path could never fire while it depended on the hook."""
    from apps.seat.claude_chat import ClaudeChat

    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    for label in ("claude-seat-1", "claude-seat-2"):
        acct = tmp_path / label
        acct.mkdir()
        (acct / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "x"}}), encoding="utf-8"
        )

    chat = ClaudeChat()
    chat.account_label = "claude-seat-1"
    chat.config_dir = tmp_path / "claude-seat-1"
    chat.quota = parse_rate_limit_event(
        {"status": RATE_LIMIT_WARNING, "rateLimitType": "five_hour"}
    )

    plan = chat.failover_plan([])
    assert plan["action"] == "compact", "a stream warning must start the handoff"
    assert plan["account"].label == "claude-seat-2"
    assert plan["window"] == "five_hour"


def test_an_allowed_status_leaves_the_session_alone(tmp_path, monkeypatch):
    from apps.seat.claude_chat import ClaudeChat

    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    for label in ("claude-seat-1", "claude-seat-2"):
        acct = tmp_path / label
        acct.mkdir()
        (acct / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "x"}}), encoding="utf-8"
        )

    chat = ClaudeChat()
    chat.account_label = "claude-seat-1"
    chat.config_dir = tmp_path / "claude-seat-1"
    chat.quota = parse_rate_limit_event({"status": "allowed", "rateLimitType": "five_hour"})
    assert chat.failover_plan([])["action"] == "none"


def test_refresh_quota_prefers_the_stream_over_a_stale_file(tmp_path, monkeypatch):
    from apps.seat.claude_chat import ClaudeChat

    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    stale = tmp_path / "acct"
    stale.mkdir()
    (stale / "last-usage.json").write_text(
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": 3.0}}}), encoding="utf-8"
    )
    chat = ClaudeChat()
    chat.config_dir = stale
    chat.quota = parse_rate_limit_event(
        {"status": RATE_LIMIT_WARNING, "rateLimitType": "five_hour"}
    )
    refreshed = chat.refresh_quota()
    assert refreshed["status"] == RATE_LIMIT_WARNING, "a stale file must not mask a live warning"


def test_refresh_quota_falls_back_to_the_file_when_the_stream_said_nothing(tmp_path, monkeypatch):
    from apps.seat.claude_chat import ClaudeChat

    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    acct = tmp_path / "acct"
    acct.mkdir()
    (acct / "last-usage.json").write_text(
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": 88.0}}}), encoding="utf-8"
    )
    chat = ClaudeChat()
    chat.config_dir = acct
    chat.quota = None
    refreshed = chat.refresh_quota()
    assert refreshed["five_hour"] == 88.0
