"""Draft a solution's Brief, Specs & QA, and time budget from just a title.

Runs on the desk's host Claude account (the same one `testgen.py` uses to
author acceptance suites) with read-only access to the project's own
checkout, so the draft is grounded in what's actually in the repo rather
than a guess from the title alone. The host reviews and edits before adding
the solution — this only ever proposes, never publishes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from . import projects as project_ops
from . import testgen

MIN_WELLNESS = 15
MAX_WELLNESS = 240
MIN_BREAK = 10
MAX_BREAK = 180

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "brief": {"type": "string"},
        "spec": {"type": "string"},
        "wellness_minutes": {"type": "integer"},
        "break_after": {"type": "integer"},
    },
    "required": ["brief", "spec", "wellness_minutes", "break_after"],
}


class BoardSuggestError(RuntimeError):
    """The draft couldn't be produced — host account not logged in, timeout, etc."""


def suggest_prompt(*, title: str, project_name: str) -> str:
    return (
        "You are the BYOI salon brief author. A host wants to add a task named "
        f'"{title}" to the project "{project_name}".\n'
        "Look at the repository in your current working directory — its stack, "
        "size, and conventions — then draft:\n\n"
        "- brief: a short paragraph describing what \"done\" looks like for this "
        "task, specific to what you find in this repo.\n"
        "- spec: a Specs & QA checklist, one plain-English fact per line, each "
        "phrased so a blind test-writer (who will NOT see the solution) could "
        "turn it into a concrete test against the repo's own files — same style "
        "as an acceptance spec, not implementation notes.\n"
        "- wellness_minutes: a realistic single-session time budget for this "
        f"task, an integer between {MIN_WELLNESS} and {MAX_WELLNESS}.\n"
        "- break_after: minutes into that session before a break is suggested, "
        f"an integer between {MIN_BREAK} and {MAX_BREAK}, always less than "
        "wellness_minutes.\n\n"
        "Base the complexity estimate on what the task actually touches in this "
        "repo, not a generic guess. Return structured output only."
    )


def _clamp(value: Any, *, low: int, high: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def parse_suggestion(raw: str) -> dict[str, Any]:
    from apps.seat.verify import _extract_json

    try:
        data = _extract_json(raw)
    except ValueError as exc:
        raise BoardSuggestError(f"could not parse the suggestion: {exc}") from exc
    payload: Any = data.get("structured_output")
    if payload is None:
        result = data.get("result")
        payload = result if isinstance(result, dict) else data
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise BoardSuggestError("generator did not return a suggestion") from exc
    if not isinstance(payload, dict):
        raise BoardSuggestError("generator did not return a suggestion")
    brief = str(payload.get("brief") or "").strip()
    spec = str(payload.get("spec") or "").strip()
    if not brief or not spec:
        raise BoardSuggestError("generator left the brief or spec empty")
    wellness = _clamp(payload.get("wellness_minutes"), low=MIN_WELLNESS, high=MAX_WELLNESS, default=90)
    break_after = _clamp(payload.get("break_after"), low=MIN_BREAK, high=MAX_BREAK, default=50)
    if break_after >= wellness:
        break_after = max(MIN_BREAK, wellness // 2)
    return {
        "brief": brief,
        "spec": spec,
        "wellness_minutes": wellness,
        "break_after": break_after,
    }


def suggest_solution(*, title: str, project: dict[str, Any]) -> dict[str, Any]:
    """Stage: runs on the host account, grounded in the project's own checkout."""
    config_dir = testgen.host_config_dir()
    if not (config_dir / ".credentials.json").is_file():
        raise BoardSuggestError(
            f"host Claude account '{testgen.host_account_label()}' is not logged in "
            f"(scripts/seat-claude-login.sh --account {testgen.host_account_label()})"
        )
    try:
        local_path = project_ops.ensure_local(project)
    except FileNotFoundError as exc:
        raise BoardSuggestError(str(exc)) from exc
    except RuntimeError as exc:
        raise BoardSuggestError(f"could not fetch this project: {exc}") from exc
    binary = shutil.which(testgen.CLAUDE_BIN) or testgen.CLAUDE_BIN
    prompt = suggest_prompt(title=title, project_name=project.get("name") or "")
    argv = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCHEMA),
        "--allowedTools",
        "Read,Glob,Grep",
    ]
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}
    try:
        proc = subprocess.run(
            argv, cwd=local_path, capture_output=True, text=True, timeout=testgen.timeout_s(), env=env
        )
    except FileNotFoundError as exc:
        raise BoardSuggestError("claude is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise BoardSuggestError("suggestion generation timed out") from exc
    raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode != 0 and not raw:
        raise BoardSuggestError(proc.stderr.strip() or f"claude exited {proc.returncode}")
    return parse_suggestion(raw)
