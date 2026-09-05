"""Drive Claude Code as a chat backend (stream-json), not a TTY mirror."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import os
import shutil
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect

from apps.secrets import scrub

from .accounts import (
    COMPACT_CMD,
    PRIMER,
    SUBMIT_MARK,
    Account,
    AccountPool,
    clear_submit,
    collect_summary,
    parse_limit_error,
    preferred_label,
    quota_over_threshold,
    read_submit,
    read_usage_file,
    write_handoff,
)

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_BIN = os.environ.get("BYOI_CLAUDE", "claude")
SUBMIT_SENTINEL = SUBMIT_MARK + " {session_id}"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".pytest_cache"}
Spawn = Callable[..., Awaitable[asyncio.subprocess.Process]]
MODES = ("acceptEdits", "plan", "auto", "manual")
# The guest picks from salon-friendly labels; Claude Code's CLI only
# recognizes default/plan/acceptEdits/bypassPermissions for
# set_permission_mode, so "auto"/"manual" sent verbatim are silently
# rejected or ignored by a real `claude` process. "auto" (skip prompts) maps
# to bypassPermissions and "manual" (ask every time) to the CLI's own default.
CLI_MODE = {
    "acceptEdits": "acceptEdits",
    "plan": "plan",
    "auto": "bypassPermissions",
    "manual": "default",
}

# Claude Code writes one JSON object per line, and a line carrying a base64
# image -- a Read of any photo in the guest's repo -- runs to hundreds of
# kilobytes. asyncio's StreamReader defaults to 64 KiB and raises ValueError on
# a longer line rather than returning it, which killed the read pump mid-turn:
# the process stayed alive, so nothing said it had exited, and both the phone
# and the desk sat on a half-finished answer forever. Measured on a 240 KB
# .webp, which is ~321 KB once base64'd.
STREAM_LIMIT = 16 * 1024 * 1024


def default_workspace() -> Path:
    raw = os.environ.get("BYOI_WORKSPACE", "").strip()
    path = Path(raw).expanduser() if raw else ROOT
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def workspace() -> Path:
    """Active guest project folder (board project, else BYOI_WORKSPACE / this repo)."""
    inst = globals().get("session")
    path = getattr(inst, "workspace_path", None) if inst is not None else None
    if path is not None:
        return Path(path)
    return default_workspace()


def safe_workspace_path(rel: str = "") -> Path:
    root = workspace().resolve()
    target = (root / (rel or ".")).resolve()
    if target != root and root not in target.parents:
        raise PermissionError("path outside workspace")
    return target


def list_workspace(rel: str = "") -> dict[str, Any]:
    target = safe_workspace_path(rel)
    if not target.is_dir():
        raise FileNotFoundError("not a directory")
    entries: list[dict[str, Any]] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name in SKIP_DIRS:
            continue
        if child.name.startswith(".") and child.name not in {".gitignore", ".env.example"}:
            continue
        rel_path = str(child.relative_to(workspace().resolve()))
        entries.append(
            {
                "name": child.name,
                "path": rel_path,
                "dir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else None,
            }
        )
        if len(entries) >= 200:
            break
    root = workspace().resolve()
    cwd = "" if target == root else str(target.relative_to(root))
    parent: str | None = None
    if target != root:
        parent = "" if target.parent == root else str(target.parent.relative_to(root))
    return {"cwd": cwd, "parent": parent, "entries": entries}


def submit_wait() -> float:
    try:
        return float(os.environ.get("BYOI_SUBMIT_WAIT", "10"))
    except ValueError:
        return 10.0


# Flags that improve the chat but are not what makes it work. Claude Code
# releases add and drop these, and an `unknown option` is fatal: the process
# exits before it reads a byte of stdin, and the phone shows "Claude Code
# exited" with nothing to go on. Degrade instead.
OPTIONAL_FLAGS = (
    "--include-partial-messages",
    "--replay-user-messages",
    "--forward-subagent-text",
    "--prompt-suggestions",
)

# Claude Code's own Bash safety classifier denies some commands outright in
# headless mode -- `npm run <script>` among them, since the script name could
# run anything -- without ever asking over the control channel. Neither
# acceptEdits nor manual/"ask every time" mode changes that; nothing on the
# guest side can turn it into an approvable prompt. Pre-approving the shapes
# of a normal build/test cycle here is what keeps a guest from hitting that
# wall on an ordinary "npm run lint". Deliberately narrow: run/test/install
# invocations, not anything that could touch the network or the filesystem
# outside the project (no curl, no rm, no sudo).
#
# `mcp__browser` is the headless browser (deploy/seat-mcp.json). It is
# pre-approved rather than left to prompt because looking at a page is a dozen
# navigate/snapshot/click calls, and a card per call on a phone is worse than
# useless -- the guest taps Allow twelve times or gives up. The tools it covers
# are confined to a browser the seat owns and throws away.
DEFAULT_ALLOWED_TOOLS = (
    "Bash(npm run *) Bash(npm test) Bash(npm install) Bash(npm ci) "
    "Bash(yarn *) Bash(pnpm *) Bash(bun run *) Bash(bun install) Bash(bun test) "
    "Bash(git status) Bash(git diff *) Bash(git log *) Bash(git add *) "
    "Bash(git commit *) Bash(git branch *) Bash(git show *) "
    # `git branch <name>` without these creates a branch the guest cannot then
    # switch to, which is how a real visit ended: the seat's Claude read the
    # silent denial as a prompt awaiting approval and told the guest twice to
    # approve something that was never going to appear. Both are local, need no
    # credential, and reach nothing outside the project.
    "Bash(git checkout *) Bash(git switch *) "
    "Bash(pytest *) Bash(python -m pytest *) Bash(python3 -m pytest *) "
    "Bash(pip install *) Bash(pip3 install *) "
    "Bash(make *) Bash(cargo build *) Bash(cargo test *) Bash(go build *) Bash(go test *) "
    "mcp__browser"
)


# Three things a seat's Claude cannot work out from inside the sandbox, and
# gets wrong in a way that costs the guest their visit rather than a turn.
#
# The third is not hypothetical. A guest asked to push their finished work; the
# push was refused before any control request was emitted, and the seat read
# that as a prompt awaiting approval -- so it told them, twice, to approve
# something that was never going to appear, at the end of an otherwise finished
# brief. A skill only helps if it is loaded; this is in every request.
SEAT_SYSTEM = (
    "You are Claude Code on a BYOI salon seat. The person you are talking to is "
    "on their phone, in a cafe, on a visit with a time limit.\n"
    "- Bash is restricted to an allowlist. Anything off it is denied by Claude "
    "Code's own safety classifier BEFORE any approval request is emitted, so "
    "there is no card on the guest's phone and no prompt for anyone to approve. "
    "Never tell the guest to approve, allow, or re-try a blocked command; say "
    "what you cannot do and what you will do instead. `npm run <script>` is "
    "allowlisted, so prefer it over npx or a node_modules/.bin binary.\n"
    "- The guest ships by tapping \"I'm done\" in their app, which pins their "
    "tree to a ref the desk fetches. Never `git push`: the seat holds no git "
    "credential by design. Committing locally is fine and encouraged.\n"
    "- If mcp__browser__* tools are present you have a headless browser. Use it "
    "to check a page you changed: browser_snapshot for structure, "
    "browser_take_screenshot when the question is visual. The guest sees your "
    "screenshots in the chat."
)


def seat_system_prompt() -> str:
    """Extra system prompt for a guest seat. Empty turns it off."""
    raw = os.environ.get("BYOI_SEAT_SYSTEM")
    return SEAT_SYSTEM if raw is None else raw


def seat_mcp_config() -> Path | None:
    """The seat's own MCP servers, or None to start Claude without any.

    Unset: the file shipped in the repo, which is also where the seat image
    copies it. Explicitly empty: an operator who wants no MCP servers at all --
    the same "unset vs. empty" distinction ``BYOI_CLAUDE_TOOLS`` already makes.

    A path that is not there is not an error. A salon PC that never installed
    the browser should still open for business; the guest loses the page
    snapshot, not the seat.
    """
    raw = os.environ.get("BYOI_SEAT_MCP")
    if raw is None:
        path = ROOT / "deploy" / "seat-mcp.json"
    elif not raw.strip():
        return None
    else:
        path = Path(raw).expanduser()
    return path if path.is_file() else None


@lru_cache(maxsize=4)
def _help_text(binary: str) -> str:
    """`claude --help`, once per binary.

    Cached on the text rather than per flag: asking four times cost four
    subprocesses on the first message of a visit, which the guest waits for.
    """
    try:
        res = subprocess.run(
            [binary, "--help"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (res.stdout or "") + (res.stderr or "")


def supports_flag(binary: str, flag: str) -> bool:
    """Whether this Claude Code build advertises *flag* in its help."""
    return flag in _help_text(binary)


def claude_argv() -> list[str]:
    binary = shutil.which(CLAUDE_BIN) or CLAUDE_BIN
    mode = os.environ.get("BYOI_CLAUDE_PERMISSION_MODE", "acceptEdits")
    argv = [
        binary,
        "-p",
        "--output-format",
        "stream-json",
        "--input-format",
        "stream-json",
        "--verbose",
    ]
    argv.extend(f for f in OPTIONAL_FLAGS if supports_flag(binary, f))
    argv.extend(["--permission-mode", mode])
    # Unset: the default build/test allowlist. Explicitly set, even to "": the
    # operator's own choice, including a deliberately tighter guest sandbox.
    tools = os.environ.get("BYOI_CLAUDE_TOOLS")
    if tools is None:
        tools = DEFAULT_ALLOWED_TOOLS
    if tools.strip():
        argv.extend(["--allowedTools", tools])
    system = seat_system_prompt()
    if system.strip() and supports_flag(binary, "--append-system-prompt"):
        argv.extend(["--append-system-prompt", system])
    mcp = seat_mcp_config()
    if mcp is not None and supports_flag(binary, "--mcp-config"):
        argv.extend(["--mcp-config", str(mcp)])
        # The guest is editing this tree. A repo that carries its own .mcp.json
        # would otherwise have its servers loaded into the seat's Claude, which
        # turns a file the guest controls into a way to add tools to the
        # sandbox. Strict mode keeps the seat to the servers the salon declared.
        if supports_flag(binary, "--strict-mcp-config"):
            argv.append("--strict-mcp-config")
    extra = os.environ.get("BYOI_CLAUDE_EXTRA", "").strip()
    if extra:
        argv.extend(extra.split())
    for raw in os.environ.get("BYOI_ADD_DIR", "").split(":"):
        extra_dir = raw.strip()
        if extra_dir:
            argv.extend(["--add-dir", extra_dir])
    return argv


def tool_detail(name: str, payload: Any) -> str:
    if not isinstance(payload, dict):
        text = str(payload or "").strip()
        return text[:160]
    for key in ("file_path", "path", "command", "query", "pattern", "url", "description", "skill"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    if name == "TodoWrite" and isinstance(payload.get("todos"), list):
        return f"{len(payload['todos'])} todos"
    if name == "MultiEdit" and isinstance(payload.get("edits"), list):
        return f"{len(payload['edits'])} edits"
    if name == "ExitPlanMode" and isinstance(payload.get("plan"), str) and payload["plan"].strip():
        return payload["plan"].strip()[:200]
    return name


def tool_diff(name: str, payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    path = payload.get("file_path") or payload.get("path")
    if name in {"Edit", "NotebookEdit"} and payload.get("old_string") is not None:
        return {
            "path": path,
            "old": str(payload.get("old_string") or ""),
            "new": str(payload.get("new_string") or ""),
        }
    if name == "MultiEdit" and isinstance(payload.get("edits"), list):
        edits = [e for e in payload["edits"] if isinstance(e, dict)]
        if not edits:
            return None
        # Each edit is its own old/new snippet, not a hunk of one file, so
        # stitching them with blank lines keeps the LCS diff from bleeding
        # unrelated edits into each other's context.
        old = "\n\n".join(str(e.get("old_string") or "") for e in edits)
        new = "\n\n".join(str(e.get("new_string") or "") for e in edits)
        return {"path": path, "old": old, "new": new}
    if name == "Write":
        body = payload.get("content")
        if body is None:
            body = payload.get("contents")
        if body is not None:
            return {"path": path, "old": "", "new": str(body)}
    return None


def encode_user(
    text: str,
    session_id: str | None = None,
    images: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if (text or "").strip():
        content.append({"type": "text", "text": text})
    for img in images or []:
        data = (img.get("data") or "").strip()
        if not data:
            continue
        media = img.get("media_type") or "image/jpeg"
        if media not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
            media = "image/jpeg"
        content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    if not content:
        content.append({"type": "text", "text": text or ""})
    message: dict[str, Any] = {"type": "user", "message": {"role": "user", "content": content}}
    if session_id:
        message["session_id"] = session_id
    return message


def encode_interrupt() -> dict[str, Any]:
    return {
        "type": "control_request",
        "request_id": str(uuid.uuid4()),
        "request": {"subtype": "interrupt"},
    }


def encode_mode(mode: str) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"unknown permission mode {mode}")
    return {
        "type": "control_request",
        "request_id": str(uuid.uuid4()),
        "request": {"subtype": "set_permission_mode", "mode": CLI_MODE[mode]},
    }


def encode_permission(request_id: str, allow: bool, tool_input: Any = None) -> dict[str, Any]:
    if allow:
        updated = tool_input if isinstance(tool_input, dict) else {}
        response: dict[str, Any] = {"behavior": "allow", "updatedInput": updated}
    else:
        response = {"behavior": "deny", "message": "Guest denied this tool on their phone."}
    return {
        "type": "control_response",
        "response": {"subtype": "success", "request_id": request_id, "response": response},
    }


def _blocks(message: dict[str, Any] | None) -> list[dict[str, Any]]:
    content = (message or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _text_of(blocks: list[dict[str, Any]]) -> str:
    parts = [str(block.get("text") or "") for block in blocks if block.get("type") == "text"]
    return "".join(parts)


def _thinking_of(blocks: list[dict[str, Any]]) -> str:
    parts = [str(block.get("thinking") or block.get("text") or "") for block in blocks if block.get("type") == "thinking"]
    return "".join(parts)


# A screenshot is worth a paragraph of "the header is still too tall", but only
# if it arrives. Chromium hands back a full-resolution PNG -- a 1280x800 page is
# about a megabyte, half as much again once base64'd -- and the whole history is
# re-sent in the snapshot every time a phone on cellular reconnects. Re-encoding
# to something phone-sized, and keeping the pixels only on the last few, is what
# stops a visit's worth of screenshots from becoming the slow part of the visit.
SHOT_MAX_EDGE = 900
SHOT_QUALITY = 72
SHOT_MAX_PER_RESULT = 2
SHOT_HISTORY = 4
# Only when Pillow could not re-encode it. Bigger than this and the picture is
# worth less than the reconnect it would cost.
SHOT_RAW_MAX = 400_000
SHOT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _shrink(data: str, media_type: str) -> dict[str, str] | None:
    """One image block, re-encoded small enough to send to a phone."""
    if not data:
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        from PIL import Image
    except ImportError:
        # A salon PC that somehow has no Pillow. Forward a small one as it came
        # and drop a large one -- this is the only branch where "could not
        # re-encode it" still means "it is probably an image", because nothing
        # here has looked at the bytes.
        if len(data) <= SHOT_RAW_MAX and media_type in SHOT_TYPES:
            return {"media_type": media_type, "data": data}
        return None
    try:
        image = Image.open(io.BytesIO(raw))
        fits = max(image.size) <= SHOT_MAX_EDGE
        image.thumbnail((SHOT_MAX_EDGE, SHOT_MAX_EDGE))
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=SHOT_QUALITY)
        shrunk = buf.getvalue()
    except Exception:  # noqa: BLE001 -- never lose a turn over a screenshot
        # Pillow is here and could not read it, so it was not an image. Passing
        # the bytes on would put whatever this is into an <img> on the phone.
        return None
    if fits and len(raw) <= len(shrunk) and media_type in SHOT_TYPES:
        # A page of flat colour is smaller as the PNG it arrived as. Nothing had
        # to be scaled down, so the smaller of the two is simply the better one.
        return {"media_type": media_type, "data": data}
    return {
        "media_type": "image/jpeg",
        "data": base64.b64encode(shrunk).decode("ascii"),
    }


def _shots(content: Any) -> list[dict[str, str]]:
    """The images in a tool result, ready for the guest's phone.

    The browser is the reason this exists: a page snapshot the guest cannot see
    leaves them taking Claude's word for how their own work looks.
    """
    if not isinstance(content, list):
        return []
    out: list[dict[str, str]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        source = block.get("source")
        if isinstance(source, dict):
            # Anthropic's shape -- a Read of a photo out of the guest's repo.
            if source.get("type") != "base64":
                continue
            data = str(source.get("data") or "")
            media = str(source.get("media_type") or "")
        else:
            # MCP's own shape, which is what the browser actually returns:
            # flat, and `mimeType` rather than `media_type`. Measured against
            # @playwright/mcp 0.0.80's browser_take_screenshot inside the seat
            # image. Both are accepted because which one reaches the stream
            # depends on how the CLI relays an MCP result, and a screenshot
            # silently becoming "[image]" again is not a failure anyone sees.
            data = str(block.get("data") or "")
            media = str(block.get("mimeType") or block.get("media_type") or "")
        packed = _shrink(data, media)
        if packed:
            out.append(packed)
        if len(out) >= SHOT_MAX_PER_RESULT:
            break
    return out


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = _text_of(content)
        if text:
            return text
        # A tool result is not always text: reading a photo gives back an image
        # block whose base64 is megabytes and says nothing on a phone. Name what
        # came back instead of serialising it into the tool card.
        kinds = sorted({str(b.get("type") or "block") for b in content if isinstance(b, dict)})
        return f"[{', '.join(kinds)}]" if kinds else ""
    if content is None:
        return ""
    return str(content)


class GuestTranslator:
    """Turn Claude Code stream-json objects into mobile chat events."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.model: str | None = None
        self.cwd: str | None = None
        self.mode: str = os.environ.get("BYOI_CLAUDE_PERMISSION_MODE", "acceptEdits")
        self.assistant_id: str | None = None
        self.thinking_id: str | None = None
        self._pending_input: dict[str, Any] = {}
        self._tools: dict[str, dict[str, Any]] = {}

    def feed(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        kind = obj.get("type")
        if kind == "system":
            return self._system(obj)
        if kind == "stream_event":
            return self._stream(obj)
        if kind == "assistant":
            return self._assistant(obj)
        if kind == "user":
            return self._user(obj)
        if kind == "result":
            return self._result(obj)
        if kind == "control_request":
            return self._control(obj)
        if kind in {"prompt_suggestion", "prompt-suggestion"}:
            text = obj.get("suggestion") or obj.get("text") or obj.get("prompt") or ""
            if not text:
                return []
            return [{"type": "suggestion", "text": str(text)}]
        if kind == "error":
            return [{"type": "error", "message": str(obj.get("error") or obj.get("message") or "Claude error")}]
        return []

    def _system(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        subtype = obj.get("subtype")
        if subtype == "init":
            self.session_id = obj.get("session_id") or self.session_id
            self.model = obj.get("model") or self.model
            self.cwd = obj.get("cwd") or self.cwd
            if obj.get("permissionMode") or obj.get("permission_mode"):
                self.mode = str(obj.get("permissionMode") or obj.get("permission_mode"))
            return [
                {
                    "type": "ready",
                    "session_id": self.session_id,
                    "model": self.model,
                    "cwd": self.cwd,
                    "mode": self.mode,
                    "tools": obj.get("tools") or [],
                }
            ]
        if subtype == "api_retry":
            delay = obj.get("retry_delay_ms") or 0
            return [{"type": "status", "busy": True, "label": f"Retrying… ({delay}ms)"}]
        if subtype == "status" and obj.get("status"):
            return [{"type": "status", "busy": True, "label": str(obj.get("status"))}]
        if subtype == "compact_boundary":
            # A guest who runs /compact otherwise gets no sign anything
            # happened beyond whatever the model chooses to say next.
            summary = obj.get("summary") or obj.get("compact_metadata", {}).get("summary")
            return [{"type": "compact", "summary": str(summary) if summary else ""}]
        return []

    def _stream(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        event = obj.get("event") or {}
        etype = event.get("type")
        if etype == "content_block_start":
            block = event.get("content_block") or {}
            btype = block.get("type")
            if btype == "tool_use":
                return [self._tool_event(block, status="running")]
            if btype == "thinking":
                self.thinking_id = str(obj.get("uuid") or uuid.uuid4())
                seed = str(block.get("thinking") or "")
                return [{"type": "thinking", "id": self.thinking_id, "text": seed, "done": False}]
            if btype == "text" and not self.assistant_id:
                self.assistant_id = str(obj.get("uuid") or uuid.uuid4())
                return [{"type": "assistant", "id": self.assistant_id, "text": "", "done": False}]
        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                chunk = str(delta.get("text") or "")
                if not chunk:
                    return []
                if not self.assistant_id:
                    self.assistant_id = str(obj.get("uuid") or uuid.uuid4())
                return [{"type": "assistant", "id": self.assistant_id, "text": chunk, "done": False, "delta": True}]
            if dtype in {"thinking_delta", "thinking"}:
                chunk = str(delta.get("thinking") or delta.get("text") or "")
                if not chunk:
                    return []
                if not self.thinking_id:
                    self.thinking_id = str(obj.get("uuid") or uuid.uuid4())
                return [{"type": "thinking", "id": self.thinking_id, "text": chunk, "done": False, "delta": True}]
            if dtype == "input_json_delta":
                return []
        if etype == "message_stop":
            events: list[dict[str, Any]] = []
            if self.thinking_id:
                events.append({"type": "thinking", "id": self.thinking_id, "text": "", "done": True, "delta": True})
                self.thinking_id = None
            if self.assistant_id:
                events.append({"type": "assistant", "id": self.assistant_id, "text": "", "done": True, "delta": True})
                self.assistant_id = None
            return events
        return []

    def _tool_event(
        self,
        block: dict[str, Any],
        status: str,
        output: str = "",
        shots: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        tool_id = str(block.get("id") or uuid.uuid4())
        name = str(block.get("name") or self._tools.get(tool_id, {}).get("name") or "tool")
        payload = block.get("input") if isinstance(block.get("input"), dict) else {}
        prior = self._tools.get(tool_id) or {}
        if not payload:
            payload = prior.get("input") or {}
        event: dict[str, Any] = {
            "type": "tool",
            "id": tool_id,
            "name": name,
            "detail": tool_detail(name, payload),
            "input": payload,
            "status": status,
            "output": output[:8000],
        }
        if shots:
            event["shots"] = shots
        diff = tool_diff(name, payload)
        if diff:
            event["diff"] = diff
        if name == "TodoWrite" and isinstance(payload.get("todos"), list):
            event["todos"] = payload["todos"]
        if name == "AskUserQuestion":
            event["type"] = "ask"
            event["questions"] = payload.get("questions") or []
        self._tools[tool_id] = {"name": name, "input": payload}
        return event

    def _assistant(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        message = obj.get("message") or {}
        blocks = _blocks(message)
        events: list[dict[str, Any]] = []
        thinking = _thinking_of(blocks)
        if thinking:
            tid = self.thinking_id or str(obj.get("uuid") or uuid.uuid4()) + "-think"
            events.append({"type": "thinking", "id": tid, "text": thinking, "done": True})
            self.thinking_id = None
        text = _text_of(blocks)
        msg_id = str(message.get("id") or obj.get("uuid") or self.assistant_id or uuid.uuid4())
        if text:
            events.append({"type": "assistant", "id": msg_id, "text": text, "done": True})
            self.assistant_id = None
        for block in blocks:
            if block.get("type") != "tool_use":
                continue
            events.append(self._tool_event(block, status="running"))
        return events

    def _user(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for block in _blocks(obj.get("message") or {}):
            if block.get("type") != "tool_result":
                continue
            tool_id = str(block.get("tool_use_id") or uuid.uuid4())
            is_error = bool(block.get("is_error"))
            content = block.get("content")
            shots = _shots(content)
            output = _result_text(content)
            if shots and not _text_of(content if isinstance(content, list) else []):
                # The picture is the whole result. "[image]" printed beside it
                # is the old placeholder describing what is now on screen.
                output = ""
            meta = self._tools.get(tool_id) or {}
            events.append(
                self._tool_event(
                    {"id": tool_id, "name": meta.get("name") or "tool", "input": meta.get("input") or {}},
                    status="error" if is_error else "done",
                    output=output,
                    shots=shots,
                )
            )
        return events

    def _result(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        self.assistant_id = None
        self.thinking_id = None
        events: list[dict[str, Any]] = []
        if obj.get("is_error") or obj.get("subtype") == "error":
            err = obj.get("errors") or obj.get("error") or obj.get("result") or "Claude hit an error"
            events.append({"type": "error", "message": str(err)})
        usage = {
            "cost": obj.get("total_cost_usd"),
            "duration_ms": obj.get("duration_ms"),
            "turns": obj.get("num_turns"),
            "usage": obj.get("usage") or {},
            "model": obj.get("model") or self.model,
        }
        events.append({"type": "usage", **{k: v for k, v in usage.items() if v is not None}})
        events.append({"type": "status", "busy": False, "label": "Ready"})
        events.append({"type": "turn", "done": True})
        return events

    def _control(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        request = obj.get("request") or {}
        if request.get("subtype") != "can_use_tool":
            return []
        request_id = str(obj.get("request_id") or "")
        name = str(request.get("tool_name") or "tool")
        payload = request.get("input") or {}
        self._pending_input[request_id] = payload
        return [
            {
                "type": "permission",
                "request_id": request_id,
                "name": name,
                "detail": tool_detail(name, payload),
                "input": payload,
                "diff": tool_diff(name, payload),
            }
        ]

    def pop_permission_input(self, request_id: str) -> Any:
        return self._pending_input.pop(request_id, {})


def _history_item(event: dict[str, Any]) -> dict[str, Any] | None:
    kind = event.get("type")
    if kind in {"user", "assistant", "thinking", "tool", "ask", "todos", "permission", "compact"}:
        item = dict(event)
        item.pop("delta", None)
        if kind == "assistant":
            item["done"] = bool(event.get("done"))
        return item
    if kind == "usage":
        return dict(event)
    return None


class ClaudeChat:
    """One Claude Code process per seat, shared by guest chat sockets."""

    def __init__(self, spawn: Spawn | None = None, pool: AccountPool | None = None) -> None:
        self._spawn = spawn
        self._pool = pool
        self._proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task | None = None
        self._err_pump: asyncio.Task | None = None
        self._clients: set[WebSocket] = set()
        self._history: list[dict[str, Any]] = []
        self._busy = False
        self._lock = asyncio.Lock()
        self._phase = "idle"
        self._failover_task: asyncio.Task | None = None
        self.translator = GuestTranslator()
        self.last_error: str | None = None
        self.last_usage: dict[str, Any] | None = None
        self.suggestions: list[str] = []
        self.extra_dirs: list[str] = []
        # The one entry in extra_dirs the desk owns: this visit's input media.
        self.media_dir: str | None = None
        self.workspace_path: Path | None = None
        self.account_label: str | None = None
        self.config_dir: Path | None = None
        self.byo = False
        self._swallow_result = False
        self.quota: dict[str, Any] | None = None
        self.handoff_text: str | None = None

    @property
    def pool(self) -> AccountPool:
        if self._pool is not None:
            return self._pool
        from .accounts import pool as default_pool

        return default_pool

    def set_workspace(self, path: str | Path, media_dir: str | Path | None = None) -> Path:
        dest = Path(path).expanduser().resolve()
        if not dest.is_dir():
            raise FileNotFoundError(f"not a directory: {dest}")
        self.reset()
        self.workspace_path = dest

        # A brief's input media sits beside the clone rather than in it, so it
        # is outside Claude's cwd and needs --add-dir to be readable at all.
        # Replace rather than append: extra_dirs outlives reset(), and a stale
        # path from the previous visit would otherwise pile up here.
        if self.media_dir and self.media_dir in self.extra_dirs:
            self.extra_dirs.remove(self.media_dir)
        self.media_dir = None
        if media_dir:
            found = Path(media_dir).expanduser().resolve()
            if found.is_dir():
                self.media_dir = str(found)
                if self.media_dir not in self.extra_dirs:
                    self.extra_dirs.append(self.media_dir)
        return dest

    def assign_account(self, account: Account | None) -> None:
        clear_submit(self.config_dir)
        self.account_label = account.label if account else None
        self.config_dir = account.config_dir if account else None
        self.byo = False
        if account is None:
            return
        from .accounts import accounts_dir
        from .guest_auth import guest_root

        # Hooks are what make the status line, PostCompact, StopFailure and
        # byoi-submit.sh work — without them the seat loses quota tracking and
        # grading stops. Install them for salon accounts and for a guest's own
        # ephemeral account, but never anywhere else: the pool falls back to
        # ~/.claude, and that is the operator's own config, not ours to write to.
        config_dir = account.config_dir.resolve()
        for root, is_guest in ((accounts_dir(), False), (guest_root(), True)):
            try:
                config_dir.relative_to(root)
            except ValueError:
                continue
            self.byo = is_guest
            self.pool.ensure_hooks(account)
            return

    def assign_preferred(self, seat_id: str | None = None) -> Account | None:
        account = self.pool.pick(preferred=preferred_label(seat_id))
        self.assign_account(account)
        return account

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "ready",
            "session_id": self.translator.session_id,
            "model": self.translator.model,
            "cwd": self.translator.cwd or str(workspace()),
            "mode": self.translator.mode,
            "busy": self._busy,
            "history": list(self._history),
            "error": self.last_error,
            "usage": self.last_usage,
            "suggestions": list(self.suggestions),
            "account": self.account_label,
            "byo": self.byo,
            "quota": dict(self.quota) if self.quota else None,
            "handoff": bool(self.handoff_text),
            "handoff_text": self.handoff_text,
            "accounts": self.pool.snapshot(current=self.account_label),
            "phase": self._phase,
        }

    def _stop_process(self) -> None:
        pump = self._pump
        err = self._err_pump
        proc = self._proc
        self._pump = None
        self._err_pump = None
        self._proc = None
        self._busy = False
        if pump:
            pump.cancel()
        if err:
            err.cancel()
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def reset(self) -> None:
        clear_submit(self.config_dir)
        self._swallow_result = False
        self._stop_process()
        self._phase = "idle"
        self._history.clear()
        self.last_error = None
        self.last_usage = None
        self.suggestions = []
        self.translator = GuestTranslator()
        self.quota = None
        self.handoff_text = None

    async def ensure(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        async with self._lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            # A process that existed and has now exited is a crash respawn,
            # not the session's first start. _start() throws away the old
            # translator/history for a fresh Claude session with no memory of
            # the conversation, while the guest's own transcript stays right
            # where it was — without this, the chat looks uninterrupted while
            # Claude has actually forgotten everything said so far.
            crashed = self._proc is not None
            await self._start()
            if crashed:
                await self._broadcast(
                    {
                        "type": "error",
                        "message": "Claude's process restarted — it no longer remembers this conversation. Your files are unchanged.",
                    }
                )

    async def _start(self) -> None:
        self._stop_process()
        self.translator = GuestTranslator()
        cwd = workspace()
        # The guest's Claude has Bash and inherits this environment, so desk-only
        # deploy credentials must never be in it — however they got here.
        env = scrub(os.environ.copy())
        if self.config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        try:
            if self._spawn:
                try:
                    self._proc = await self._spawn(env)  # type: ignore[call-arg]
                except TypeError:
                    self._proc = await self._spawn()
            else:
                argv = claude_argv()
                for extra_dir in self.extra_dirs:
                    argv.extend(["--add-dir", extra_dir])
                if not shutil.which(argv[0]) and not Path(argv[0]).exists():
                    self.last_error = "claude is not on PATH — run scripts/seat-claude-login.sh on this seat"
                    await self._broadcast({"type": "error", "message": self.last_error})
                    return
                self._proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cwd),
                    env=env,
                    limit=STREAM_LIMIT,
                )
        except FileNotFoundError:
            self.last_error = "claude is not installed on this seat"
            await self._broadcast({"type": "error", "message": self.last_error})
            return
        self.translator.cwd = str(cwd)
        self._pump = asyncio.create_task(self._pump_stdout())
        self._err_pump = asyncio.create_task(self._pump_stderr())

    async def attach_client(self, ws: WebSocket) -> None:
        await self.ensure()
        self._clients.add(ws)
        try:
            await ws.send_json(self.snapshot())
            while True:
                incoming = await ws.receive_json()
                kind = incoming.get("type")
                if kind == "user":
                    await self.send_user(str(incoming.get("text") or ""), incoming.get("images"))
                elif kind == "slash":
                    await self.send_user(str(incoming.get("command") or incoming.get("text") or ""))
                elif kind == "interrupt":
                    await self.interrupt()
                elif kind == "clear":
                    await self.ensure_fresh()
                    await ws.send_json(self.snapshot())
                elif kind == "mode":
                    await self.set_mode(str(incoming.get("mode") or ""))
                elif kind == "permission":
                    await self.answer_permission(
                        str(incoming.get("request_id") or ""),
                        bool(incoming.get("allow")),
                    )
        except WebSocketDisconnect:
            return
        except json.JSONDecodeError:
            await self._send(ws, {"type": "error", "message": "bad chat frame"})
        finally:
            self._clients.discard(ws)

    async def ensure_fresh(self) -> None:
        self.reset()
        await self.ensure()
        await self._broadcast({"type": "cleared"})
        await self._broadcast(self.snapshot())

    async def send_user(self, text: str, images: Any = None, *, silent: bool = False) -> None:
        pics = images if isinstance(images, list) else None
        if pics and len(pics) > 4:
            dropped = len(pics) - 4
            pics = pics[:4]
            if not silent:
                # The client caps at 4 too, so this only fires for an older
                # client or a bug — but silently dropping images with no
                # explanation is worse either way.
                await self._broadcast(
                    {"type": "error", "message": f"Only 4 photos go with one message — {dropped} not sent."}
                )
        text = (text or "").strip()
        if not text and not pics:
            return
        if text.startswith("/add-dir "):
            raw_dir = text.split(None, 1)[1].strip().strip("'\"")
            extra = Path(raw_dir).expanduser()
            try:
                extra = extra.resolve()
            except OSError:
                pass
            if extra.is_dir() and str(extra) not in self.extra_dirs:
                self.extra_dirs.append(str(extra))
        await self.ensure()
        if self._proc is None or self._proc.stdin is None:
            await self._broadcast({"type": "error", "message": self.last_error or "Claude is not running on this seat"})
            return
        user_id = str(uuid.uuid4())
        event = {"type": "user", "id": user_id, "text": text, "images": bool(pics)}
        if not silent:
            self._remember(event)
            await self._broadcast(event)
        self._busy = True
        self.suggestions = []
        if not silent:
            await self._broadcast({"type": "status", "busy": True, "label": "Working…"})
        await self._write(encode_user(text, self.translator.session_id, pics))

    async def signal_submit(self, session_id: str, *, wait: float | None = None) -> dict[str, Any] | None:
        """Fire the UserPromptSubmit hook without starting a turn.

        Deliberately not send_user(): that flips _busy and the hook exits 2, so no
        turn-end would ever arrive to clear it and the phone would sit on "Working…".
        """
        if self.config_dir is None:
            return None
        clear_submit(self.config_dir)
        await self.ensure()
        if self._proc is None or self._proc.stdin is None:
            return None
        text = SUBMIT_SENTINEL.format(session_id=session_id or "")
        # The hook exits 2, which still closes out a (zero-turn, zero-cost) result.
        # Swallow it so the phone gets no phantom turn or 0-cost usage event.
        self._swallow_result = True
        await self._write(encode_user(text, self.translator.session_id, None))
        if wait is None:
            wait = submit_wait()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(wait, 0.0)
        while True:
            found = read_submit(self.config_dir)
            if found is not None:
                return found
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.1)

    async def interrupt(self) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        await self._write(encode_interrupt())
        self._busy = False
        await self._broadcast({"type": "status", "busy": False, "label": "Stopped"})

    async def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            await self._broadcast({"type": "error", "message": f"unknown mode {mode}"})
            return
        if self._proc is None or self._proc.stdin is None:
            await self._broadcast({"type": "error", "message": "not connected yet"})
            return
        # Every mode, plan included, goes over the control channel with a
        # real CLI mode name (see CLI_MODE) — plan used to be entered by
        # sending the literal chat text "/plan", which is not a Claude Code
        # slash command and never actually changed the running mode.
        await self._write(encode_mode(mode))
        self.translator.mode = mode
        await self._broadcast({"type": "mode", "mode": mode})

    async def answer_permission(self, request_id: str, allow: bool) -> None:
        if self._proc is None or self._proc.stdin is None or not request_id:
            return
        payload = self.translator.pop_permission_input(request_id)
        await self._write(encode_permission(request_id, allow, payload))
        resolved = "allowed" if allow else "denied"
        for item in self._history:
            if item.get("type") == "permission" and item.get("request_id") == request_id:
                item["resolved"] = resolved
        await self._broadcast({"type": "permission", "request_id": request_id, "resolved": resolved})

    def refresh_quota(self) -> dict[str, Any] | None:
        if self.config_dir is None:
            return self.quota
        payload = read_usage_file(self.config_dir / "last-usage.json")
        if payload:
            self.quota = payload
        return self.quota

    def _salon_session_id(self) -> str | None:
        from .gate import gate

        return gate.snapshot().get("session_id")

    def _limit_from_events(self, events: list[dict[str, Any]]) -> Any:
        for event in events:
            if event.get("type") != "error":
                continue
            info = parse_limit_error(event.get("message"))
            if info:
                return info
        return parse_limit_error(self.last_error)

    def _spare(self) -> Account | None:
        """A salon account to fail over to, or None on a guest's own account.

        Moving a BYO session onto a salon account would put the guest's work on
        the salon's billing mid-sentence, without either of them agreeing to it.
        The host can still switch deliberately from the desk.
        """
        if self.byo:
            return None
        return self.pool.pick(excluding=self.account_label)

    def failover_plan(self, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Decide compact / switch / opus / none. Pure enough to unit-test."""
        events = events or []
        if self._phase == "switching":
            return {"action": "none"}
        if self._phase == "compacting":
            spare = self._spare()
            return {"action": "switch" if spare else "no_spare", "account": spare, "reason": "compacted"}
        limit = self._limit_from_events(events)
        if limit and limit.kind == "opus":
            return {"action": "opus", "limit": limit}
        spare = self._spare()
        if limit and limit.kind in {"session", "weekly", "daily", "usage"}:
            if not spare:
                return {"action": "no_spare", "limit": limit}
            return {"action": "switch", "account": spare, "limit": limit, "reason": limit.kind}
        self.refresh_quota()
        window = quota_over_threshold(self.quota)
        if window:
            if not spare:
                return {"action": "none", "window": window}
            return {"action": "compact", "account": spare, "window": window}
        return {"action": "none"}

    async def _on_turn_end(self, events: list[dict[str, Any]]) -> None:
        self.refresh_quota()
        if self.quota:
            await self._broadcast({"type": "quota", **self.quota})
        plan = self.failover_plan(events)
        action = plan.get("action")
        if action == "opus":
            await self._broadcast(
                {"type": "status", "busy": False, "label": "Opus limit — continuing on Sonnet"}
            )
            self._failover_task = asyncio.create_task(self.send_user("/model sonnet"))
            return
        if action == "no_spare":
            message = (
                "Your Claude account hit a usage limit. The salon can take over on "
                "one of its own accounts — ask the host."
                if self.byo
                else "Claude account hit a usage limit and no spare login is ready on this seat."
            )
            await self._broadcast({"type": "error", "message": message})
            return
        if action == "compact":
            self._phase = "compacting"
            await self._broadcast(
                {
                    "type": "status",
                    "busy": True,
                    "label": "Usage limit approaching — compacting, then switching accounts…",
                }
            )
            self._failover_task = asyncio.create_task(self.send_user(COMPACT_CMD, silent=True))
            return
        if action == "switch":
            account = plan.get("account")
            if not isinstance(account, Account):
                return
            reason = str(plan.get("reason") or "usage")
            limit = plan.get("limit")
            until = getattr(limit, "resets_at", None)
            self._failover_task = asyncio.create_task(self.switch_account(account, reason=reason, limited_until=until))

    async def switch_account(
        self,
        account: Account,
        *,
        reason: str,
        limited_until: float | None = None,
        primer: str | None = None,
    ) -> None:
        previous = self.account_label
        self._phase = "switching"
        summary = primer if primer is not None else collect_summary(self.config_dir, self._history)
        if summary:
            self.handoff_text = summary
            write_handoff(self._salon_session_id(), summary)
        if previous:
            self.pool.mark_limited(previous, limited_until)
        history = list(self._history)
        self._stop_process()
        self.translator = GuestTranslator()
        self.last_error = None
        self.last_usage = None
        self.quota = None
        self.suggestions = []
        self._history[:] = history
        self.assign_account(account)
        await self.ensure()
        if summary:
            await self.send_user(PRIMER.format(summary=summary), silent=True)
        await self._broadcast(
            {
                "type": "account",
                "label": account.label,
                "previous": previous,
                "reason": reason,
                "handoff": bool(summary),
            }
        )
        self._phase = "idle"

    def _should_swallow(self, obj: dict[str, Any]) -> bool:
        """Drop the zero-turn result the sentinel's blocked prompt leaves behind."""
        if not self._swallow_result or obj.get("type") != "result":
            return False
        self._swallow_result = False
        return True

    async def _write(self, obj: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            return
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

    async def _pump_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if self._should_swallow(obj):
                    continue
                events = self.translator.feed(obj)
                for event in events:
                    kind = event.get("type")
                    if kind == "status":
                        self._busy = bool(event.get("busy"))
                    if kind == "usage":
                        self.last_usage = event
                    if kind == "error":
                        self.last_error = str(event.get("message") or self.last_error or "")
                    if kind == "suggestion":
                        text = str(event.get("text") or "")
                        if text and text not in self.suggestions:
                            self.suggestions.append(text)
                    compacting = self._phase == "compacting" and kind not in {"status", "error", "turn", "usage"}
                    if not compacting:
                        self._remember(event)
                        await self._broadcast(event)
                if any(event.get("type") in {"turn", "error"} for event in events):
                    await self._on_turn_end(events)
        except asyncio.CancelledError:
            return
        except (BrokenPipeError, ConnectionResetError):
            return
        except (ValueError, asyncio.LimitOverrunError):
            # A line the reader cannot return leaves it out of step with the
            # stream, so this process is finished whatever we do. Say so: going
            # quiet here is what left a guest watching a spinner that would
            # never stop, with nothing in the logs either.
            self.last_error = "Claude Code sent more in one go than the seat could read — the turn was cut short. Ask again, and avoid opening large images."
            await self._broadcast({"type": "error", "message": self.last_error})
            return
        finally:
            self._busy = False
            if self._proc is not None and self._proc.returncode not in (None, 0):
                self.last_error = "Claude Code exited"
                await self._broadcast({"type": "error", "message": self.last_error})

    async def _pump_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            while True:
                raw = await self._proc.stderr.readline()
                if not raw:
                    break
        except (asyncio.CancelledError, BrokenPipeError):
            return

    def _remember(self, event: dict[str, Any]) -> None:
        item = _history_item(event)
        if not item:
            return
        kind = item.get("type")
        key = "request_id" if kind == "permission" else "id"
        if kind in {"tool", "ask", "assistant", "thinking", "permission"}:
            for existing in self._history:
                if existing.get("type") == kind and existing.get(key) == item.get(key):
                    if item.get("delta"):
                        existing["text"] = (existing.get("text") or "") + (item.get("text") or "")
                        existing["done"] = item.get("done")
                    else:
                        existing.update({k: v for k, v in item.items() if k != "delta"})
                    existing.pop("delta", None)
                    if existing.get("shots"):
                        self._trim_shots()
                    return
            stored = {k: v for k, v in item.items() if k != "delta"}
            self._history.append(stored)
            if stored.get("shots"):
                self._trim_shots()
            return
        if kind == "usage":
            self._history = [h for h in self._history if h.get("type") != "usage"]
        self._history.append(item)

    def _trim_shots(self) -> None:
        """Keep the pixels on the last few screenshots and drop the rest.

        Every reconnect re-sends the whole history, and a phone in a cafe
        reconnects a lot. The tool card stays where it was -- it goes back to
        naming the image the way it did before there was anywhere to show it.
        """
        kept = 0
        for item in reversed(self._history):
            if not item.get("shots"):
                continue
            kept += 1
            if kept > SHOT_HISTORY:
                item.pop("shots", None)
                item["output"] = item.get("output") or "[image]"

    async def _broadcast(self, event: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def _send(self, ws: WebSocket, event: dict[str, Any]) -> None:
        try:
            await ws.send_json(event)
        except Exception:
            self._clients.discard(ws)


session = ClaudeChat()
