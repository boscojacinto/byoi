import asyncio
import json
from pathlib import Path

import pytest

from apps.seat import claude_chat
from apps.seat.accounts import SUBMIT_MARK

from apps.seat.claude_chat import (
    GuestTranslator,
    encode_interrupt,
    encode_mode,
    encode_permission,
    encode_user,
    list_workspace,
    safe_workspace_path,
    tool_detail,
    tool_diff,
)


def test_tool_detail_prefers_path_and_command():
    assert tool_detail("Read", {"file_path": "src/main.py"}) == "src/main.py"
    assert tool_detail("Bash", {"command": "pytest -q"}) == "pytest -q"
    assert tool_detail("WebSearch", {"query": "fastapi websocket"}) == "fastapi websocket"


def test_encode_user_and_permission():
    user = encode_user("hello", "sess-1")
    assert user["type"] == "user"
    assert user["session_id"] == "sess-1"
    assert user["message"]["content"][0]["text"] == "hello"
    allow = encode_permission("req-1", True, {"command": "ls"})
    assert allow["response"]["request_id"] == "req-1"
    assert allow["response"]["response"]["behavior"] == "allow"
    deny = encode_permission("req-1", False)
    assert deny["response"]["response"]["behavior"] == "deny"
    assert encode_interrupt()["request"]["subtype"] == "interrupt"


def test_translator_init_and_text_delta():
    t = GuestTranslator()
    ready = t.feed({"type": "system", "subtype": "init", "session_id": "abc", "model": "opus", "cwd": "/tmp"})
    assert ready[0]["type"] == "ready"
    assert ready[0]["session_id"] == "abc"
    deltas = t.feed(
        {
            "type": "stream_event",
            "uuid": "a1",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi "}},
        }
    )
    deltas += t.feed(
        {
            "type": "stream_event",
            "uuid": "a1",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "there"}},
        }
    )
    assert deltas[0]["delta"] is True
    assert deltas[0]["text"] == "Hi "
    assert deltas[1]["text"] == "there"
    assert t.assistant_id == "a1"


def test_translator_tools_and_results():
    t = GuestTranslator()
    running = t.feed(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [
                    {"type": "text", "text": "Looking."},
                    {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "README.md"}},
                ],
            },
        }
    )
    assert running[0]["type"] == "assistant"
    assert running[0]["done"] is True
    assert running[1]["type"] == "tool"
    assert running[1]["status"] == "running"
    assert running[1]["detail"] == "README.md"
    done = t.feed(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok", "is_error": False}]},
        }
    )
    assert done[0]["id"] == "toolu_1"
    assert done[0]["status"] == "done"


def test_translator_permission_and_result():
    t = GuestTranslator()
    perm = t.feed(
        {
            "type": "control_request",
            "request_id": "req-9",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "rm -rf /"}},
        }
    )
    assert perm[0]["type"] == "permission"
    assert perm[0]["request_id"] == "req-9"
    assert t.pop_permission_input("req-9")["command"] == "rm -rf /"
    end = t.feed({"type": "result", "subtype": "success"})
    kinds = [e["type"] for e in end]
    assert "status" in kinds
    assert "turn" in kinds


def test_translator_ignores_replayed_user_text():
    t = GuestTranslator()
    assert t.feed({"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}) == []


def test_encode_roundtrip_is_json_lines():
    line = json.dumps(encode_user("ship it")) + "\n"
    parsed = json.loads(line)
    assert parsed["message"]["role"] == "user"


def test_encode_user_with_image_and_mode():
    packed = encode_user("see this", images=[{"media_type": "image/png", "data": "aaa"}])
    kinds = [b["type"] for b in packed["message"]["content"]]
    assert kinds == ["text", "image"]
    assert encode_mode("plan")["request"]["subtype"] == "set_permission_mode"
    with pytest.raises(ValueError):
        encode_mode("explode")


def test_tool_diff_for_edit():
    diff = tool_diff("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"})
    assert diff["old"] == "x"
    assert diff["new"] == "y"


def test_translator_thinking_todos_usage():
    t = GuestTranslator()
    think = t.feed(
        {
            "type": "stream_event",
            "uuid": "th1",
            "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        }
    )
    assert think[0]["type"] == "thinking"
    todos = t.feed(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "td1",
                        "name": "TodoWrite",
                        "input": {"todos": [{"content": "ship", "status": "in_progress"}]},
                    }
                ]
            },
        }
    )
    assert todos[0]["todos"][0]["content"] == "ship"
    usage = t.feed({"type": "result", "subtype": "success", "total_cost_usd": 0.12, "duration_ms": 800, "num_turns": 2})
    assert any(e["type"] == "usage" and e["cost"] == 0.12 for e in usage)


def test_default_workspace_is_the_repo(monkeypatch):
    monkeypatch.delenv("BYOI_WORKSPACE", raising=False)
    from apps.seat.claude_chat import ROOT, workspace

    assert workspace() == ROOT.resolve()


def test_workspace_rejects_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_WORKSPACE", str(tmp_path))
    (tmp_path / "ok.txt").write_text("hi")
    listing = list_workspace("")
    assert any(e["name"] == "ok.txt" for e in listing["entries"])
    with pytest.raises(PermissionError):
        safe_workspace_path("../etc")


def _pool_with_spares(tmp_path, labels=("a", "b")):
    from apps.seat.accounts import AccountPool

    for name in labels:
        dest = tmp_path / name
        dest.mkdir()
        (dest / ".credentials.json").write_text("{}", encoding="utf-8")
    return AccountPool()


def test_failover_plan_threshold_compact_and_hard_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    monkeypatch.setenv("BYOI_QUOTA_FAILOVER_PCT", "80")
    from apps.seat.claude_chat import ClaudeChat

    chat = ClaudeChat(pool=_pool_with_spares(tmp_path))
    chat.account_label = "a"
    chat.quota = {"five_hour": 79, "seven_day": 10}
    assert chat.failover_plan([])["action"] == "none"
    chat.quota = {"five_hour": 80, "seven_day": 10}
    plan = chat.failover_plan([])
    assert plan["action"] == "compact"
    assert plan["account"].label == "b"
    hard = chat.failover_plan(
        [{"type": "error", "message": "You've hit your session limit   resets 3:45pm"}]
    )
    assert hard["action"] == "switch"
    assert hard["reason"] == "session"
    opus = chat.failover_plan([{"type": "error", "message": "You've hit your Opus limit   resets 3:45pm"}])
    assert opus["action"] == "opus"


def test_failover_plan_no_spare(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    from apps.seat.claude_chat import ClaudeChat

    chat = ClaudeChat(pool=_pool_with_spares(tmp_path, labels=("a",)))
    chat.account_label = "a"
    plan = chat.failover_plan([{"type": "error", "message": "You've hit your weekly limit"}])
    assert plan["action"] == "no_spare"
    chat._phase = "compacting"
    chat.quota = {"five_hour": 90}
    assert chat.failover_plan([])["action"] == "no_spare"


def test_switch_account_keeps_history_and_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    monkeypatch.setenv("BYOI_HANDOFFS_DIR", str(tmp_path / "handoffs"))
    from apps.seat.claude_chat import ClaudeChat

    pool = _pool_with_spares(tmp_path)
    envs: list[str | None] = []

    async def spawn(env=None):
        class Stdin:
            def write(self, _data):
                return None

            async def drain(self):
                return None

        class Proc:
            def __init__(self):
                self.returncode = None
                self.stdin = Stdin()
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()

            def kill(self):
                self.returncode = -9
                self.stdout.feed_eof()
                self.stderr.feed_eof()

        envs.append((env or {}).get("CLAUDE_CONFIG_DIR"))
        return Proc()

    async def run():
        chat = ClaudeChat(spawn=spawn, pool=pool)
        a = pool.get("a")
        b = pool.get("b")
        chat.assign_account(a)

        class FakeWS:
            def __init__(self):
                self.sent = []

            async def send_json(self, event):
                self.sent.append(event)

        dummy = FakeWS()
        chat._clients.add(dummy)  # type: ignore[arg-type]
        chat._history.append({"type": "user", "text": "fix the slip"})
        await chat.switch_account(b, reason="session", primer="Kept the QR contrast work.")
        chat._stop_process()
        return chat, a, b, dummy

    chat, a, b, dummy = asyncio.run(run())
    assert chat.account_label == "b"
    assert chat.config_dir == b.config_dir
    assert chat._history[0]["text"] == "fix the slip"
    assert dummy in chat._clients
    assert any(ev.get("type") == "account" and ev.get("label") == "b" for ev in dummy.sent)
    assert chat.handoff_text.startswith("Kept")
    assert any(e and str(e).endswith("b") for e in envs)
    assert pool.get("a").is_limited()


def test_switch_account_without_primer_uses_history(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    monkeypatch.setenv("BYOI_HANDOFFS_DIR", str(tmp_path / "handoffs"))
    from apps.seat.claude_chat import ClaudeChat

    async def spawn(env=None):
        class Stdin:
            def write(self, _data):
                return None

            async def drain(self):
                return None

        class Proc:
            def __init__(self):
                self.returncode = None
                self.stdin = Stdin()
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()

            def kill(self):
                self.returncode = -9
                self.stdout.feed_eof()
                self.stderr.feed_eof()

        return Proc()

    async def run():
        chat = ClaudeChat(spawn=spawn, pool=_pool_with_spares(tmp_path))
        chat.assign_account(chat.pool.get("a"))
        chat._history.extend(
            [{"type": "user", "text": "ship it"}, {"type": "assistant", "text": "done"}]
        )
        await chat.switch_account(chat.pool.get("b"), reason="compacted")
        chat._stop_process()
        return chat

    chat = asyncio.run(run())
    assert "user: ship it" in (chat.handoff_text or "")


def _fake_spawn(written: list[dict], hook_dir: Path | None = None):
    """hook_dir: stand in for byoi-submit.sh, which fires on the sentinel."""

    async def spawn(env=None):
        class Stdin:
            def write(self, data):
                obj = json.loads(data.decode("utf-8"))
                written.append(obj)
                if hook_dir is None:
                    return
                text = obj.get("message", {}).get("content", [{}])[0].get("text", "")
                if SUBMIT_MARK in text:
                    (hook_dir / "last-submit.json").write_text(
                        json.dumps(
                            {"prompt": text, "cwd": "/srv/proj", "transcript_path": "/t.jsonl"}
                        ),
                        encoding="utf-8",
                    )

            async def drain(self):
                return None

        class Proc:
            def __init__(self):
                self.returncode = None
                self.stdin = Stdin()
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()

            def kill(self):
                self.returncode = -9
                self.stdout.feed_eof()
                self.stderr.feed_eof()

        return Proc()

    return spawn


def test_signal_submit_returns_the_hook_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    from apps.seat.claude_chat import SUBMIT_SENTINEL, ClaudeChat

    pool = _pool_with_spares(tmp_path)
    written: list[dict] = []

    async def run():
        chat = ClaudeChat(spawn=_fake_spawn(written, hook_dir=tmp_path / "a"), pool=pool)
        chat.assign_account(pool.get("a"))
        found = await chat.signal_submit("sid1", wait=2.0)
        chat._stop_process()
        return chat, found

    chat, found = asyncio.run(run())
    assert found["cwd"] == "/srv/proj"
    text = written[-1]["message"]["content"][0]["text"]
    assert text == SUBMIT_SENTINEL.format(session_id="sid1")
    # The guest never sees it and the phone never goes busy.
    assert chat._history == []
    assert chat._busy is False


def test_signal_submit_ignores_a_previous_guests_file(tmp_path, monkeypatch):
    """A stale last-submit.json must not be read as this session's submission."""
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    from apps.seat.claude_chat import ClaudeChat

    pool = _pool_with_spares(tmp_path)

    async def run():
        chat = ClaudeChat(spawn=_fake_spawn([]), pool=pool)
        chat.assign_account(pool.get("a"))
        (chat.config_dir / "last-submit.json").write_text('{"cwd":"/old"}', encoding="utf-8")
        found = await chat.signal_submit("sid2", wait=0.0)
        chat._stop_process()
        return found

    assert asyncio.run(run()) is None


def test_signal_submit_gives_up_when_the_hook_never_fires(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    from apps.seat.claude_chat import ClaudeChat

    pool = _pool_with_spares(tmp_path)

    async def run():
        chat = ClaudeChat(spawn=_fake_spawn([]), pool=pool)
        chat.assign_account(pool.get("a"))
        found = await chat.signal_submit("sid1", wait=0.05)
        chat._stop_process()
        return found

    assert asyncio.run(run()) is None


def test_signal_submit_without_an_account_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    from apps.seat.claude_chat import ClaudeChat

    chat = ClaudeChat(spawn=_fake_spawn([]), pool=_pool_with_spares(tmp_path))
    assert asyncio.run(chat.signal_submit("sid1", wait=0.0)) is None


def test_reset_drops_a_stale_submission(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    from apps.seat.claude_chat import ClaudeChat

    pool = _pool_with_spares(tmp_path)
    chat = ClaudeChat(spawn=_fake_spawn([]), pool=pool)
    chat.assign_account(pool.get("a"))
    stale = chat.config_dir / "last-submit.json"
    stale.write_text('{"cwd":"/old"}', encoding="utf-8")
    chat.reset()
    assert not stale.is_file()


def test_signal_submit_swallows_the_blocked_prompts_result(tmp_path, monkeypatch):
    """The hook exits 2, but Claude still emits a zero-turn result the phone must not see."""
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    from apps.seat.claude_chat import ClaudeChat

    pool = _pool_with_spares(tmp_path)

    async def run():
        chat = ClaudeChat(spawn=_fake_spawn([], hook_dir=tmp_path / "a"), pool=pool)
        chat.assign_account(pool.get("a"))
        await chat.signal_submit("sid1", wait=2.0)
        chat._stop_process()
        return chat

    chat = asyncio.run(run())
    # Shape the live binary returns after a hook-blocked prompt.
    blocked = {"type": "result", "subtype": "success", "is_error": False,
               "num_turns": 0, "total_cost_usd": 0, "usage": {}}
    assert chat._should_swallow(blocked) is True
    # Only the phantom one; a real turn afterwards still reaches the guest.
    assert chat._should_swallow(blocked) is False


def test_should_swallow_leaves_other_events_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    from apps.seat.claude_chat import ClaudeChat

    chat = ClaudeChat(spawn=_fake_spawn([]), pool=_pool_with_spares(tmp_path))
    chat._swallow_result = True
    assert chat._should_swallow({"type": "assistant"}) is False
    assert chat._swallow_result is True  # still armed for the result that follows
    assert chat._should_swallow({"type": "result"}) is True


def test_reset_disarms_the_swallow_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    from apps.seat.claude_chat import ClaudeChat

    chat = ClaudeChat(spawn=_fake_spawn([]), pool=_pool_with_spares(tmp_path))
    chat._swallow_result = True
    chat.reset()
    assert chat._swallow_result is False
    assert chat._should_swallow({"type": "result"}) is False


# --- argv against a moving Claude Code -------------------------------------


def test_a_flag_this_build_lacks_is_left_out(monkeypatch):
    """`unknown option` kills the process before it reads stdin.

    Measured on Claude Code 2.1.197, which has no --forward-subagent-text: the
    guest reached the chat, the seat spawned claude, and the phone showed
    "Claude Code exited" with no other clue.
    """
    claude_chat._help_text.cache_clear()
    monkeypatch.setattr(
        claude_chat, "supports_flag", lambda b, f: f != "--forward-subagent-text"
    )
    argv = claude_chat.claude_argv()

    assert "--forward-subagent-text" not in argv
    assert "--include-partial-messages" in argv
    assert "--prompt-suggestions" in argv


def test_the_flags_that_make_it_a_chat_are_not_optional(monkeypatch):
    """Degrading on the extras is right; degrading on these is not."""
    claude_chat._help_text.cache_clear()
    monkeypatch.setattr(claude_chat, "supports_flag", lambda b, f: False)
    argv = claude_chat.claude_argv()

    assert argv[1] == "-p"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert "--permission-mode" in argv
    for flag in claude_chat.OPTIONAL_FLAGS:
        assert flag not in argv


def test_support_is_probed_from_the_binarys_own_help(monkeypatch):
    claude_chat._help_text.cache_clear()
    seen = []

    class Res:
        stdout = "  --prompt-suggestions   Suggest follow-ups\n"
        stderr = ""

    def fake_run(argv, **kw):
        seen.append(argv)
        return Res()

    monkeypatch.setattr(claude_chat.subprocess, "run", fake_run)
    assert claude_chat.supports_flag("claude", "--prompt-suggestions") is True
    assert claude_chat.supports_flag("claude", "--forward-subagent-text") is False
    assert seen[0] == ["claude", "--help"]
    # Once, not once per flag: the guest waits on this before their first reply.
    assert len(seen) == 1


def test_a_binary_that_will_not_run_drops_the_extras(monkeypatch):
    claude_chat._help_text.cache_clear()
    monkeypatch.setattr(
        claude_chat.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    )
    assert claude_chat.supports_flag("claude", "--prompt-suggestions") is False
