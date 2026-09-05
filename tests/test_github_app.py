"""A GitHub App the desk owns: JWT signing, installation lookup and token
exchange, the manifest-flow bootstrap, and how Solutions sync picks a token
up once a project is linked."""

from __future__ import annotations

import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from apps.api import github_app
from apps.api.main import create_app
from apps.api.store import Store
from apps.secrets import read_secret


def _desk(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path), headers={"Authorization": "Bearer byoi-host"})


@pytest.fixture()
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


@pytest.fixture()
def configured_app(monkeypatch, keypair):
    private_pem, _ = keypair
    monkeypatch.setenv("BYOI_GITHUB_APP_ID", "9001")
    monkeypatch.setenv("BYOI_GITHUB_APP_PRIVATE_KEY", private_pem)
    monkeypatch.setenv("BYOI_GITHUB_APP_SLUG", "byoi-salon-sync")
    github_app._token_cache.clear()
    yield
    github_app._token_cache.clear()


class _Resp:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self.is_error = status_code >= 400
        self._body = body
        self.content = b"{}" if body is not None else b""
        self.text = text

    def json(self):
        return self._body


# --------------------------------------------------------------------- configured()


def test_not_configured_without_a_key():
    assert not github_app.configured()


def test_configured_once_id_and_key_are_set(configured_app):
    assert github_app.configured()
    assert github_app.slug() == "byoi-salon-sync"


# --------------------------------------------------------------------- _app_jwt


def test_app_jwt_is_signed_with_the_configured_key(configured_app, keypair):
    _, public_pem = keypair
    token = github_app._app_jwt()
    payload = jwt.decode(token, public_pem, algorithms=["RS256"])
    assert payload["iss"] == "9001"
    assert payload["exp"] > payload["iat"]


def test_app_jwt_requires_configuration():
    with pytest.raises(github_app.GithubAppError):
        github_app._app_jwt()


# --------------------------------------------------------------------- installation_id_for


def test_installation_id_for_returns_the_id(configured_app, monkeypatch):
    seen = {}

    def fake_request(method, url, *, headers, timeout):
        seen["method"], seen["url"], seen["auth"] = method, url, headers["Authorization"]
        return _Resp(200, {"id": 555})

    monkeypatch.setattr(github_app.httpx, "request", fake_request)
    assert github_app.installation_id_for("salon/neon") == 555
    assert seen["method"] == "GET"
    assert seen["url"].endswith("/repos/salon/neon/installation")
    assert seen["auth"].startswith("Bearer ")


def test_installation_id_for_returns_none_when_not_installed(configured_app, monkeypatch):
    monkeypatch.setattr(github_app.httpx, "request", lambda *a, **k: _Resp(404))
    assert github_app.installation_id_for("salon/neon") is None


# --------------------------------------------------------------------- installation_token


def test_installation_token_mints_and_caches(configured_app, monkeypatch):
    calls = []

    def fake_request(method, url, *, headers, timeout):
        calls.append(url)
        return _Resp(201, {"token": "ghs_abc123", "expires_at": "2999-01-01T00:00:00Z"})

    monkeypatch.setattr(github_app.httpx, "request", fake_request)
    assert github_app.installation_token(42) == "ghs_abc123"
    assert github_app.installation_token(42) == "ghs_abc123"
    assert len(calls) == 1  # second call served from cache


def test_installation_token_refreshes_when_near_expiry(configured_app, monkeypatch):
    calls = []
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 30))

    def fake_request(method, url, *, headers, timeout):
        calls.append(url)
        return _Resp(201, {"token": "ghs_fresh", "expires_at": soon})

    monkeypatch.setattr(github_app.httpx, "request", fake_request)
    github_app.installation_token(42)
    github_app.installation_token(42)
    assert len(calls) == 2  # within the refresh skew both times — never served stale


def test_installation_token_missing_installation_raises(configured_app, monkeypatch):
    monkeypatch.setattr(github_app.httpx, "request", lambda *a, **k: _Resp(404))
    with pytest.raises(github_app.GithubAppError):
        github_app.installation_token(999)


# --------------------------------------------------------------------- manifest flow


def test_convert_manifest_code_returns_the_credentials(monkeypatch):
    def fake_post(url, *, headers, timeout):
        assert url.endswith("/app-manifests/abc123/conversions")
        return _Resp(200, {"id": 77, "slug": "byoi-sync", "pem": "-----BEGIN...-----"})

    monkeypatch.setattr(github_app.httpx, "post", fake_post)
    data = github_app.convert_manifest_code("abc123")
    assert data == {"id": 77, "slug": "byoi-sync", "pem": "-----BEGIN...-----"}


def test_convert_manifest_code_surfaces_a_failure(monkeypatch):
    monkeypatch.setattr(github_app.httpx, "post", lambda *a, **k: _Resp(404, text="not found"))
    with pytest.raises(github_app.GithubAppError):
        github_app.convert_manifest_code("stale")


def test_store_credentials_persists_as_managed_secrets():
    github_app.store_credentials({"id": 123, "slug": "byoi-sync", "pem": "-----BEGIN RSA...-----"})
    assert read_secret("BYOI_GITHUB_APP_ID") == "123"
    assert github_app.slug() == "byoi-sync"
    assert github_app.configured()


# --------------------------------------------------------------------- sync integration


def test_sync_uses_an_installation_token_once_the_project_is_linked(
    tmp_path: Path, configured_app, monkeypatch
):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="neon", local_path=str(tmp_path), github="https://github.com/salon/neon")
    store.close()

    def fake_request(method, url, *, headers, timeout):
        if url.endswith("/installation"):
            return _Resp(200, {"id": 4242})
        return _Resp(201, {"token": "ghs_linked", "expires_at": "2999-01-01T00:00:00Z"})

    monkeypatch.setattr(github_app.httpx, "request", fake_request)

    seen_tokens = []

    def fake_fetch(slug, *, limit=100, token=None):
        seen_tokens.append(token)
        return []

    monkeypatch.setattr("apps.api.github_issues.fetch_open_issues", fake_fetch)

    desk = _desk(tmp_path)
    res = desk.post(f"/api/projects/{proj['id']}/sync-issues")
    assert res.status_code == 200
    assert seen_tokens == ["ghs_linked"]

    projects = desk.get("/api/projects").json()["projects"]
    linked = next(p for p in projects if p["id"] == proj["id"])
    assert linked["github_installation_id"] == 4242

    # A second sync reuses the stored installation id and the cached token.
    seen_tokens.clear()
    res2 = desk.post(f"/api/projects/{proj['id']}/sync-issues")
    assert res2.status_code == 200
    assert seen_tokens == ["ghs_linked"]


def test_sync_falls_back_to_gh_auth_when_the_app_isnt_installed_on_the_repo(
    tmp_path: Path, configured_app, monkeypatch
):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="neon", local_path=str(tmp_path), github="https://github.com/salon/neon")
    store.close()

    monkeypatch.setattr(github_app.httpx, "request", lambda *a, **k: _Resp(404))

    seen_tokens = []

    def fake_fetch(slug, *, limit=100, token=None):
        seen_tokens.append(token)
        return []

    monkeypatch.setattr("apps.api.github_issues.fetch_open_issues", fake_fetch)

    desk = _desk(tmp_path)
    res = desk.post(f"/api/projects/{proj['id']}/sync-issues")
    assert res.status_code == 200
    assert seen_tokens == [None]


# --------------------------------------------------------------------- API endpoints


def test_github_app_new_returns_a_self_submitting_manifest_form(tmp_path: Path):
    desk = _desk(tmp_path)
    res = desk.get("/api/github/app/new")
    assert res.status_code == 200
    assert "github.com/settings/apps/new" in res.text
    assert "manifest" in res.text


def test_github_app_new_name_fits_githubs_34_char_limit(tmp_path: Path, monkeypatch):
    import html
    import json
    import re

    # A real, unremarkable production domain — long enough that an unbounded
    # "BYOI Solutions sync (<domain>)" template blows past GitHub's cap.
    monkeypatch.setenv("BYOI_PUBLIC_BASE", "https://salon.aipilots.online")
    desk = _desk(tmp_path)
    res = desk.get("/api/github/app/new")
    match = re.search(r'name="manifest" value="([^"]*)"', res.text)
    assert match, "manifest hidden input not found"
    manifest = json.loads(html.unescape(match.group(1)))
    assert len(manifest["name"]) <= 34, manifest["name"]


def test_github_app_created_exchanges_the_code_and_stores_credentials(tmp_path: Path, monkeypatch):
    def fake_post(url, *, headers, timeout):
        assert url.endswith("/app-manifests/xyz/conversions")
        return _Resp(
            200,
            {
                "id": 321,
                "slug": "byoi-sync",
                "pem": "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n",
            },
        )

    monkeypatch.setattr(github_app.httpx, "post", fake_post)
    desk = _desk(tmp_path)
    res = desk.get("/api/github/app/created", params={"code": "xyz"}, follow_redirects=False)
    assert res.status_code == 303
    assert read_secret("BYOI_GITHUB_APP_ID") == "321"


def test_github_app_setup_links_the_installation_to_the_project(tmp_path: Path):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="neon", local_path=str(tmp_path), github="https://github.com/salon/neon")
    store.close()
    desk = _desk(tmp_path)
    res = desk.get(
        "/api/github/app/setup",
        params={"installation_id": 909, "state": proj["id"], "setup_action": "install"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    projects = desk.get("/api/projects").json()["projects"]
    linked = next(p for p in projects if p["id"] == proj["id"])
    assert linked["github_installation_id"] == 909


def test_github_app_install_url_needs_a_configured_app(tmp_path: Path):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="neon", local_path=str(tmp_path), github="https://github.com/salon/neon")
    store.close()
    desk = _desk(tmp_path)
    res = desk.get(f"/api/projects/{proj['id']}/github-app-install-url")
    assert res.status_code == 400


def test_github_app_install_url_once_configured(tmp_path: Path, configured_app):
    store = Store(tmp_path / "salon.db")
    proj = store.add_project(name="neon", local_path=str(tmp_path), github="https://github.com/salon/neon")
    store.close()
    desk = _desk(tmp_path)
    res = desk.get(f"/api/projects/{proj['id']}/github-app-install-url")
    assert res.status_code == 200
    assert (
        res.json()["url"]
        == f"https://github.com/apps/byoi-salon-sync/installations/new?state={proj['id']}"
    )
