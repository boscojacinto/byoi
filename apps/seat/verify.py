"""One-shot Claude Code run that grades a shipped solution against a host spec."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .claude_chat import CLAUDE_BIN, default_workspace

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "passed": {"type": "integer"},
        "failed": {"type": "integer"},
        "cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "pass": {"type": "boolean"},
                    "detail": {"type": "string"},
                },
                "required": ["name", "pass"],
            },
        },
    },
    "required": ["cases"],
}


def verify_prompt(*, title: str, spec: str) -> str:
    return (
        "You are the BYOI salon verifier. A guest just shipped a solution in this repo.\n"
        f"Brief title: {title or '(untitled)'}\n\n"
        "SPEC (acceptance tests written by the host):\n"
        f"{spec.strip()}\n\n"
        "Inspect the working tree. Run the project's own tests if they exist "
        "(pytest, npm test, go test, etc.). For each distinct requirement in the spec, "
        "emit one test case with pass true or false and a short detail.\n"
        "Do not rewrite the guest's product code; you may add a temporary test file if needed.\n"
        "Return structured output only."
    )


def _extract_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty verifier output")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    last = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    if isinstance(last, dict):
        return last
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        data = json.loads(text[start : end + 1])
        if isinstance(data, dict):
            return data
    raise ValueError("verifier did not return JSON")


def parse_verify_output(raw: str) -> dict[str, Any]:
    data = _extract_json(raw)
    payload: Any = data.get("structured_output")
    if payload is None:
        result = data.get("result")
        payload = result if isinstance(result, dict) else data
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {"summary": payload, "cases": []}
    if not isinstance(payload, dict):
        payload = {"summary": str(payload), "cases": []}
    cases: list[dict[str, Any]] = []
    for item in payload.get("cases") or []:
        if not isinstance(item, dict):
            continue
        ok = item.get("pass")
        if ok is None:
            ok = item.get("passed")
        cases.append(
            {
                "name": str(item.get("name") or item.get("id") or "case"),
                "pass": bool(ok),
                "detail": str(item.get("detail") or item.get("message") or ""),
            }
        )
    passed = sum(1 for c in cases if c["pass"])
    failed = sum(1 for c in cases if not c["pass"])
    return {
        "summary": str(payload.get("summary") or data.get("result") or ""),
        "passed": int(payload.get("passed") if payload.get("passed") is not None else passed),
        "failed": int(payload.get("failed") if payload.get("failed") is not None else failed),
        "cases": cases,
    }


def run_verify(*, spec: str, title: str = "", cwd: str | None = None) -> dict[str, Any]:
    if not (spec or "").strip():
        return {"summary": "No spec on this brief.", "passed": 0, "failed": 0, "cases": []}
    work = Path(cwd).expanduser().resolve() if cwd else default_workspace()
    if not work.is_dir():
        raise FileNotFoundError(f"not a directory: {work}")
    binary = shutil.which(os.environ.get("BYOI_CLAUDE", CLAUDE_BIN)) or CLAUDE_BIN
    argv = [
        binary,
        "-p",
        verify_prompt(title=title, spec=spec),
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCHEMA),
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read,Bash,Grep,Glob,Edit,Write",
    ]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("BYOI_VERIFY_TIMEOUT", "180")),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("claude is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("verifier timed out") from exc
    raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode != 0 and not raw:
        raise RuntimeError(proc.stderr.strip() or f"claude exited {proc.returncode}")
    try:
        return parse_verify_output(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not parse verifier output: {exc}") from exc
