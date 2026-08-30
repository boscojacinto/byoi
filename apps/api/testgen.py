"""Host-side acceptance testing: generate a suite from the spec, run it sandboxed.

Two properties the old seat-side verifier did not have:

* **Blind.** Stage A sees the spec and nothing else — no tools, no repo — so the
  suite cannot be shaped by the code it is meant to judge.
* **Complete.** Stage A must map every spec requirement to a test node. Stage C
  fails any requirement whose test never showed up in the JUnit report, so a
  suite that quietly skips a requirement cannot score 100%.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_BIN = os.environ.get("BYOI_CLAUDE", "claude")
JUNIT_REL = ".byoi/junit.xml"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "framework": {"type": "string"},
        "image": {"type": "string"},
        "setup": {"type": "string"},
        "command": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
        "cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "requirement": {"type": "string"},
                    "node_id": {"type": "string"},
                },
                "required": ["name", "requirement", "node_id"],
            },
        },
    },
    "required": ["framework", "command", "files", "cases"],
}


class TestgenError(RuntimeError):
    """Precondition failure. The desk falls back to the seat verifier and says why."""

    __test__ = False  # pytest collects Test*-named classes otherwise


# --------------------------------------------------------------------------- paths


def host_account_label() -> str:
    return os.environ.get("BYOI_HOST_CLAUDE_ACCOUNT", "claude-host").strip() or "claude-host"


def host_config_dir() -> Path:
    from apps.seat.accounts import accounts_dir

    return accounts_dir() / host_account_label()


def runs_dir() -> Path:
    raw = os.environ.get("BYOI_VERIFY_RUNS_DIR", "").strip()
    path = Path(raw).expanduser() if raw else ROOT / "data" / "verify-runs"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def runtime() -> str:
    """docker | bwrap | none. Auto-detects unless BYOI_TESTGEN_RUNTIME pins it."""
    pinned = os.environ.get("BYOI_TESTGEN_RUNTIME", "").strip().lower()
    if pinned:
        return pinned
    if shutil.which("docker"):
        probe = subprocess.run(["docker", "info"], capture_output=True, text=True)
        if probe.returncode == 0:
            return "docker"
    if shutil.which("bwrap"):
        return "bwrap"
    return "none"


def timeout_s() -> int:
    try:
        return int(os.environ.get("BYOI_TESTGEN_TIMEOUT", "300"))
    except ValueError:
        return 300


# ------------------------------------------------------------------- stage A: write


def generate_prompt(*, title: str, spec: str) -> str:
    return (
        "You are the BYOI salon test author. Write an acceptance suite for a brief.\n"
        "You have NOT seen the solution and must not ask for it. Write the tests the\n"
        "spec deserves, not the tests some implementation would pass.\n\n"
        f"Brief title: {title or '(untitled)'}\n\n"
        "SPEC (written by the host):\n"
        f"{spec.strip()}\n\n"
        "Rules:\n"
        "- Break the spec into every distinct requirement it states.\n"
        "- Every requirement gets at least one entry in `cases`, with `requirement`\n"
        "  quoting the spec clause and `node_id` naming the test that proves it.\n"
        "- Put test files under `.byoi/tests/`. Use paths relative to the repo root.\n"
        f"- `command` must write a JUnit XML report to `{JUNIT_REL}`\n"
        "  (e.g. `pytest -q --junitxml=.byoi/junit.xml .byoi/tests`).\n"
        "- `setup` may install dependencies; it runs with no network, so prefer\n"
        "  what the project already vendors and let it fail soft (`|| true`).\n"
        "- `image` is the container image to run in (e.g. python:3.12-slim).\n"
        "- Test the public behaviour the spec describes. Do not import private\n"
        "  helpers or assume file layout the spec does not state.\n"
        "Return structured output only."
    )


def smoke_prompt(*, title: str, spec: str, url: str) -> str:
    return (
        "You are the BYOI salon smoke-test author. A guest's project has just been\n"
        "deployed and you are checking the running service, not the source.\n"
        "You have NOT seen the code and must not ask for it.\n\n"
        f"Brief title: {title or '(untitled)'}\n"
        f"The deployment is reachable at the environment variable BYOI_TARGET_URL\n"
        f"(currently {url}). Always read the variable; never hard-code the host.\n\n"
        "SPEC (written by the host):\n"
        f"{spec.strip()}\n\n"
        "Rules:\n"
        "- Only requirements observable over HTTP belong here. Skip anything that\n"
        "  can only be checked by reading source, and say so in the summary.\n"
        "- Each such requirement gets an entry in `cases` mapped to a test node.\n"
        "- Put tests under `.byoi/tests/`, paths relative to the working directory.\n"
        f"- `command` must write JUnit XML to `{JUNIT_REL}`.\n"
        "- Only the standard library plus what `image` already ships; `setup` runs\n"
        "  with network access but keep installs minimal.\n"
        "- Follow redirects, allow a few seconds of cold start, and assert on status\n"
        "  codes and response bodies rather than on exact HTML.\n"
        "Return structured output only."
    )


def generate_suite(
    *, spec: str, title: str = "", mode: str = "code", target_url: str = ""
) -> dict[str, Any]:
    """Stage A. Runs on the host account with no tools at all — spec in, plan out."""
    config_dir = host_config_dir()
    if not (config_dir / ".credentials.json").is_file():
        raise TestgenError(
            f"host Claude account '{host_account_label()}' is not logged in "
            f"(scripts/seat-claude-login.sh --account {host_account_label()})"
        )
    binary = shutil.which(CLAUDE_BIN) or CLAUDE_BIN
    prompt = (
        smoke_prompt(title=title, spec=spec, url=target_url)
        if mode == "smoke"
        else generate_prompt(title=title, spec=spec)
    )
    argv = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCHEMA),
        "--allowedTools",
        "",
    ]
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}
    try:
        proc = subprocess.run(
            argv, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout_s(), env=env
        )
    except FileNotFoundError as exc:
        raise TestgenError("claude is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise TestgenError("test generation timed out") from exc
    raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode != 0 and not raw:
        raise TestgenError(proc.stderr.strip() or f"claude exited {proc.returncode}")
    return parse_plan(raw)


def parse_plan(raw: str) -> dict[str, Any]:
    from apps.seat.verify import _extract_json

    try:
        data = _extract_json(raw)
    except ValueError as exc:
        raise TestgenError(f"could not parse the generated suite: {exc}") from exc
    payload: Any = data.get("structured_output")
    if payload is None:
        result = data.get("result")
        payload = result if isinstance(result, dict) else data
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TestgenError("generator did not return a suite") from exc
    if not isinstance(payload, dict):
        raise TestgenError("generator did not return a suite")
    files = [f for f in (payload.get("files") or []) if isinstance(f, dict) and f.get("path")]
    cases = [c for c in (payload.get("cases") or []) if isinstance(c, dict) and c.get("node_id")]
    if not files:
        raise TestgenError("generator produced no test files")
    if not cases:
        raise TestgenError("generator mapped no spec requirements to tests")
    return {
        "framework": str(payload.get("framework") or "pytest"),
        "image": str(payload.get("image") or "python:3.12-slim"),
        "setup": str(payload.get("setup") or ""),
        "command": str(payload.get("command") or ""),
        "files": files,
        "cases": cases,
    }


# ------------------------------------------------------------------ fetch the tree


def fetch_submission(*, source: str, ref: str, dest: Path) -> None:
    """Check the seat's submission ref out into a throwaway dir."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    try:
        subprocess.run(
            ["git", "init", "-q", "."], cwd=str(dest), check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "fetch", "--depth", "1", source, f"{ref}:{ref}"],
            cwd=str(dest),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_s(),
        )
        subprocess.run(
            ["git", "checkout", "-q", "--detach", ref],
            cwd=str(dest),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TestgenError("git is not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise TestgenError((exc.stderr or exc.stdout or "git fetch failed").strip()) from exc
    except subprocess.TimeoutExpired as exc:
        raise TestgenError("fetching the submission timed out") from exc


def write_suite(dest: Path, plan: dict[str, Any]) -> None:
    for item in plan["files"]:
        rel = Path(str(item["path"]))
        if rel.is_absolute() or ".." in rel.parts:
            raise TestgenError(f"generated test path escapes the repo: {rel}")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(item.get("content") or ""), encoding="utf-8")
    (dest / ".byoi").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- stage B: execute


def _clean_env(dest: Path) -> dict[str, str]:
    """Nothing from the desk goes in: no BYOI_*, no token or TLS paths."""
    home = dest / ".byoi" / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def run_argv(
    dest: Path,
    plan: dict[str, Any],
    mode: str,
    *,
    network: bool = False,
    env_extra: dict[str, str] | None = None,
) -> list[str]:
    script = plan["command"]
    if plan.get("setup"):
        script = f"{plan['setup']}\n{script}"
    if mode == "docker":
        image = os.environ.get("BYOI_TESTGEN_IMAGE", "").strip() or plan["image"]
        passthrough: list[str] = []
        for key, value in sorted((env_extra or {}).items()):
            passthrough += ["-e", f"{key}={value}"]
        return [
            "docker", "run", "--rm",
            *(["--network", "bridge"] if network else ["--network", "none"]),
            "--pids-limit", "256",
            "--memory", "2g",
            "--cpus", "2",
            "-u", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp",
            "-e", "HOME=/tmp",
            *passthrough,
            "-v", f"{dest}:/work",
            "-w", "/work",
            image,
            "sh", "-lc", script,
        ]
    if mode == "bwrap":
        return [
            "bwrap",
            *(["--unshare-user", "--unshare-pid", "--unshare-ipc"] if network else ["--unshare-all"]),
            "--die-with-parent",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/etc", "/etc",
            "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--bind", str(dest), str(dest),
            "--chdir", str(dest),
            "sh", "-lc", script,
        ]
    return ["sh", "-lc", script]


def run_suite(
    dest: Path,
    plan: dict[str, Any],
    *,
    network: bool = False,
    env_extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    mode = runtime()
    argv = run_argv(dest, plan, mode, network=network, env_extra=env_extra)
    env = _clean_env(dest)
    env.update(env_extra or {})
    try:
        proc = subprocess.run(
            argv,
            cwd=str(dest),
            capture_output=True,
            text=True,
            timeout=timeout_s(),
            env=env,
        )
    except FileNotFoundError as exc:
        raise TestgenError(f"{mode} is not on PATH") from exc
    except subprocess.TimeoutExpired:
        return {"runtime": mode, "returncode": 124, "output": "the test run timed out", "timeout": True}
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return {
        "runtime": mode,
        "returncode": proc.returncode,
        "output": output[-8000:],
        "timeout": False,
    }


# -------------------------------------------------------------------- stage C: grade


def _testcases(xml_path: Path) -> list[dict[str, Any]]:
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError) as exc:
        raise TestgenError(f"could not read the JUnit report: {exc}") from exc
    out: list[dict[str, Any]] = []
    for node in tree.iter("testcase"):
        failures = [c for c in node if c.tag in {"failure", "error"}]
        skipped = [c for c in node if c.tag == "skipped"]
        detail = ""
        if failures:
            first = failures[0]
            detail = (first.get("message") or (first.text or "")).strip()
        elif skipped:
            detail = (skipped[0].get("message") or "skipped").strip()
        out.append(
            {
                "name": node.get("name") or "",
                "classname": node.get("classname") or "",
                "pass": not failures and not skipped,
                "skipped": bool(skipped),
                "detail": detail[:500],
            }
        )
    return out


def _select(case_node_id: str, reported: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find the reported testcases for a node id.

    Match on the test name first. Runners disagree wildly about `classname`
    (pytest says 'pkg.mod', vitest says the suite title, some say nothing), so
    it is only used to disambiguate when several tests share a name — never to
    reject an otherwise-good match, or every case would fall through to a false
    'completeness' failure.
    """
    node = (case_node_id or "").strip()
    if not node:
        return []
    want = node.split("::")[-1].strip()
    hits = [tc for tc in reported if tc["name"] == want]
    if not hits:
        hits = [tc for tc in reported if want and want in tc["name"]]
    if len(hits) <= 1:
        return hits
    prefix = node.split("::")[0]
    if not prefix or prefix == node:
        return hits
    stem = Path(prefix).with_suffix("").as_posix().replace("/", ".")
    narrowed = [
        tc for tc in hits
        if tc["classname"] and (tc["classname"].endswith(stem) or stem.endswith(tc["classname"]))
    ]
    return narrowed or hits


def grade(plan: dict[str, Any], dest: Path, run: dict[str, Any]) -> dict[str, Any]:
    """Per-requirement results, plus a failed case for anything never tested."""
    xml_path = dest / JUNIT_REL
    reported: list[dict[str, Any]] = []
    if xml_path.is_file():
        reported = _testcases(xml_path)

    cases: list[dict[str, Any]] = []
    missing: list[str] = []
    for case in plan["cases"]:
        node_id = str(case.get("node_id") or "")
        name = str(case.get("name") or node_id or "case")
        requirement = str(case.get("requirement") or "")
        hits = _select(node_id, reported)
        if not hits:
            missing.append(requirement or name)
            continue
        ok = all(tc["pass"] for tc in hits)
        detail = next((tc["detail"] for tc in hits if tc["detail"]), "")
        cases.append(
            {
                "name": name,
                "requirement": requirement,
                "pass": ok,
                "detail": detail or (requirement if not ok else ""),
            }
        )

    for requirement in missing:
        cases.append(
            {
                "name": f"completeness: {requirement}",
                "requirement": requirement,
                "pass": False,
                "detail": "the suite reported no result for this requirement",
            }
        )

    passed = sum(1 for c in cases if c["pass"])
    failed = sum(1 for c in cases if not c["pass"])
    if not reported:
        head = "The suite produced no JUnit report"
        if run.get("timeout"):
            head = "The test run timed out"
        summary = f"{head}. {run.get('output', '')[-400:]}".strip()
    else:
        summary = (
            f"{plan['framework']} in {run['runtime']}: {passed} passed, {failed} failed "
            f"across {len(plan['cases'])} spec requirement(s)."
        )
        if missing:
            summary += f" {len(missing)} requirement(s) had no test."
    return {"summary": summary, "passed": passed, "failed": failed, "cases": cases}


# ------------------------------------------------------------------------ pipeline


def run_smoke(*, spec: str, title: str, url: str, session_id: str) -> dict[str, Any]:
    """Check the running deployment over HTTP.

    The container gets network here, so it must never get the guest's tree with
    it: the run directory holds only the tests we just generated. Guest code is
    not present, and so cannot use the network we opened.
    """
    if not (url or "").strip():
        raise TestgenError("no deployment URL to smoke test")
    if not (spec or "").strip():
        return {"summary": "No spec on this brief.", "passed": 0, "failed": 0, "cases": []}
    plan = generate_suite(spec=spec, title=title, mode="smoke", target_url=url)
    dest = runs_dir() / f"{session_id or 'session'}-smoke"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    write_suite(dest, plan)
    (dest / ".byoi" / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    result = run_suite(dest, plan, network=True, env_extra={"BYOI_TARGET_URL": url})
    report = grade(plan, dest, result)
    report["summary"] = f"Against {url} — {report['summary']}"
    (dest / ".byoi" / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def run(*, spec: str, title: str, source: str, ref: str, session_id: str) -> dict[str, Any]:
    """Generate blind, fetch the submission, run sandboxed, grade deterministically."""
    if not (spec or "").strip():
        return {"summary": "No spec on this brief.", "passed": 0, "failed": 0, "cases": []}
    plan = generate_suite(spec=spec, title=title)
    dest = runs_dir() / (session_id or "session")
    fetch_submission(source=source, ref=ref, dest=dest)
    write_suite(dest, plan)
    (dest / ".byoi" / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    result = run_suite(dest, plan)
    report = grade(plan, dest, result)
    (dest / ".byoi" / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
