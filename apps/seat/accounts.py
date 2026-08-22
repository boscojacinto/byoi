"""Seat-local Claude account pool, quota watch, and compact handoff."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
COMPACT_CMD = (
    "/compact Keep the current task, files touched, decisions, failing tests, and next steps."
)
PRIMER = (
    "You are continuing a salon guest session after an account switch. "
    "The working directory is unchanged. Do not greet as if starting fresh. "
    "Here is the compacted summary of prior work:\n\n{summary}"
)
STATUSLINE_SH = """#!/bin/sh
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cat > "$dir/last-usage.json"
echo "byoi"
"""
POSTCOMPACT_SH = """#!/bin/sh
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cat > "$dir/last-compact.json"
"""
STOPFAILURE_SH = """#!/bin/sh
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cat > "$dir/last-stopfailure.json"
"""
SUBMIT_MARK = "__byoi_submit__"
# UserPromptSubmit has no matcher, so this runs on every guest prompt: stay cheap
# and silent unless the seat injected the sentinel. Exit 2 erases the sentinel so
# it never reaches the model.
SUBMIT_SH = """#!/bin/sh
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
payload=$(cat)
case "$payload" in
  *__byoi_submit__*) ;;
  *) exit 0 ;;
esac
printf '%s' "$payload" > "$dir/last-submit.json"
echo "byoi: submission recorded" >&2
exit 2
"""


def accounts_dir() -> Path:
    raw = os.environ.get("BYOI_CLAUDE_ACCOUNTS_DIR", "").strip()
    path = Path(raw).expanduser() if raw else ROOT / "data" / "claude-accounts"
    return path.resolve()


def handoffs_dir() -> Path:
    raw = os.environ.get("BYOI_HANDOFFS_DIR", "").strip()
    path = Path(raw).expanduser() if raw else ROOT / "data" / "handoffs"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def quota_threshold() -> float:
    try:
        return float(os.environ.get("BYOI_QUOTA_FAILOVER_PCT", "80"))
    except ValueError:
        return 80.0


def preferred_label(seat_id: str | None = None) -> str | None:
    env = os.environ.get("BYOI_CLAUDE_ACCOUNT", "").strip()
    if env:
        return env
    sid = (seat_id or os.environ.get("BYOI_SEAT_ID") or "").strip()
    if not sid:
        return None
    if sid.startswith("claude-"):
        return sid
    return f"claude-{sid}"


@dataclass
class Account:
    label: str
    config_dir: Path
    limited_until: float | None = None

    @property
    def credentialed(self) -> bool:
        return (self.config_dir / ".credentials.json").is_file()

    def is_limited(self, now: float | None = None) -> bool:
        if self.limited_until is None:
            return False
        return self.limited_until > (now if now is not None else time.time())

    def usable(self, now: float | None = None) -> bool:
        return self.credentialed and not self.is_limited(now)

    def snapshot(self, *, in_use: bool = False) -> dict[str, Any]:
        return {
            "label": self.label,
            "credentialed": self.credentialed,
            "limited": self.is_limited(),
            "limited_until": self.limited_until,
            "in_use": in_use,
        }


@dataclass
class LimitInfo:
    kind: str
    message: str
    resets_at: float | None = None


@dataclass
class AccountPool:
    _limited: dict[str, float] = field(default_factory=dict)

    def discover(self) -> list[Account]:
        found: list[Account] = []
        root = accounts_dir()
        if root.is_dir():
            for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if child.is_dir() and not child.name.startswith("."):
                    found.append(self._hydrate(child.name, child.resolve()))
        if not found:
            found.append(self._hydrate("default", (Path.home() / ".claude").resolve()))
        return found

    def _hydrate(self, label: str, config_dir: Path) -> Account:
        until = self._limited.get(label)
        return Account(label=label, config_dir=config_dir, limited_until=until)

    def get(self, label: str | None) -> Account | None:
        if not label:
            return None
        for acct in self.discover():
            if acct.label == label:
                return acct
        return None

    def pick(self, *, preferred: str | None = None, excluding: str | None = None) -> Account | None:
        accounts = self.discover()
        usable = [a for a in accounts if a.usable() and a.label != excluding]
        if preferred:
            for acct in usable:
                if acct.label == preferred:
                    return acct
        return usable[0] if usable else None

    def mark_limited(self, label: str | None, until: float | None) -> None:
        if not label:
            return
        self._limited[label] = until if until is not None else default_until("session")

    def snapshot(self, current: str | None = None) -> list[dict[str, Any]]:
        return [a.snapshot(in_use=a.label == current) for a in self.discover()]

    def ensure_hooks(self, account: Account) -> None:
        """Status-line, PostCompact, and submit scripts so -p sessions dump state to disk."""
        dest = account.config_dir
        dest.mkdir(parents=True, exist_ok=True)
        scripts = {
            "byoi-statusline.sh": STATUSLINE_SH,
            "byoi-postcompact.sh": POSTCOMPACT_SH,
            "byoi-stopfailure.sh": STOPFAILURE_SH,
            "byoi-submit.sh": SUBMIT_SH,
        }
        for name, body in scripts.items():
            path = dest / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        settings_path = dest / "settings.json"
        settings: dict[str, Any] = {}
        if settings_path.is_file():
            try:
                loaded = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    settings = loaded
            except json.JSONDecodeError:
                settings = {}
        settings["statusLine"] = {
            "type": "command",
            "command": str(dest / "byoi-statusline.sh"),
        }
        hooks = settings.setdefault("hooks", {})
        hooks["PostCompact"] = [
            {
                "hooks": [
                    {"type": "command", "command": str(dest / "byoi-postcompact.sh")},
                ]
            }
        ]
        hooks["UserPromptSubmit"] = [
            {
                "hooks": [
                    {"type": "command", "command": str(dest / "byoi-submit.sh")},
                ]
            }
        ]
        hooks["StopFailure"] = [
            {
                "matcher": "rate_limit",
                "hooks": [
                    {"type": "command", "command": str(dest / "byoi-stopfailure.sh")},
                ],
            }
        ]
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


pool = AccountPool()


def parse_usage_payload(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    rl = data.get("rate_limits") if isinstance(data.get("rate_limits"), dict) else {}
    five = rl.get("five_hour") if isinstance(rl.get("five_hour"), dict) else {}
    week = rl.get("seven_day") if isinstance(rl.get("seven_day"), dict) else {}
    out = {
        "five_hour": _pct(five.get("used_percentage")),
        "five_hour_resets": five.get("resets_at"),
        "seven_day": _pct(week.get("used_percentage")),
        "seven_day_resets": week.get("resets_at"),
        "transcript_path": data.get("transcript_path"),
    }
    if all(out[k] is None for k in ("five_hour", "seven_day", "transcript_path")):
        if "rate_limits" not in data and "transcript_path" not in data:
            return None
    return out


def read_usage_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parse_usage_payload(data if isinstance(data, dict) else None)


def quota_over_threshold(
    quota: dict[str, Any] | None, threshold: float | None = None
) -> str | None:
    if not quota:
        return None
    cut = quota_threshold() if threshold is None else threshold
    five = quota.get("five_hour")
    if five is not None and float(five) >= cut:
        return "five_hour"
    week = quota.get("seven_day")
    if week is not None and float(week) >= cut:
        return "seven_day"
    return None


def _pct(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_LIMIT_KINDS = (
    (re.compile(r"opus limit", re.I), "opus"),
    (re.compile(r"weekly limit", re.I), "weekly"),
    (re.compile(r"session limit", re.I), "session"),
    (re.compile(r"daily limit", re.I), "daily"),
    (re.compile(r"hit your (?:usage )?limit", re.I), "usage"),
    (re.compile(r"usage limit reached", re.I), "usage"),
)
_RESETS = re.compile(r"resets\s+(.+?)(?:\s*$|\s{2,})", re.I)


def parse_limit_error(message: str | None) -> LimitInfo | None:
    text = str(message or "").strip()
    if not text:
        return None
    kind: str | None = None
    for pat, name in _LIMIT_KINDS:
        if pat.search(text):
            kind = name
            break
    if not kind:
        return None
    resets_at = parse_resets(text, kind)
    return LimitInfo(kind=kind, message=text, resets_at=resets_at)


def parse_resets(text: str, kind: str) -> float:
    match = _RESETS.search(text)
    if match:
        parsed = _parse_reset_phrase(match.group(1).strip())
        if parsed is not None:
            return parsed
    return default_until(kind)


def default_until(kind: str) -> float:
    now = time.time()
    if kind == "session":
        return now + 5 * 3600
    if kind == "weekly":
        local = datetime.now()
        days = (7 - local.weekday()) % 7
        if days == 0:
            days = 7
        nxt = (local + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        return nxt.timestamp()
    return now + 24 * 3600


def _parse_reset_phrase(phrase: str) -> float | None:
    raw = phrase.strip().rstrip(".")
    if not raw:
        return None
    local = datetime.now()
    for fmt in ("%I:%M%p", "%I:%M %p", "%H:%M"):
        try:
            clock = datetime.strptime(raw.replace(" ", "").lower() if "m" in raw.lower() else raw, fmt)
            candidate = local.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
            if candidate.timestamp() <= time.time():
                candidate += timedelta(days=1)
            return candidate.timestamp()
        except ValueError:
            continue
    try:
        clock = datetime.strptime(re.sub(r"\s+", " ", raw.lower()), "%a %I:%M%p")
        days = (clock.weekday() - local.weekday()) % 7
        candidate = local.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
        candidate += timedelta(days=days or 7)
        return candidate.timestamp()
    except ValueError:
        return None


def _flatten_text(obj: dict[str, Any]) -> str:
    if isinstance(obj.get("summary"), str) and obj["summary"].strip():
        return obj["summary"].strip()
    for key in ("compactSummary", "compact_summary", "text"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    content = obj.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {None, "text"}:
                text = str(block.get("text") or "").strip()
                if text:
                    parts.append(text)
            elif isinstance(block, str) and block.strip():
                parts.append(block.strip())
        if parts:
            return "\n".join(parts)
    message = obj.get("message")
    if isinstance(message, dict):
        nested = _flatten_text(message)
        if nested:
            return nested
    if isinstance(message, str) and message.strip():
        return message.strip()
    return ""


def _looks_like_compact(obj: dict[str, Any]) -> str:
    if obj.get("isCompactSummary") or obj.get("is_compact_summary"):
        return _flatten_text(obj)
    kind = f"{obj.get('type') or ''} {obj.get('subtype') or ''}".lower()
    if "compact" in kind and "pre" not in kind:
        return _flatten_text(obj)
    for key in ("compactSummary", "compact_summary"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def extract_compact_summary(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    last: str | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        text = _looks_like_compact(obj)
        if text:
            last = text
    return last


def find_latest_transcript(config_dir: Path | None) -> Path | None:
    if config_dir is None:
        return None
    usage = read_usage_file(config_dir / "last-usage.json")
    if usage and usage.get("transcript_path"):
        candidate = Path(str(usage["transcript_path"]))
        if candidate.is_file():
            return candidate
    compact_meta = config_dir / "last-compact.json"
    if compact_meta.is_file():
        try:
            data = json.loads(compact_meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data.get("transcript_path"):
            candidate = Path(str(data["transcript_path"]))
            if candidate.is_file():
                return candidate
    projects = config_dir / "projects"
    if not projects.is_dir():
        return None
    latest: Path | None = None
    latest_mtime = -1.0
    for jsonl in projects.rglob("*.jsonl"):
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        if mtime > latest_mtime:
            latest = jsonl
            latest_mtime = mtime
    return latest


def history_fallback(history: list[dict[str, Any]], n: int = 20) -> str:
    lines: list[str] = []
    for item in history:
        kind = item.get("type") or item.get("kind")
        if kind not in {"user", "assistant"}:
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{kind}: {text}")
    chunk = lines[-n:]
    return "\n\n".join(chunk)[:12000]


def collect_summary(config_dir: Path | None, history: list[dict[str, Any]]) -> str:
    transcript = find_latest_transcript(config_dir)
    summary = extract_compact_summary(transcript) if transcript else None
    if summary:
        return summary
    return history_fallback(history)


def write_handoff(session_id: str | None, markdown: str) -> Path:
    sid = (session_id or "seat").strip() or "seat"
    path = handoffs_dir() / f"{sid}.md"
    path.write_text(markdown or "", encoding="utf-8")
    return path


def read_handoff(session_id: str | None) -> str | None:
    sid = (session_id or "").strip()
    if not sid:
        return None
    path = handoffs_dir() / f"{sid}.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def submit_path(config_dir: Path | None) -> Path | None:
    if config_dir is None:
        return None
    return Path(config_dir) / "last-submit.json"


def read_submit(config_dir: Path | None) -> dict[str, Any] | None:
    """The payload byoi-submit.sh captured, or None if the hook has not fired."""
    path = submit_path(config_dir)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def clear_submit(config_dir: Path | None) -> None:
    """Drop a previous guest's submission so it cannot be read as this one's."""
    path = submit_path(config_dir)
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass
