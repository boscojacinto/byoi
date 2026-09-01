import pytest

from apps.api.store import ProjectBusy, Store


def _claimable(store, project_id, *, title="Fix it"):
    return store.add_board(title, "brief", 60, 40, project_id=project_id)


def test_second_guest_cannot_claim_a_solution_on_a_busy_project(tmp_path):
    store = Store(tmp_path / "salon.db")
    site = tmp_path / "site"
    site.mkdir()
    proj = store.add_project(name="site", local_path=str(site))
    first_item = _claimable(store, proj["id"], title="Fix the header")
    second_item = _claimable(store, proj["id"], title="Fix the footer")

    ada = store.check_in("seat-1", "Ada")
    store.claim(ada["id"], first_item["id"])

    bea = store.check_in("seat-2", "Bea")
    with pytest.raises(ProjectBusy):
        store.claim(bea["id"], second_item["id"])


def test_a_finished_visit_frees_the_project_for_the_next_guest(tmp_path):
    store = Store(tmp_path / "salon.db")
    site = tmp_path / "site"
    site.mkdir()
    proj = store.add_project(name="site", local_path=str(site))
    first_item = _claimable(store, proj["id"], title="Fix the header")
    second_item = _claimable(store, proj["id"], title="Fix the footer")

    ada = store.check_in("seat-1", "Ada")
    store.claim(ada["id"], first_item["id"])
    store.complete(ada["id"])

    bea = store.check_in("seat-2", "Bea")
    store.claim(bea["id"], second_item["id"])  # does not raise
    assert store.session(bea["id"])["board_id"] == second_item["id"]


def test_the_same_guest_can_switch_between_solutions_on_their_own_project(tmp_path):
    store = Store(tmp_path / "salon.db")
    site = tmp_path / "site"
    site.mkdir()
    proj = store.add_project(name="site", local_path=str(site))
    first_item = _claimable(store, proj["id"], title="Fix the header")
    second_item = _claimable(store, proj["id"], title="Fix the footer")

    ada = store.check_in("seat-1", "Ada")
    store.claim(ada["id"], first_item["id"])
    store.claim(ada["id"], second_item["id"])  # does not raise
    assert store.session(ada["id"])["board_id"] == second_item["id"]


def test_projects_without_a_desk_project_are_never_busy(tmp_path):
    store = Store(tmp_path / "salon.db")
    item = store.add_board("Freeform", "brief", 60, 40)

    ada = store.check_in("seat-1", "Ada")
    store.claim(ada["id"], item["id"])

    bea = store.check_in("seat-2", "Bea")
    store.claim(bea["id"], item["id"])  # does not raise: no project to lock


def test_set_project_vercel_is_first_write_wins(tmp_path):
    store = Store(tmp_path / "salon.db")
    site = tmp_path / "site"
    site.mkdir()
    proj = store.add_project(name="site", local_path=str(site))

    store.set_project_vercel(proj["id"], vercel_project_id="prj_1", vercel_org_id="org_1")
    # A later deploy of a different solution on the same project must not
    # overwrite the linkage — every solution ships to the same Vercel project.
    store.set_project_vercel(proj["id"], vercel_project_id="prj_2", vercel_org_id="org_2")

    row = store.project(proj["id"])
    assert row["vercel_project_id"] == "prj_1"
    assert row["vercel_org_id"] == "org_1"


def test_set_project_infra_round_trips_and_replaces(tmp_path):
    store = Store(tmp_path / "salon.db")
    site = tmp_path / "site"
    site.mkdir()
    proj = store.add_project(name="site", local_path=str(site))

    assert store.project(proj["id"])["infra_resources"] == []

    first = [{"kind": "postgres", "provider": "neon", "id": "prj_1", "env": {"DATABASE_URL": "x"}}]
    store.set_project_infra(proj["id"], first)
    assert store.project(proj["id"])["infra_resources"] == first

    # Unlike the Vercel linkage, infra is replaced wholesale by the caller's
    # already-merged list — the next deploy adding redis on top of postgres.
    merged = first + [{"kind": "redis", "provider": "upstash", "id": "db_1", "env": {"REDIS_URL": "y"}}]
    store.set_project_infra(proj["id"], merged)
    assert store.project(proj["id"])["infra_resources"] == merged
