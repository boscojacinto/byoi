"""The board a fresh desk opens with: fixes on The Fusion Studio site."""

from pathlib import Path

from fastapi.testclient import TestClient

from apps.api import seed_board
from apps.api.main import create_app
from apps.api.store import Store


def _desk(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path), client=("127.0.0.1", 50000))


def test_defaults_are_the_website_fixes(tmp_path: Path):
    items = _desk(tmp_path).get("/api/board").json()["items"]
    titles = [i["title"] for i in items]
    assert titles == [i["title"] for i in seed_board.SEED_BOARD]
    for item in items:
        assert item["project"]["github"] == seed_board.SEED_PROJECT["github"]
        assert item["spec"].strip(), f"{item['title']} ships without an acceptance spec"


def test_every_default_names_its_fix(tmp_path: Path):
    board = {i["id"]: i for i in _desk(tmp_path).get("/api/board").json()["items"]}
    joined = {k: (v["brief"] + v["spec"]).lower() for k, v in board.items()}
    tattoo = next(v for k, v in joined.items() if k.startswith("seed-no-tattoo"))
    cafe = next(v for k, v in joined.items() if k.startswith("seed-morning-cafe"))
    ticker = next(v for k, v in joined.items() if k.startswith("seed-services-ticker"))
    assert "tattoo" in tattoo
    assert "morning" in cafe and "after dark" in cafe
    for service in seed_board.SERVICES:
        assert service.lower() in ticker


def test_one_project_row_shared_by_every_default(tmp_path: Path):
    store = Store(tmp_path / "salon.db")
    assert len({i["project_id"] for i in store.board()}) == 1
    assert len(store.projects()) == 1


def test_reopening_the_desk_does_not_duplicate_the_board(tmp_path: Path):
    db = tmp_path / "salon.db"
    first = Store(db)
    ids = [i["id"] for i in first.board()]
    first.close()
    again = Store(db)
    assert [i["id"] for i in again.board()] == ids
    assert len(again.projects()) == 1


def test_a_new_seed_version_replaces_the_old_defaults(tmp_path: Path, monkeypatch):
    db = tmp_path / "salon.db"
    old = Store(db)
    mine = old.add_board("Host wrote this", "keep me", 60, 40)
    old.close()

    monkeypatch.setattr(seed_board, "SEED_VERSION", "fusionstudio-next")
    monkeypatch.setattr(
        seed_board,
        "SEED_BOARD",
        [{"id": "seed-later", "title": "A fix told later", "brief": "b", "wellness_minutes": 90, "break_after": 50, "spec": "s"}],
    )
    fresh = Store(db)
    titles = [i["title"] for i in fresh.board()]
    assert "A fix told later" in titles
    assert "Host wrote this" in titles, "host briefs must survive a re-seed"
    assert not any(t in titles for t in [i["title"] for i in SEED_BOARD_V1])
    assert len(fresh.projects()) == 1, "the project row is reused, not cloned per version"


SEED_BOARD_V1 = list(seed_board.SEED_BOARD)


def test_a_claimed_default_is_unpublished_not_deleted(tmp_path: Path, monkeypatch):
    db = tmp_path / "salon.db"
    old = Store(db)
    claimed = old.board()[0]
    sess = old.check_in("seat-1", "Ada")
    old.claim(sess["id"], claimed["id"])
    old.close()

    monkeypatch.setattr(seed_board, "SEED_VERSION", "fusionstudio-next")
    monkeypatch.setattr(seed_board, "SEED_BOARD", [])
    fresh = Store(db)
    assert claimed["id"] not in {i["id"] for i in fresh.board()}
    assert fresh.board_item(claimed["id"])["published"] == 0
    assert fresh.session(sess["id"])["board_id"] == claimed["id"]


def test_legacy_peripage_defaults_are_retired(tmp_path: Path):
    """A salon.db from before this board existed: the old briefs come off."""
    db = tmp_path / "salon.db"
    store = Store(db)
    for title in seed_board.LEGACY_TITLES:
        store.add_board(title, "the old default", 60, 40)
    # Rewind to a database that has never seen a versioned seed.
    store.conn.execute("DELETE FROM meta WHERE key='board_seed'")
    store.conn.execute("DELETE FROM board WHERE id LIKE 'seed-%'")
    store.conn.commit()
    store.close()

    fresh = Store(db)
    titles = {i["title"] for i in fresh.board()}
    assert not (titles & set(seed_board.LEGACY_TITLES))
    assert titles == {i["title"] for i in seed_board.SEED_BOARD}
