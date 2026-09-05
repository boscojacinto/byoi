"""The media a brief needs as an input, and how it reaches a seat.

A brief can require files to work from — the photos a landing page is built
from, a logo to match. They live in one Utho bucket (``object_store``) and are
recorded per project in ``project_media``.

Getting them to the guest is the interesting half. A seat's Bash allowlist has
no ``curl`` and no ``wget``, and anything off that allowlist is refused by
Claude Code's own classifier before any approval request is emitted — so a
download URL inside a seat is close to useless. Instead the desk materialises
the files at claim time into a directory beside the guest's clone, which the
seat's Claude reaches as ordinary local paths via ``--add-dir``.

Beside the clone, never inside it: ``apps/seat/submission.py`` runs ``git add
-A`` at the repo toplevel, so media placed in the working tree would be swept
into the guest's submission ref and pushed to origin on Solutions sync.

Downloads go through a content-addressed cache keyed by sha256, so the second
guest to claim a brief on the same project pays nothing, and a truncated
download is caught before a seat ever sees it.

Every function here degrades to a no-op when no storage is configured: a brief
with no media is the normal case, and a media failure must never cost a guest
their visit.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

from . import object_store
from .object_store import ObjectStoreError

ROOT = Path(__file__).resolve().parents[2]

MANIFEST = "MANIFEST.md"


def cache_dir() -> Path:
    raw = os.environ.get("BYOI_MEDIA_CACHE", "").strip()
    base = Path(raw).expanduser() if raw else Path(os.environ.get("BYOI_DATA", ROOT / "data")) / "media-cache"
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def configured() -> bool:
    return object_store.configured()


# --- the operator's side ------------------------------------------------------


def add(
    store: Any,
    *,
    project_id: str,
    filename: str,
    data: bytes,
    content_type: str = "",
    role: str = "",
    board_id: str | None = None,
) -> dict[str, Any]:
    """Put one file in the bucket and record it against the project.

    Uses the **write** key, which only the operator's routes ever reach.
    """
    client = object_store.writer()
    if client is None:
        raise ObjectStoreError(
            "object storage is not configured — run `deploy-utho/utho.py storage-init` "
            "and store the keys with `scripts/salon-secrets.sh utho-storage-write`"
        )
    if not data:
        raise ObjectStoreError(f"{filename} is empty")

    checksum = digest(data)
    media_id = checksum[:8]
    key = object_store.object_key(project_id, media_id, filename)
    client.put_object(key, data, content_type)

    # Seed the cache from the bytes we already hold, so the first claim after
    # an upload does not go back out to the network for them.
    _cache_write(checksum, data)

    return store.add_media(
        project_id=project_id,
        board_id=board_id,
        object_key=key,
        filename=filename,
        content_type=content_type or "application/octet-stream",
        size=len(data),
        checksum=checksum,
        role=role,
    )


def remove(store: Any, media_id: str) -> dict[str, Any] | None:
    """Drop one file from the bucket and the project. Operator-only."""
    item = store.media(media_id)
    if not item:
        return None
    client = object_store.writer()
    if client is not None:
        try:
            client.delete_object(item["object_key"])
        except ObjectStoreError:
            # The row is what the salon reads; a bucket object left behind is
            # tidied by the lifecycle rule rather than blocking the operator.
            pass
    return store.remove_media(media_id)


# --- the claim path -----------------------------------------------------------


def for_board(store: Any, board_id: str) -> list[dict[str, Any]]:
    return store.media_for_board(board_id)


def _cache_path(checksum: str) -> Path:
    return cache_dir() / checksum


def _cache_write(checksum: str, data: bytes) -> None:
    path = _cache_path(checksum)
    tmp = path.with_suffix(".part")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)


def _fetch(item: dict[str, Any]) -> bytes | None:
    """Cache, then the bucket. Returns None when the bytes cannot be trusted."""
    checksum = item.get("checksum") or ""
    if checksum:
        cached = _cache_path(checksum)
        if cached.is_file():
            data = cached.read_bytes()
            if digest(data) == checksum:
                return data
            cached.unlink(missing_ok=True)  # corrupt on disk; go back to the bucket

    client = object_store.reader()
    if client is None:
        return None
    data = client.get_object(item["object_key"])
    if checksum and digest(data) != checksum:
        raise ObjectStoreError(
            f"{item['filename']} did not match its checksum — refusing to hand it to a seat"
        )
    if checksum:
        _cache_write(checksum, data)
    return data


def manifest_text(items: list[dict[str, Any]]) -> str:
    """What each file is for, so the seat's Claude does not have to guess."""
    lines = [
        "# Media for this brief",
        "",
        "These files are inputs to the work. They are read-only reference material",
        "and live outside the project tree, so they are not part of what you ship.",
        "",
    ]
    for item in items:
        role = item.get("role") or ""
        size = item.get("size") or 0
        lines.append(f"- `{item['filename']}` — {role or 'no description given'} ({size:,} bytes)")
    lines.append("")
    return "\n".join(lines)


def materialize(store: Any, board_id: str, dest: Path) -> list[dict[str, Any]]:
    """Write this brief's media into ``dest``. Returns what actually landed.

    Never raises: a brief whose media cannot be fetched is still a brief the
    guest can work on, and the desk logs rather than failing the claim.
    """
    items = for_board(store, board_id)
    if not items or not configured():
        return []

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    landed: list[dict[str, Any]] = []
    for item in items:
        try:
            data = _fetch(item)
        except ObjectStoreError:
            continue
        if data is None:
            continue
        target = dest / Path(item["filename"]).name
        try:
            target.write_bytes(data)
        except OSError:
            continue
        landed.append(item)

    if landed:
        try:
            (dest / MANIFEST).write_text(manifest_text(landed), encoding="utf-8")
        except OSError:
            pass
    elif not any(dest.iterdir()):
        shutil.rmtree(dest, ignore_errors=True)
    return landed
