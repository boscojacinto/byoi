#!/usr/bin/env python3
"""Stream-json stand-in for Claude Code. Used to simulate quota failover in the browser."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

CONFIG = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
CONFIG.mkdir(parents=True, exist_ok=True)
LABEL = CONFIG.name
SESSION = f"fake-{LABEL}-{os.getpid()}"
CWD = os.getcwd()


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def write_usage(pct: float) -> Path:
    transcript = CONFIG / "projects" / "sim" / f"{SESSION}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "transcript_path": str(transcript),
        "rate_limits": {
            "five_hour": {"used_percentage": pct, "resets_at": int(time.time()) + 3600},
            "seven_day": {"used_percentage": 12.0, "resets_at": int(time.time()) + 86400 * 4},
        },
    }
    path = CONFIG / "last-usage.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return transcript


def write_compact(summary: str) -> None:
    transcript = write_usage(82)
    line = {
        "type": "system",
        "subtype": "compact_boundary",
        "isCompactSummary": True,
        "summary": summary,
    }
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def user_text(msg: dict) -> str:
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def main() -> None:
    inited = False
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or msg.get("type") != "user":
            continue
        if not inited:
            emit(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": SESSION,
                    "model": "fake-sonnet",
                    "cwd": CWD,
                    "permissionMode": "acceptEdits",
                }
            )
            inited = True
        text = user_text(msg)
        low = text.lower()
        if low.startswith("/compact"):
            write_compact("Kept the slip QR contrast work and the failing scan test.")
            emit(
                {
                    "type": "assistant",
                    "message": {
                        "id": "compact-1",
                        "content": [{"type": "text", "text": "Compacted the session for handoff."}],
                    },
                }
            )
            emit({"type": "result", "subtype": "success", "total_cost_usd": 0.02, "duration_ms": 50, "num_turns": 1})
            continue
        if "account switch" in low or "compacted summary" in low:
            write_usage(4)
            emit(
                {
                    "type": "assistant",
                    "message": {
                        "id": "primer-1",
                        "content": [
                            {
                                "type": "text",
                                "text": "Continuing on the spare account. Workspace is unchanged.",
                            }
                        ],
                    },
                }
            )
            emit({"type": "result", "subtype": "success", "total_cost_usd": 0.01, "duration_ms": 30, "num_turns": 1})
            continue
        # Preferred account (claude-seat-1) reports 81% so the seat compact-then-switches.
        hot = LABEL.endswith("seat-1") or LABEL in {"a", "claude-seat-1"}
        write_usage(81 if hot else 9)
        reply = "I'll bump the QR contrast on the slip." if hot else "Still here on the spare login."
        emit(
            {
                "type": "assistant",
                "message": {"id": f"a-{os.getpid()}", "content": [{"type": "text", "text": reply}]},
            }
        )
        emit({"type": "result", "subtype": "success", "total_cost_usd": 0.05, "duration_ms": 80, "num_turns": 1})


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
