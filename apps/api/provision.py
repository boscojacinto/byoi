"""Managed Postgres and Redis for a deploy, created and destroyed per session.

Only the desk ever holds these tokens. The seat never sees them, and neither
does the guest's Claude — provisioning happens after the tree has been pinned
to a git ref and fetched, so guest code is never running when a token is in
the environment.

Providers are pluggable and every one of them is optional: with no token set,
`provision()` returns no resources and the deploy simply ships without a
managed data layer, which is the right behaviour for a static brief.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

import httpx

from apps.secrets import read_secret

NEON_API = "https://console.neon.tech/api/v2"
UPSTASH_API = "https://api.upstash.com/v2"
TIMEOUT = 60.0


class ProvisionError(RuntimeError):
    """Reported to the desk and shown on the deployment, never raised as a 500."""


def _token(name: str) -> str | None:
    """Env var or data/secrets file — see apps/secrets.py."""
    return read_secret(name)


def resource_name(session_id: str) -> str:
    sid = "".join(c for c in (session_id or "").lower() if c.isalnum())[:12] or "seat"
    return f"byoi-{sid}"


# ----------------------------------------------------------------------- postgres


def provision_postgres(session_id: str) -> dict[str, Any] | None:
    """A Neon project per session. Returns None when no token is configured."""
    token = _token("BYOI_NEON_API_KEY")
    if not token:
        return None
    name = resource_name(session_id)
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            res = client.post(
                f"{NEON_API}/projects",
                headers={"Authorization": f"Bearer {token}"},
                json={"project": {"name": name, "pg_version": 16}},
            )
            if res.status_code >= 400:
                raise ProvisionError(f"neon: {res.status_code} {res.text[:200]}")
            data = res.json()
    except httpx.HTTPError as exc:
        raise ProvisionError(f"neon: {exc}") from exc

    project = data.get("project") or {}
    uris = data.get("connection_uris") or []
    uri = (uris[0] or {}).get("connection_uri") if uris else None
    if not uri:
        raise ProvisionError("neon did not return a connection string")
    return {
        "kind": "postgres",
        "provider": "neon",
        "id": project.get("id"),
        "name": name,
        "env": {"DATABASE_URL": uri, "POSTGRES_URL": uri},
    }


def destroy_postgres(resource: dict[str, Any]) -> None:
    token = _token("BYOI_NEON_API_KEY")
    project_id = resource.get("id")
    if not token or not project_id:
        return
    with httpx.Client(timeout=TIMEOUT) as client:
        res = client.delete(
            f"{NEON_API}/projects/{project_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if res.status_code >= 400 and res.status_code != 404:
            raise ProvisionError(f"neon delete: {res.status_code} {res.text[:200]}")


# -------------------------------------------------------------------------- redis


def provision_redis(session_id: str) -> dict[str, Any] | None:
    """An Upstash Redis database per session. Returns None without a token."""
    email = _token("BYOI_UPSTASH_EMAIL")
    key = _token("BYOI_UPSTASH_API_KEY")
    if not (email and key):
        return None
    name = resource_name(session_id)
    region = os.environ.get("BYOI_UPSTASH_REGION", "us-east-1").strip() or "us-east-1"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            res = client.post(
                f"{UPSTASH_API}/redis/database",
                auth=(email, key),
                json={"name": name, "region": region, "tls": True},
            )
            if res.status_code >= 400:
                raise ProvisionError(f"upstash: {res.status_code} {res.text[:200]}")
            data = res.json()
    except httpx.HTTPError as exc:
        raise ProvisionError(f"upstash: {exc}") from exc

    endpoint = data.get("endpoint")
    password = data.get("password")
    port = data.get("port") or 6379
    if not (endpoint and password):
        raise ProvisionError("upstash did not return connection details")
    url = f"rediss://default:{password}@{endpoint}:{port}"
    return {
        "kind": "redis",
        "provider": "upstash",
        "id": data.get("database_id"),
        "name": name,
        "env": {"REDIS_URL": url, "KV_URL": url},
    }


def destroy_redis(resource: dict[str, Any]) -> None:
    email = _token("BYOI_UPSTASH_EMAIL")
    key = _token("BYOI_UPSTASH_API_KEY")
    database_id = resource.get("id")
    if not (email and key and database_id):
        return
    with httpx.Client(timeout=TIMEOUT) as client:
        res = client.delete(f"{UPSTASH_API}/redis/database/{database_id}", auth=(email, key))
        if res.status_code >= 400 and res.status_code != 404:
            raise ProvisionError(f"upstash delete: {res.status_code} {res.text[:200]}")


# --------------------------------------------------------------------------- auth


def provision_auth(session_id: str) -> dict[str, Any]:
    """No vendor call: auth just needs a per-deploy secret nobody else holds."""
    return {
        "kind": "auth",
        "provider": "local",
        "id": None,
        "name": resource_name(session_id),
        "env": {"AUTH_SECRET": secrets.token_urlsafe(32), "AUTH_TRUST_HOST": "true"},
    }


PROVISIONERS = {
    "postgres": provision_postgres,
    "redis": provision_redis,
    "auth": provision_auth,
}
DESTROYERS = {"postgres": destroy_postgres, "redis": destroy_redis}


def provision(*, session_id: str, needs: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Create what the project needs. Returns (resources, notes about anything skipped)."""
    resources: list[dict[str, Any]] = []
    notes: list[str] = []
    for kind in needs or []:
        maker = PROVISIONERS.get(kind)
        if maker is None:
            notes.append(f"no provisioner for {kind}")
            continue
        try:
            resource = maker(session_id)
        except ProvisionError as exc:
            notes.append(str(exc))
            continue
        if resource is None:
            notes.append(f"{kind}: no credentials configured on the desk, skipped")
            continue
        resources.append(resource)
    return resources, notes


def destroy(resources: list[dict[str, Any]]) -> list[str]:
    """Best effort: one failure must not strand the rest."""
    problems: list[str] = []
    for resource in resources or []:
        killer = DESTROYERS.get(str(resource.get("kind")))
        if killer is None:
            continue
        try:
            killer(resource)
        except Exception as exc:
            problems.append(f"{resource.get('kind')}: {exc}")
    return problems


def env_from(resources: list[dict[str, Any]]) -> dict[str, str]:
    env: dict[str, str] = {}
    for resource in resources or []:
        env.update(resource.get("env") or {})
    return env
