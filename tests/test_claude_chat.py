import asyncio
import base64
import io
import json
import sys
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


def test_permission_survives_reconnect_and_resolves():
    # A guest reconnecting (page reload, phone screen lock) while a tool
    # permission ask is pending must still see it in the snapshot's history --
    # otherwise Claude sits blocked on an approval nobody can answer anymore.
    from apps.seat.claude_chat import ClaudeChat

    async def run():
        class Stdin:
            def write(self, _data):
                return None

            async def drain(self):
                return None

        class Proc:
            stdin = Stdin()

        chat = ClaudeChat()
        chat._proc = Proc()  # type: ignore[assignment]
        [event] = chat.translator.feed(
            {
                "type": "control_request",
                "request_id": "req-9",
                "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "rm -rf /"}},
            }
        )
        chat._remember(event)
        # A reconnecting client rebuilds its view from exactly this list.
        pending = chat.snapshot()["history"]
        assert any(h["type"] == "permission" and h["request_id"] == "req-9" for h in pending)

        await chat.answer_permission("req-9", True)
        return chat.snapshot()["history"]

    history = asyncio.run(run())
    stored = next(h for h in history if h["type"] == "permission")
    assert stored["resolved"] == "allowed"
    assert stored["name"] == "Bash"  # answering must not blank out the card's own fields


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


def test_a_guest_gets_the_default_build_test_allowlist(monkeypatch):
    """Claude Code denies `npm run <script>` outright in headless mode, on any
    permission mode, with no ask at all -- nothing on the guest side can turn
    that into an approvable prompt. Pre-approving the normal build/test cycle
    is what keeps a guest from hitting that wall on an ordinary `npm run lint`."""
    monkeypatch.delenv("BYOI_CLAUDE_TOOLS", raising=False)
    argv = claude_chat.claude_argv()
    assert argv[argv.index("--allowedTools") + 1] == claude_chat.DEFAULT_ALLOWED_TOOLS
    assert "Bash(npm run *)" in claude_chat.DEFAULT_ALLOWED_TOOLS


def test_an_operator_set_allowlist_overrides_the_default(monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_TOOLS", "Bash(ls *)")
    argv = claude_chat.claude_argv()
    assert argv[argv.index("--allowedTools") + 1] == "Bash(ls *)"


def test_an_operator_can_explicitly_disable_the_default(monkeypatch):
    """Empty is a deliberate choice -- a tighter guest sandbox -- not "unset"."""
    monkeypatch.setenv("BYOI_CLAUDE_TOOLS", "")
    argv = claude_chat.claude_argv()
    assert "--allowedTools" not in argv


# --- the seat's own MCP servers (the headless browser) ----------------------


def test_the_browser_is_reached_over_mcp_not_bash(monkeypatch):
    """Bash could never carry it.

    Claude Code's Bash safety classifier denies off-allowlist commands before
    emitting a control request, so a browser driven from Bash has no card the
    guest could ever approve. MCP tools take the ordinary permission path.
    """
    monkeypatch.delenv("BYOI_SEAT_MCP", raising=False)
    monkeypatch.setattr(claude_chat, "supports_flag", lambda b, f: True)
    argv = claude_chat.claude_argv()

    config = Path(argv[argv.index("--mcp-config") + 1])
    assert config.is_file()
    assert "browser" in json.loads(config.read_text())["mcpServers"]
    assert "mcp__browser" in claude_chat.DEFAULT_ALLOWED_TOOLS


def test_a_guest_repos_own_mcp_json_cannot_add_tools(monkeypatch):
    """The guest is editing this tree. A .mcp.json they commit must not become
    a way to load servers into the seat's Claude."""
    monkeypatch.delenv("BYOI_SEAT_MCP", raising=False)
    monkeypatch.setattr(claude_chat, "supports_flag", lambda b, f: True)
    assert "--strict-mcp-config" in claude_chat.claude_argv()


def test_a_salon_pc_without_the_browser_still_opens(monkeypatch, tmp_path):
    """A missing config is not an error -- the guest loses the page snapshot,
    not the seat. Passing --mcp-config a path that is not there is."""
    monkeypatch.setenv("BYOI_SEAT_MCP", str(tmp_path / "absent.json"))
    monkeypatch.setattr(claude_chat, "supports_flag", lambda b, f: True)
    assert "--mcp-config" not in claude_chat.claude_argv()


def test_an_operator_can_run_a_seat_with_no_mcp_servers(monkeypatch):
    """Empty is a deliberate choice, the way it is for BYOI_CLAUDE_TOOLS."""
    monkeypatch.setenv("BYOI_SEAT_MCP", "")
    monkeypatch.setattr(claude_chat, "supports_flag", lambda b, f: True)
    assert "--mcp-config" not in claude_chat.claude_argv()


def test_a_build_without_mcp_config_degrades(monkeypatch):
    """`unknown option` is fatal before stdin is read, same as any other flag."""
    monkeypatch.delenv("BYOI_SEAT_MCP", raising=False)
    monkeypatch.setattr(claude_chat, "supports_flag", lambda b, f: False)
    argv = claude_chat.claude_argv()

    assert "--mcp-config" not in argv
    assert "--strict-mcp-config" not in argv


def test_the_shipped_browser_can_actually_launch_in_a_seat():
    """Two things the seat container makes non-negotiable.

    The container runs as root, where chromium's own sandbox refuses to start,
    and there is no display. Both are silent failures at the first navigate.
    """
    config = json.loads((claude_chat.ROOT / "deploy" / "seat-mcp.json").read_text())
    args = config["mcpServers"]["browser"]["args"]

    assert "--headless" in args
    assert "--no-sandbox" in args
    # Screenshots are files. verify.py already learned that a grader writing
    # into the guest's tree leaves things in a repo the guest keeps.
    out = args[args.index("--output-dir") + 1]
    assert not out.startswith(str(claude_chat.ROOT))


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


def test_an_image_result_is_named_not_dumped():
    """A Read of a photo comes back as an image block, not as text.

    Serialising it put ~321 KB of base64 into the tool card the phone renders,
    which is both unreadable and enormous. Name the block instead.
    """
    image = {"type": "image", "source": {"type": "base64", "data": "A" * 5000}}

    assert claude_chat._result_text([image]) == "[image]"
    assert "AAAA" not in claude_chat._result_text([image])
    # Text still wins whenever there is any.
    assert claude_chat._result_text([{"type": "text", "text": "ok"}, image]) == "ok"


# --- what the browser saw, on the guest's phone -----------------------------


def _png(width: int = 1200, height: int = 900, *, flat: bool = False) -> str:
    """A screenshot-shaped PNG. Noisy by default: a page of flat colour is a
    pathological case for JPEG, and it is the case the shrink path must not
    make worse."""
    import random

    from PIL import Image

    if flat:
        image = Image.new("RGB", (width, height), (200, 40, 40))
    else:
        rand = random.Random(0)
        image = Image.frombytes("RGB", (width, height), rand.randbytes(width * height * 3))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _shot_result(data: str, media_type: str = "image/png") -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
                    ],
                }
            ]
        },
    }


def test_the_shape_the_browser_actually_returns_is_understood():
    """MCP does not use Anthropic's image block.

    `browser_take_screenshot` returns a flat block with `mimeType`, not a
    `source` wrapper with `media_type`, and it ships a text block beside it.
    Measured against @playwright/mcp 0.0.80 inside the seat image. Reading only
    Anthropic's shape sent the pixels nowhere and nothing said so.
    """
    translator = GuestTranslator()
    translator._tools["t1"] = {"name": "mcp__browser__browser_take_screenshot", "input": {}}
    (event,) = translator.feed(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            {"type": "text", "text": "### Result\n- Screenshot of viewport"},
                            {"type": "image", "data": _png(400, 300), "mimeType": "image/png"},
                        ],
                    }
                ]
            },
        }
    )

    assert event["shots"], "the browser's own block shape was not recognised"
    # The text beside it says what was captured, so it is kept.
    assert "Screenshot of viewport" in event["output"]


def test_a_screenshot_reaches_the_phone_instead_of_being_named():
    """Claude seeing the page is half of it. A guest taking Claude's word for
    how their own work looks is the half that was missing."""
    translator = GuestTranslator()
    translator._tools["t1"] = {"name": "mcp__browser__browser_take_screenshot", "input": {}}
    (event,) = translator.feed(_shot_result(_png()))

    assert event["shots"], "the picture never made it out of the tool result"
    assert event["shots"][0]["media_type"] == "image/jpeg"
    # The old placeholder described what there was nowhere to show. There is now.
    assert event["output"] == ""


def test_a_screenshot_is_shrunk_to_something_a_phone_will_load():
    """Chromium hands back a full-resolution PNG, and every one of them is
    re-sent in the snapshot on every reconnect."""
    from PIL import Image

    raw = _png(1200, 900)
    translator = GuestTranslator()
    (event,) = translator.feed(_shot_result(raw))

    packed = event["shots"][0]["data"]
    assert len(packed) < len(raw) / 4
    image = Image.open(io.BytesIO(base64.b64decode(packed)))
    assert max(image.size) <= claude_chat.SHOT_MAX_EDGE


def test_a_small_screenshot_is_not_made_bigger():
    """JPEG loses to PNG on a page of flat colour. Nothing needed scaling here,
    so re-encoding would spend bytes to make the picture worse."""
    raw = _png(400, 300, flat=True)
    translator = GuestTranslator()
    (event,) = translator.feed(_shot_result(raw))

    assert event["shots"][0]["media_type"] == "image/png"
    assert event["shots"][0]["data"] == raw


def test_something_that_is_not_an_image_is_not_forwarded():
    translator = GuestTranslator()
    (event,) = translator.feed(_shot_result(base64.b64encode(b"not a png").decode()))

    assert not event.get("shots")
    # And the tool card still says what came back, the way it always did.
    assert event["output"] == "[image]"


def test_a_read_of_a_photo_still_shows_its_text():
    """Text wins whenever there is any -- a tool that returns both is not a
    screenshot, and blanking its output would lose the part that was readable."""
    translator = GuestTranslator()
    obj = _shot_result(_png(40, 40))
    obj["message"]["content"][0]["content"].insert(0, {"type": "text", "text": "read 1 image"})
    (event,) = translator.feed(obj)

    assert event["output"] == "read 1 image"
    assert event["shots"]


def test_only_the_last_few_screenshots_keep_their_pixels(tmp_path, monkeypatch):
    """A phone in a cafe reconnects a lot, and every reconnect re-sends the
    whole history."""
    chat = claude_chat.ClaudeChat()
    for i in range(claude_chat.SHOT_HISTORY + 3):
        chat._remember(
            {
                "type": "tool",
                "id": f"t{i}",
                "name": "mcp__browser__browser_take_screenshot",
                "status": "done",
                "output": "",
                "shots": [{"media_type": "image/jpeg", "data": "AAAA"}],
            }
        )

    kept = [h for h in chat._history if h.get("shots")]
    assert len(kept) == claude_chat.SHOT_HISTORY
    # The newest are the ones worth carrying.
    assert kept[-1]["id"] == f"t{claude_chat.SHOT_HISTORY + 2}"
    # An older card is still a card -- it goes back to naming what it returned.
    assert chat._history[0]["output"] == "[image]"


def test_the_reader_takes_a_line_carrying_an_image(monkeypatch):
    """asyncio's 64 KiB default killed the read pump mid-turn, silently.

    A base64 image line runs to hundreds of kilobytes and readline() raises
    ValueError rather than returning it. The process stayed alive, so the
    "Claude Code exited" branch never fired, and the phone and the desk both
    froze on a half-finished answer with nothing in the logs.
    """
    seen: dict = {}
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        seen.update(kwargs)
        return await real(*argv, **kwargs)

    monkeypatch.setattr(claude_chat.asyncio, "create_subprocess_exec", spy)
    # A stand-in for claude that emits one oversized stream-json line.
    monkeypatch.setattr(
        claude_chat,
        "claude_argv",
        lambda: [
            sys.executable,
            "-u",
            "-c",
            "import json;"
            "print(json.dumps({'type':'user','message':{'content':["
            "{'type':'tool_result','tool_use_id':'t1','content':["
            "{'type':'image','source':{'type':'base64','data':'A'*400000}}]}]}}));"
            "print(json.dumps({'type':'result','subtype':'success'}))",
        ],
    )

    chat = claude_chat.ClaudeChat()

    async def drive():
        await chat.ensure()
        for _ in range(200):
            if any(e.get("type") == "usage" for e in chat._history):
                break
            await asyncio.sleep(0.05)
        chat._stop_process()

    asyncio.run(drive())

    # The symptom first: the oversized line has to come out the other side.
    tools = [e for e in chat._history if e.get("type") == "tool"]
    assert tools, "the image tool_result never arrived -- the pump died on it"
    assert tools[0]["output"] == "[image]"
    # Then the mechanism that makes it possible.
    assert seen.get("limit") == claude_chat.STREAM_LIMIT
    assert seen["limit"] > 64 * 1024
