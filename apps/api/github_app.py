"""A GitHub App the desk owns, so Solutions sync doesn't depend on a
personal `gh auth login` sitting on whatever box the desk runs on.

Three calls, chained:
  1. Sign a JWT with the App's private key (app-level auth, capped at 10 min
     by GitHub).
  2. Use that JWT to look up which installation, if any, covers a given
     owner/repo.
  3. Exchange the JWT for that installation's own access token (~1 hour),
     which is what actually reads issues — scoped to only the repos the
     operator installed the App on.

The App itself is created once via GitHub's "manifest" flow (see
``github_app_new`` / ``github_app_created`` in main.py): the operator clicks
one button, GitHub hands back a one-time `code`, and ``convert_manifest_code``
trades it for the App's id, slug, and private key — no PEM to download and
paste by hand.
"""

from __future__ import annotations

import calendar
import time
from typing import Any

import httpx
import jwt

from apps.secrets import read_secret, read_secret_full, write_secret_file

API = "https://api.github.com"

# GitHub rejects an App JWT older than 10 minutes; back off a bit for clock
# skew between here and GitHub's servers.
_JWT_TTL_SECONDS = 9 * 60
_TOKEN_REFRESH_SKEW_SECONDS = 120

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GithubAppError(RuntimeError):
    """The App isn't configured, its key is bad, or GitHub rejected a call."""


def configured() -> bool:
    return bool(read_secret("BYOI_GITHUB_APP_ID") and read_secret_full("BYOI_GITHUB_APP_PRIVATE_KEY"))


def slug() -> str | None:
    return read_secret("BYOI_GITHUB_APP_SLUG")


def _app_jwt() -> str:
    app_id = read_secret("BYOI_GITHUB_APP_ID")
    private_key = read_secret_full("BYOI_GITHUB_APP_PRIVATE_KEY")
    if not app_id or not private_key:
        raise GithubAppError("no GitHub App configured — set one up from the desk's Projects tab")
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + _JWT_TTL_SECONDS, "iss": app_id}
    try:
        return jwt.encode(payload, private_key, algorithm="RS256")
    except (ValueError, jwt.InvalidKeyError) as exc:
        raise GithubAppError(f"GitHub App private key is invalid: {exc}") from exc


def _request(method: str, path: str, *, token: str) -> Any:
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    try:
        resp = httpx.request(method, f"{API}{path}", headers=headers, timeout=15)
    except httpx.HTTPError as exc:
        raise GithubAppError(f"GitHub API unreachable: {exc}") from exc
    if resp.status_code == 404:
        return None
    if resp.is_error:
        raise GithubAppError(f"GitHub API {path} failed: {resp.status_code} {resp.text[:200]}")
    return resp.json() if resp.content else None


def installation_id_for(repo_slug: str) -> int | None:
    """The installation covering ``owner/repo``, or None if the App isn't
    installed there yet — the operator still needs to do that from GitHub."""
    data = _request("GET", f"/repos/{repo_slug}/installation", token=_app_jwt())
    return data["id"] if data else None


# installation_id -> (token, expires_at epoch seconds)
_token_cache: dict[int, tuple[str, float]] = {}


def installation_token(installation_id: int) -> str:
    """A short-lived token scoped to one installation. Cached until shortly
    before it expires, so a board read doesn't mint a fresh token every time."""
    cached = _token_cache.get(installation_id)
    if cached and cached[1] - _TOKEN_REFRESH_SKEW_SECONDS > time.time():
        return cached[0]
    body = _request(
        "POST", f"/app/installations/{installation_id}/access_tokens", token=_app_jwt()
    )
    if not body:
        raise GithubAppError(f"installation {installation_id} not found — was it uninstalled?")
    token = body["token"]
    expires_at = calendar.timegm(time.strptime(body["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    _token_cache[installation_id] = (token, expires_at)
    return token


def convert_manifest_code(code: str) -> dict[str, Any]:
    """Finish the App-manifest flow: trade the one-time `code` GitHub just
    redirected back with for the App's id, slug, and private key."""
    try:
        resp = httpx.post(
            f"{API}/app-manifests/{code}/conversions", headers=_HEADERS, timeout=15
        )
    except httpx.HTTPError as exc:
        raise GithubAppError(f"GitHub API unreachable: {exc}") from exc
    if resp.is_error:
        raise GithubAppError(f"manifest conversion failed: {resp.status_code} {resp.text[:200]}")
    return resp.json()


def store_credentials(data: dict[str, Any]) -> None:
    """Persist what ``convert_manifest_code`` returned as managed secrets,
    the same 0600-under-data/secrets/ convention ``salon-secrets.sh`` uses."""
    write_secret_file("BYOI_GITHUB_APP_ID", str(data["id"]))
    write_secret_file("BYOI_GITHUB_APP_SLUG", data["slug"])
    write_secret_file("BYOI_GITHUB_APP_PRIVATE_KEY", data["pem"])
