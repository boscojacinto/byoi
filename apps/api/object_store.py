"""Utho Object Storage, reduced to what a project's media needs from a bucket.

One bucket holds every project's media, keyed ``projects/<project_id>/...``.
Utho grants a key ``read``/``write``/``full``/``none`` on a *whole bucket* —
there is no prefix scoping, no policy document, no per-object ACL — so a
per-project key would mean a per-project bucket, and a bucket is a billed,
globally-named allocation. We split by **role** instead of by project:

    reader()  read-only key   the claim path, and share_url()
    writer()  read+write key  the operator's upload and delete routes

The split is structural, not a convention. ``put_object`` and ``delete_object``
exist only on the writer, and nothing here is exposed at module level, so the
guest-facing claim path has no way to name the write credential even by
mistake. Both keys are desk-only: they are registered in ``apps.secrets`` so
``scrub()`` strips them from any environment handed to a process running guest
code.

Every provider here is optional, the same contract as ``provision.py``: with no
keys configured ``reader()``/``writer()`` return None and a brief simply runs
without media.

Endpoints are the published v2 API. As in the deploy skill's ``utho.py``, Utho
answers an error with HTTP 200 and ``{"status": "error"}``, so every call checks
the body rather than the status code.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from apps.secrets import read_secret

API = "https://api.utho.com/v2"
TIMEOUT = 120.0

# Utho's own duration vocabulary for a sharable link.
SHARE_EXPIRY = ("30s", "15m", "1h", "7d", "1M", "1y")


class ObjectStoreError(RuntimeError):
    """Reported to the desk and shown on the project, never raised as a 500."""


def bucket() -> str | None:
    return read_secret("BYOI_UTHO_BUCKET")


def datacenter() -> str | None:
    return read_secret("BYOI_UTHO_DC")


def object_key(project_id: str, media_id: str, filename: str) -> str:
    """Where a project's media lives in the shared bucket.

    The prefix is organisational only — it is not a security boundary, because
    a key's grant covers the whole bucket. What keeps one project's media away
    from another guest is that no seat ever holds a key at all.
    """
    safe = "".join(c for c in filename if c.isalnum() or c in "._-") or "file"
    return f"projects/{project_id}/{media_id}-{safe}"


class _Reader:
    """Read-only half of the bucket. Constructed with the read key."""

    role = "read"

    def __init__(self, access_key: str, secret_key: str, dc: str, name: str) -> None:
        self._access_key = access_key
        self._secret_key = secret_key
        self.dc = dc
        self.bucket = name

    # --- transport ------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "X-Access-Key": self._access_key,
            "X-Secret-Key": self._secret_key,
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kw) -> dict:
        url = f"{API}/objectstorage/{self.dc}/bucket/{self.bucket}/{path}"
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                res = client.request(method, url, headers=self._headers(), **kw)
        except httpx.HTTPError as exc:
            raise ObjectStoreError(f"utho storage: {exc}") from exc
        if res.status_code >= 400:
            raise ObjectStoreError(f"utho storage: {res.status_code} {res.text[:200]}")
        try:
            parsed = res.json()
        except ValueError:
            raise ObjectStoreError(f"unreadable response from {path}: {res.text[:200]}") from None
        if isinstance(parsed, dict) and parsed.get("status") not in (None, "success"):
            raise ObjectStoreError(parsed.get("message") or "utho storage rejected the call")
        return parsed if isinstance(parsed, dict) else {}

    # --- reads ----------------------------------------------------------

    def share_url(self, key: str, expire: str = "1h") -> str:
        """A time-limited link to one object.

        Deliberately kept off the claim path — a seat has no ``curl`` on its
        Bash allowlist, so a URL is close to useless inside one. This is for
        the desk UI's thumbnails and for a deployed app that must fetch the
        file at runtime.
        """
        if expire not in SHARE_EXPIRY:
            raise ObjectStoreError(f"expire must be one of {', '.join(SHARE_EXPIRY)}")
        got = self._request(
            "GET", f"download?path={quote(key)}&expire={expire}"
        )
        url = got.get("url") or got.get("download_url") or got.get("data")
        if not isinstance(url, str) or not url:
            raise ObjectStoreError(f"utho did not return a link for {key}")
        return url

    def get_object(self, key: str) -> bytes:
        """Fetch one object's bytes, via the time-limited link Utho mints."""
        url = self.share_url(key, "15m")
        try:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
                res = client.get(url)
        except httpx.HTTPError as exc:
            raise ObjectStoreError(f"downloading {key}: {exc}") from exc
        if res.status_code >= 400:
            raise ObjectStoreError(f"downloading {key}: {res.status_code}")
        return res.content


class _Writer(_Reader):
    """Read-write half. Only the operator's routes ever construct this."""

    role = "full"

    def put_object(self, key: str, data: bytes, content_type: str) -> None:
        directory, _, filename = key.rpartition("/")
        files = {"file": (filename, data, content_type or "application/octet-stream")}
        # The `path` field carries the directory only; the filename rides with
        # the part itself. A root-level object sends no path at all.
        form = {"path": directory} if directory else None
        self._request("POST", "upload/", files=files, data=form)

    def delete_object(self, key: str) -> None:
        # The SDK's delete-file helper appends a stray trailing slash to the
        # query value; its delete-directory helper does not. Follow the latter.
        self._request("DELETE", f"delete/object?path={quote(key)}")


def _build(kind: type[_Reader], key_name: str, secret_name: str) -> _Reader | None:
    access_key = read_secret(key_name)
    secret_key = read_secret(secret_name)
    dc = datacenter()
    name = bucket()
    if not (access_key and secret_key and dc and name):
        return None
    return kind(access_key, secret_key, dc, name)


def reader() -> _Reader | None:
    """The read-only client, or None when storage is not configured."""
    return _build(_Reader, "BYOI_UTHO_S3_READ_KEY", "BYOI_UTHO_S3_READ_SECRET")


def writer() -> _Writer | None:
    """The read-write client, or None when storage is not configured."""
    got = _build(_Writer, "BYOI_UTHO_S3_WRITE_KEY", "BYOI_UTHO_S3_WRITE_SECRET")
    return got  # type: ignore[return-value]


def configured() -> bool:
    """Whether the desk can read media at all — the claim path's question."""
    return reader() is not None
