"""Host-brokered Vercel deploys of a guest's work.

The guest never holds a credential. The seat pins the tree to a git ref, the
desk fetches that ref, provisions managed infrastructure, and only then runs
`vercel` with its own token — at which point no guest code is executing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

from apps.secrets import read_secret

from . import projects as project_ops
from . import provision as provisioning

ROOT = Path(__file__).resolve().parents[2]
URL_RE = re.compile(r"https://[^\s]+\.vercel\.app")
VERCEL_API = "https://api.vercel.com"


class DeployError(RuntimeError):
    """Precondition or tool failure. Recorded on the deployment, not raised as a 500."""


def vercel_bin() -> str:
    return os.environ.get("BYOI_VERCEL", "vercel")


def deploy_timeout() -> int:
    try:
        return int(os.environ.get("BYOI_DEPLOY_TIMEOUT", "600"))
    except ValueError:
        return 600


def runs_dir() -> Path:
    raw = os.environ.get("BYOI_DEPLOY_RUNS_DIR", "").strip()
    path = Path(raw).expanduser() if raw else ROOT / "data" / "deploy-runs"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _token() -> str:
    token = read_secret("BYOI_VERCEL_TOKEN")
    if not token:
        raise DeployError(
            "no Vercel token on the desk — set BYOI_VERCEL_TOKEN or run "
            "./scripts/salon-secrets.sh vercel (never on the seat)"
        )
    return token


def _clean_env() -> dict[str, str]:
    """Vercel gets its token and nothing else of ours."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "VERCEL_TELEMETRY_DISABLED": "1",
    }


def deploy_argv(dest: Path, env: dict[str, str], *, token: str, production: bool = False) -> list[str]:
    argv = [vercel_bin(), "deploy", "--yes", "--token", token, "--cwd", str(dest)]
    scope = read_secret("BYOI_VERCEL_SCOPE")
    if scope:
        argv += ["--scope", scope]
    argv.append("--prod" if production else "--target=preview")
    for key, value in sorted(env.items()):
        # Present at build time (migrations) and at runtime (the app itself).
        argv += ["--build-env", f"{key}={value}", "--env", f"{key}={value}"]
    return argv


def _run(argv: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_clean_env(),
        )
    except FileNotFoundError as exc:
        raise DeployError(f"{argv[0]} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeployError("the deploy timed out") from exc


def _redact(text: str, token: str) -> str:
    return (text or "").replace(token, "***") if token else (text or "")


def project_info(dest: Path) -> dict[str, str]:
    """`vercel deploy` drops the ids it used into .vercel/project.json."""
    path = dest / ".vercel" / "project.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "projectId": str(data.get("projectId") or ""),
        "orgId": str(data.get("orgId") or ""),
    }


def write_project_link(dest: Path, *, project_id: str, org_id: str) -> None:
    """Pre-seed .vercel/project.json so this deploy lands on an already-known
    project instead of `vercel deploy` minting a new one by directory name."""
    vercel_dir = dest / ".vercel"
    vercel_dir.mkdir(parents=True, exist_ok=True)
    (vercel_dir / "project.json").write_text(
        json.dumps({"projectId": project_id, "orgId": org_id}), encoding="utf-8"
    )


def _api(method: str, path: str, *, token: str, json_body: Any = None) -> httpx.Response:
    return httpx.request(
        method,
        f"{VERCEL_API}{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=json_body,
        timeout=60.0,
    )


def make_public(project_id: str, *, token: str) -> str | None:
    """Turn off Vercel's deployment protection for this project.

    Previews are SSO-gated by default, which would make the URL unopenable by
    the guest who built it and unreachable by the smoke suite. Returns a note
    when it could not be turned off.
    """
    if not project_id:
        return "no project id, left with Vercel's default protection"
    try:
        res = _api(
            "PATCH",
            f"/v9/projects/{project_id}",
            token=token,
            json_body={"ssoProtection": None, "passwordProtection": None},
        )
    except httpx.HTTPError as exc:
        return f"could not disable deployment protection: {exc}"
    if res.status_code >= 400:
        return f"could not disable deployment protection: {res.status_code} {res.text[:160]}"
    return None


def fetch(*, source: str, ref: str, dest: Path) -> None:
    """Same checkout mechanism the acceptance suite uses."""
    from .testgen import TestgenError, fetch_submission

    try:
        fetch_submission(source=source, ref=ref, dest=dest)
    except TestgenError as exc:
        raise DeployError(str(exc)) from exc


def run(
    *,
    session_id: str,
    source: str,
    ref: str,
    production: bool = False,
    vercel_project_id: str | None = None,
    vercel_org_id: str | None = None,
) -> dict[str, Any]:
    """Fetch the pinned ref, provision, deploy. Returns url/resources/notes.

    When `vercel_project_id`/`vercel_org_id` are given (a desk project that has
    already deployed once), the new build is linked onto that same Vercel
    project rather than minting a new one — so every guest who works this
    desk project, across however many solutions, ships to one place.
    """
    if not shutil.which(vercel_bin()):
        raise DeployError(f"{vercel_bin()} is not on PATH on the desk")
    token = _token()

    dest = runs_dir() / (session_id or "session")
    fetch(source=source, ref=ref, dest=dest)

    detected = project_ops.detect(dest)
    if not detected.get("deployable", True):
        raise DeployError(
            f"this project does not look deployable to Vercel (framework: {detected.get('framework')})"
        )

    if vercel_project_id and vercel_org_id:
        write_project_link(dest, project_id=vercel_project_id, org_id=vercel_org_id)

    resources, notes = provisioning.provision(
        session_id=session_id, needs=list(detected.get("needs") or [])
    )
    env = provisioning.env_from(resources)

    proc = _run(
        deploy_argv(dest, env, token=token, production=production),
        timeout=deploy_timeout(),
        cwd=dest,
    )
    stdout = _redact(proc.stdout or "", token)
    stderr = _redact(proc.stderr or "", token)
    if proc.returncode != 0:
        # Don't strand what we just created if the deploy itself failed.
        problems = provisioning.destroy(resources)
        detail = (stderr or stdout or f"vercel exited {proc.returncode}").strip()[-800:]
        if problems:
            detail += " | cleanup: " + "; ".join(problems)
        raise DeployError(detail)

    match = URL_RE.search(stdout) or URL_RE.search(stderr)
    if not match:
        provisioning.destroy(resources)
        raise DeployError("vercel did not print a deployment URL")

    ids = project_info(dest)
    if ids.get("projectId") and os.environ.get("BYOI_VERCEL_PUBLIC", "1") != "0":
        problem = make_public(ids["projectId"], token=token)
        if problem:
            notes.append(problem)

    return {
        "url": match.group(0),
        "resources": resources,
        "notes": notes,
        "framework": detected.get("framework"),
        "detail": "; ".join(notes) if notes else None,
        # The Vercel project this build landed on — the desk project's row
        # remembers it (first-write-wins) so the next deploy reuses it.
        "vercel_project_id": ids.get("projectId") or None,
        "vercel_org_id": ids.get("orgId") or None,
    }


def teardown(deployment: dict[str, Any]) -> dict[str, Any]:
    """Remove this build's preview deployment and destroy its managed
    infrastructure. Best effort.

    The Vercel project itself is not touched here — it belongs to the desk
    project, not this session, and outlives whichever guest happened to
    trigger this deploy.
    """
    problems: list[str] = []
    url = deployment.get("url")
    if url:
        try:
            token = _token()
            proc = _run(
                [vercel_bin(), "remove", url, "--yes", "--token", token],
                timeout=min(deploy_timeout(), 180),
            )
            if proc.returncode != 0:
                problems.append(
                    _redact((proc.stderr or proc.stdout or "vercel remove failed"), token).strip()[-300:]
                )
        except DeployError as exc:
            problems.append(str(exc))
    resources = deployment.get("resources") or []
    problems.extend(provisioning.destroy(resources))
    return {"ok": not problems, "problems": problems}
