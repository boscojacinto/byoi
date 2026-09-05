"""Media a brief needs as an input: the bucket client, and how it reaches a seat.

The security-shaped assertions here are the ones worth keeping: that the
read-only client cannot be used to write, that a file whose bytes do not match
their checksum never reaches a seat, and that the bucket credentials are
registered desk-only so `scrub()` strips them from a seat's environment.
"""

from pathlib import Path

import pytest

from apps.api import media as media_ops
from apps.api import object_store
from apps.api.object_store import ObjectStoreError
from apps.api.store import Store


class FakeBucket:
    """Stands in for Utho. Records what was asked of it."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.downloads: list[str] = []

    # the reader half
    def get_object(self, key: str) -> bytes:
        self.downloads.append(key)
        if key not in self.objects:
            raise ObjectStoreError(f"no such object: {key}")
        return self.objects[key]

    def share_url(self, key: str, expire: str = "1h") -> str:
        return f"https://example.invalid/{key}?e={expire}"

    # the writer half
    def put_object(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)


@pytest.fixture
def bucket(monkeypatch, tmp_path):
    fake = FakeBucket()
    monkeypatch.setenv("BYOI_MEDIA_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(object_store, "reader", lambda: fake)
    monkeypatch.setattr(object_store, "writer", lambda: fake)
    monkeypatch.setattr(object_store, "configured", lambda: True)
    return fake


def _project_with_brief(store: Store, tmp_path: Path):
    proj = store.add_project(name="cafe", local_path=str(tmp_path))
    item = store.add_board("Landing page", "build it", 60, 40, project_id=proj["id"])
    return proj, item


# --- the role split -----------------------------------------------------------


def test_read_client_cannot_write():
    """The claim path holds a reader, and a reader has no way to mutate.

    This is the whole reason there are two keys: Utho grants permission on a
    whole bucket, so the only place the read/write boundary can be enforced is
    in who holds which credential.
    """
    read_only = object_store._Reader("ak", "sk", "innoida", "bucket")
    assert not hasattr(read_only, "put_object")
    assert not hasattr(read_only, "delete_object")
    assert hasattr(read_only, "get_object")

    read_write = object_store._Writer("ak", "sk", "innoida", "bucket")
    assert hasattr(read_write, "put_object")
    assert isinstance(read_write, object_store._Reader)


def test_reader_and_writer_use_different_credentials(monkeypatch):
    monkeypatch.setenv("BYOI_UTHO_DC", "innoida")
    monkeypatch.setenv("BYOI_UTHO_BUCKET", "byoi-media")
    monkeypatch.setenv("BYOI_UTHO_S3_READ_KEY", "read-ak")
    monkeypatch.setenv("BYOI_UTHO_S3_READ_SECRET", "read-sk")
    monkeypatch.setenv("BYOI_UTHO_S3_WRITE_KEY", "write-ak")
    monkeypatch.setenv("BYOI_UTHO_S3_WRITE_SECRET", "write-sk")

    assert object_store.reader()._headers()["X-Access-Key"] == "read-ak"
    assert object_store.writer()._headers()["X-Access-Key"] == "write-ak"


def test_bucket_credentials_are_desk_only():
    """Being in SECRETS is what keeps these out of a seat that runs guest code."""
    from apps.secrets import desk_only_names, scrub

    names = desk_only_names()
    for name in (
        "BYOI_UTHO_S3_READ_KEY",
        "BYOI_UTHO_S3_READ_SECRET",
        "BYOI_UTHO_S3_WRITE_KEY",
        "BYOI_UTHO_S3_WRITE_SECRET",
    ):
        assert name in names

    handed_on = scrub({"BYOI_UTHO_S3_WRITE_SECRET": "sk", "PATH": "/usr/bin"})
    assert "BYOI_UTHO_S3_WRITE_SECRET" not in handed_on
    assert handed_on["PATH"] == "/usr/bin"


# --- upload and materialize ---------------------------------------------------


def test_add_then_materialize_round_trips(tmp_path, bucket):
    store = Store(tmp_path / "salon.db")
    proj, item = _project_with_brief(store, tmp_path)

    row = media_ops.add(
        store,
        project_id=proj["id"],
        filename="hero.jpg",
        data=b"\xff\xd8pretend-jpeg",
        content_type="image/jpeg",
        role="hero shot",
    )
    assert row["object_key"].startswith(f"projects/{proj['id']}/")
    assert row["size"] == len(b"\xff\xd8pretend-jpeg")

    dest = tmp_path / "seat-media"
    landed = media_ops.materialize(store, item["id"], dest)
    assert [x["filename"] for x in landed] == ["hero.jpg"]
    assert (dest / "hero.jpg").read_bytes() == b"\xff\xd8pretend-jpeg"

    manifest = (dest / media_ops.MANIFEST).read_text()
    assert "hero.jpg" in manifest and "hero shot" in manifest


def test_upload_seeds_the_cache_so_the_first_claim_needs_no_download(tmp_path, bucket):
    store = Store(tmp_path / "salon.db")
    proj, item = _project_with_brief(store, tmp_path)
    media_ops.add(store, project_id=proj["id"], filename="logo.png", data=b"png-bytes")

    media_ops.materialize(store, item["id"], tmp_path / "one")
    assert bucket.downloads == []  # served from the cache the upload wrote

    media_ops.materialize(store, item["id"], tmp_path / "two")
    assert bucket.downloads == []


def test_cache_miss_falls_back_to_the_bucket(tmp_path, bucket):
    store = Store(tmp_path / "salon.db")
    proj, item = _project_with_brief(store, tmp_path)
    row = media_ops.add(store, project_id=proj["id"], filename="logo.png", data=b"png-bytes")

    media_ops._cache_path(row["checksum"]).unlink()
    landed = media_ops.materialize(store, item["id"], tmp_path / "seat")
    assert bucket.downloads == [row["object_key"]]
    assert len(landed) == 1


def test_corrupt_bytes_never_reach_a_seat(tmp_path, bucket):
    store = Store(tmp_path / "salon.db")
    proj, item = _project_with_brief(store, tmp_path)
    row = media_ops.add(store, project_id=proj["id"], filename="logo.png", data=b"png-bytes")

    # Lose the cached copy and corrupt what the bucket hands back.
    media_ops._cache_path(row["checksum"]).unlink()
    bucket.objects[row["object_key"]] = b"truncated"

    dest = tmp_path / "seat"
    landed = media_ops.materialize(store, item["id"], dest)
    assert landed == []
    assert not (dest / "logo.png").exists()


def test_a_corrupt_cache_entry_is_refetched(tmp_path, bucket):
    store = Store(tmp_path / "salon.db")
    proj, item = _project_with_brief(store, tmp_path)
    row = media_ops.add(store, project_id=proj["id"], filename="logo.png", data=b"png-bytes")

    media_ops._cache_path(row["checksum"]).write_bytes(b"rot")
    landed = media_ops.materialize(store, item["id"], tmp_path / "seat")
    assert bucket.downloads == [row["object_key"]]
    assert len(landed) == 1


# --- scoping ------------------------------------------------------------------


def test_media_is_shared_across_briefs_unless_narrowed(tmp_path, bucket):
    store = Store(tmp_path / "salon.db")
    proj, first = _project_with_brief(store, tmp_path)
    second = store.add_board("Second brief", "also build", 60, 40, project_id=proj["id"])

    shared = media_ops.add(store, project_id=proj["id"], filename="brand.png", data=b"a")
    only_first = media_ops.add(
        store, project_id=proj["id"], filename="mock.png", data=b"b", board_id=first["id"]
    )

    assert {m["id"] for m in store.media_for_board(first["id"])} == {shared["id"], only_first["id"]}
    assert {m["id"] for m in store.media_for_board(second["id"])} == {shared["id"]}


def test_removing_media_drops_the_object_and_the_row(tmp_path, bucket):
    store = Store(tmp_path / "salon.db")
    proj, item = _project_with_brief(store, tmp_path)
    row = media_ops.add(store, project_id=proj["id"], filename="gone.png", data=b"x")

    assert media_ops.remove(store, row["id"])["id"] == row["id"]
    assert store.media(row["id"]) is None
    assert row["object_key"] not in bucket.objects


# --- the unconfigured salon ---------------------------------------------------


def test_without_storage_a_brief_still_claims(tmp_path, monkeypatch):
    """No keys set is the normal case for a salon with no media at all."""
    monkeypatch.setattr(object_store, "reader", lambda: None)
    monkeypatch.setattr(object_store, "writer", lambda: None)
    store = Store(tmp_path / "salon.db")
    _proj, item = _project_with_brief(store, tmp_path)

    assert media_ops.materialize(store, item["id"], tmp_path / "seat") == []


def test_media_never_lands_in_the_guests_submission(tmp_path):
    """The reason media sits beside the clone instead of inside it.

    `submission.capture()` runs `git add -A` at the repo toplevel, so a media
    folder in the working tree would be committed to the submission ref and
    pushed to origin on Solutions sync.
    """
    import subprocess

    from apps.api import seats
    from apps.seat import submission

    workspace = tmp_path / "runtime" / "workspace"
    clone = workspace / "cafe-app"
    clone.mkdir(parents=True)
    (clone / "index.html").write_text("<h1>hi</h1>")
    for args in (
        ("init", "-q"),
        ("config", "user.email", "seat@byoi.test"),
        ("config", "user.name", "seat"),
        ("add", "-A"),
        ("commit", "-qm", "first"),
    ):
        subprocess.run(["git", *args], cwd=clone, check=True, capture_output=True)

    # Where the desk puts it: a sibling of the clone, inside the workspace.
    media = workspace / "media"
    media.mkdir()
    (media / "hero.jpg").write_bytes(b"\xff\xd8jpeg")

    got = submission.capture(cwd=clone, session_id="sess-1")
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", got["commit"]],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert "index.html" in listed
    assert not any("hero.jpg" in name or name.startswith("media/") for name in listed)

    # And the path the desk actually uses is that same sibling, not a subdir
    # of the project clone.
    assert seats.media_dir("sess-1").name == "media"
    assert seats.media_dir("sess-1").parent.name == "workspace"


def test_claiming_a_brief_puts_its_media_on_the_seat(tmp_path, bucket, monkeypatch):
    """The whole path, desk-side: upload, claim, and the seat is told where."""
    from fastapi.testclient import TestClient

    from apps.api.main import create_app

    desk = TestClient(create_app(tmp_path), headers={"Authorization": "Bearer byoi-host"})
    folder = tmp_path / "work"
    folder.mkdir()
    proj = desk.post(
        "/api/projects", json={"kind": "local", "path": str(folder), "name": "work"}
    ).json()
    brief = desk.post(
        "/api/board",
        json={
            "title": "Build it",
            "brief": "from these",
            "spec": "- uses the supplied photos",
            "project_id": proj["id"],
        },
    ).json()

    up = desk.post(
        f"/api/projects/{proj['id']}/media?filename=hero.jpg&role=hero%20shot",
        content=b"\xff\xd8jpeg-bytes",
        headers={"Content-Type": "image/jpeg"},
    )
    assert up.status_code == 200

    listed = desk.get(f"/api/projects/{proj['id']}/media").json()
    assert [m["filename"] for m in listed["media"]] == ["hero.jpg"]

    seen: list = []
    monkeypatch.setattr(
        "apps.api.seat_sync.set_workspace",
        lambda seat, path, media=None: seen.append((path, media)) or {"ok": True},
    )
    sid = desk.post(
        "/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}
    ).json()["session"]["id"]
    claimed = desk.post(f"/api/sessions/{sid}/claim", json={"board_id": brief["id"]})
    assert claimed.status_code == 200

    (_workspace, media_path) = seen[0]
    assert media_path, "the seat was not told where its media is"
    landed = Path(media_path)
    assert (landed / "hero.jpg").read_bytes() == b"\xff\xd8jpeg-bytes"
    assert (landed / media_ops.MANIFEST).exists()
    # Beside the clone, not inside the project folder the guest edits.
    assert not (folder / "hero.jpg").exists()


def test_claiming_a_brief_with_no_media_tells_the_seat_nothing(tmp_path, bucket, monkeypatch):
    from fastapi.testclient import TestClient

    from apps.api.main import create_app

    desk = TestClient(create_app(tmp_path), headers={"Authorization": "Bearer byoi-host"})
    folder = tmp_path / "work"
    folder.mkdir()
    proj = desk.post(
        "/api/projects", json={"kind": "local", "path": str(folder), "name": "work"}
    ).json()
    brief = desk.post(
        "/api/board",
        json={
            "title": "No media",
            "brief": "just code",
            "spec": "- it builds",
            "project_id": proj["id"],
        },
    ).json()

    seen: list = []
    monkeypatch.setattr(
        "apps.api.seat_sync.set_workspace",
        lambda seat, path, media=None: seen.append((path, media)) or {"ok": True},
    )
    sid = desk.post(
        "/api/sessions/check-in", json={"seat_id": "seat-1", "coder_name": "Ada"}
    ).json()["session"]["id"]
    desk.post(f"/api/sessions/{sid}/claim", json={"board_id": brief["id"]})

    assert seen[0][1] is None


def test_uploading_without_storage_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(object_store, "writer", lambda: None)
    store = Store(tmp_path / "salon.db")
    proj, _item = _project_with_brief(store, tmp_path)

    with pytest.raises(ObjectStoreError) as exc:
        media_ops.add(store, project_id=proj["id"], filename="x.png", data=b"x")
    assert "storage-init" in str(exc.value)
