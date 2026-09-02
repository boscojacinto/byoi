"""Day/hour usage breakdown for a Claude account, parsed from its own transcripts.

Mirrors what `claude usage`-style tools show: token counts bucketed by day and
by hour, read straight out of the JSONL transcripts Claude Code already writes
under the account's config dir. No external pricing table — tokens only.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_FILES = 40
MAX_LINES = 40_000


def _num(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bucket() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
        "messages": 0,
    }


def _add(bucket: dict[str, int], usage: dict[str, Any]) -> None:
    inp = _num(usage.get("input_tokens"))
    out = _num(usage.get("output_tokens"))
    creation = _num(usage.get("cache_creation_input_tokens"))
    read = _num(usage.get("cache_read_input_tokens"))
    bucket["input_tokens"] += inp
    bucket["output_tokens"] += out
    bucket["cache_creation_tokens"] += creation
    bucket["cache_read_tokens"] += read
    bucket["total_tokens"] += inp + out + creation + read
    bucket["messages"] += 1


def _transcripts(config_dir: Path) -> list[Path]:
    projects = config_dir / "projects"
    if not projects.is_dir():
        return []
    files: list[tuple[float, Path]] = []
    for jsonl in projects.rglob("*.jsonl"):
        try:
            files.append((jsonl.stat().st_mtime, jsonl))
        except OSError:
            continue
    files.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _mtime, path in files[:MAX_FILES]]


def usage_report(config_dir: Path | None, *, days: int = 14, hours: int = 48) -> dict[str, Any]:
    empty = {"daily": [], "hourly": [], "totals": _bucket(), "window_days": days}
    if config_dir is None:
        return empty
    config_dir = Path(config_dir)

    daily: dict[str, dict[str, int]] = {}
    hourly: dict[str, dict[str, int]] = {}
    totals = _bucket()
    lines_read = 0

    for path in _transcripts(config_dir):
        if lines_read >= MAX_LINES:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if lines_read >= MAX_LINES:
                break
            line = line.strip()
            if not line:
                continue
            lines_read += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            ts_raw = obj.get("timestamp")
            if not isinstance(ts_raw, str):
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone()
            except ValueError:
                continue
            day_key = ts.strftime("%Y-%m-%d")
            hour_key = ts.strftime("%Y-%m-%dT%H:00")
            _add(daily.setdefault(day_key, _bucket()), usage)
            _add(hourly.setdefault(hour_key, _bucket()), usage)
            _add(totals, usage)

    daily_sorted = [{"date": k, **v} for k, v in sorted(daily.items(), reverse=True)][:days]
    hourly_sorted = [{"hour": k, **v} for k, v in sorted(hourly.items(), reverse=True)][:hours]
    return {"daily": daily_sorted, "hourly": hourly_sorted, "totals": totals, "window_days": days}
