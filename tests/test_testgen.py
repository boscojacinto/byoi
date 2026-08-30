import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import testgen
from apps.api.main import create_app
from apps.api.testgen import TestgenError

PLAN = {
    "framework": "pytest",
    "image": "python:3.12-slim",
    "setup": "",
    "command": "true",
    "files": [{"path": ".byoi/tests/test_spec.py", "content": "def test_quiet_zone():\n    pass\n"}],
    "cases": [
        {"name": "quiet zone", "requirement": "quiet zone is 4 modules", "node_id": ".byoi/tests/test_spec.py::test_quiet_zone"},
        {"name": "contrast", "requirement": "contrast >= 4.5", "node_id": ".byoi/tests/test_spec.py::test_contrast"},
    ],
}


def junit(cases: list[tuple[str, str, bool]]) -> str:
    body = "".join(
        f'<testcase classname="{cls}" name="{name}">'
        + ("" if ok else '<failure message="boom">trace</failure>')
        + "</testcase>"
        for cls, name, ok in cases
    )
    return f'<?xml version="1.0"?><testsuite name="pytest" tests="{len(cases)}">{body}</testsuite>'


# ------------------------------------------------------------------ stage A parsing


def test_parse_plan_structured_output():
    raw = json.dumps({"type": "result", "structured_output": PLAN})
    plan = testgen.parse_plan(raw)
    assert plan["framework"] == "pytest"
    assert len(plan["cases"]) == 2


def test_parse_plan_rejects_suite_with_no_cases():
    bad = {**PLAN, "cases": []}
    with pytest.raises(TestgenError, match="mapped no spec requirements"):
        testgen.parse_plan(json.dumps({"structured_output": bad}))


def test_parse_plan_rejects_suite_with_no_files():
    bad = {**PLAN, "files": []}
    with pytest.raises(TestgenError, match="no test files"):
        testgen.parse_plan(json.dumps({"structured_output": bad}))


def test_generate_prompt_is_blind_and_demands_coverage():
    text = testgen.generate_prompt(title="Slip QR", spec="- quiet zone\n- contrast")
    assert "have NOT seen the solution" in text
    assert "Every requirement gets at least one entry" in text
    assert testgen.JUNIT_REL in text


def test_generate_suite_requires_host_login(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_CLAUDE_ACCOUNTS_DIR", str(tmp_path))
    with pytest.raises(TestgenError, match="is not logged in"):
        testgen.generate_suite(spec="- something", title="t")


# ---------------------------------------------------------------- suite materialise


def test_write_suite_rejects_escaping_paths(tmp_path):
    plan = {**PLAN, "files": [{"path": "../evil.py", "content": "x"}]}
    with pytest.raises(TestgenError, match="escapes the repo"):
        testgen.write_suite(tmp_path, plan)


def test_write_suite_writes_files(tmp_path):
    testgen.write_suite(tmp_path, PLAN)
    assert (tmp_path / ".byoi" / "tests" / "test_spec.py").is_file()


# ---------------------------------------------------------------------- sandboxing


def test_docker_argv_has_no_network_and_no_desk_env(tmp_path):
    argv = testgen.run_argv(tmp_path, PLAN, "docker")
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "-e" in argv  # only HOME=/tmp is passed in
    assert not any(a.startswith("BYOI_") for a in argv)


def test_clean_env_drops_byoi_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOI_HOST_TOKEN", "supersecret")
    env = testgen._clean_env(tmp_path)
    assert "BYOI_HOST_TOKEN" not in env
    assert set(env) <= {"PATH", "HOME", "LANG", "PYTHONDONTWRITEBYTECODE"}


# ----------------------------------------------------------------- node id matching


def test_select_matches_on_name_despite_an_unrelated_classname():
    """Runners disagree about classname; rejecting on it would fake completeness failures."""
    reported = [{"name": "test_value", "classname": "t", "pass": True, "skipped": False, "detail": ""}]
    assert testgen._select(".byoi/tests/test_spec.py::test_value", reported) == reported


def test_select_matches_when_classname_is_absent():
    reported = [{"name": "test_value", "classname": "", "pass": True, "skipped": False, "detail": ""}]
    assert len(testgen._select(".byoi/tests/test_spec.py::test_value", reported)) == 1


def test_select_uses_classname_only_to_break_ties():
    reported = [
        {"name": "test_value", "classname": "byoi.tests.other", "pass": True, "skipped": False, "detail": ""},
        {"name": "test_value", "classname": "byoi.tests.test_spec", "pass": False, "skipped": False, "detail": "x"},
    ]
    hits = testgen._select(".byoi/tests/test_spec.py::test_value", reported)
    assert len(hits) == 1
    assert hits[0]["classname"] == "byoi.tests.test_spec"


def test_select_handles_a_node_id_without_a_path():
    reported = [{"name": "renders the slip", "classname": "", "pass": True, "skipped": False, "detail": ""}]
    assert len(testgen._select("renders the slip", reported)) == 1


def test_select_finds_nothing_for_an_unknown_test():
    reported = [{"name": "test_value", "classname": "", "pass": True, "skipped": False, "detail": ""}]
    assert testgen._select(".byoi/tests/test_spec.py::test_missing", reported) == []


# ------------------------------------------------------------------ stage C grading


def test_grade_all_pass(tmp_path):
    (tmp_path / ".byoi").mkdir()
    (tmp_path / testgen.JUNIT_REL).write_text(
        junit([("byoi.tests.test_spec", "test_quiet_zone", True),
               ("byoi.tests.test_spec", "test_contrast", True)])
    )
    report = testgen.grade(PLAN, tmp_path, {"runtime": "none", "output": "", "timeout": False})
    assert report["passed"] == 2
    assert report["failed"] == 0


def test_grade_reports_failure_detail(tmp_path):
    (tmp_path / ".byoi").mkdir()
    (tmp_path / testgen.JUNIT_REL).write_text(
        junit([("byoi.tests.test_spec", "test_quiet_zone", True),
               ("byoi.tests.test_spec", "test_contrast", False)])
    )
    report = testgen.grade(PLAN, tmp_path, {"runtime": "none", "output": "", "timeout": False})
    assert report["failed"] == 1
    assert report["cases"][1]["detail"] == "boom"


def test_grade_flags_untested_requirement_as_incomplete(tmp_path):
    """A suite that quietly skips a requirement must not score 100%."""
    (tmp_path / ".byoi").mkdir()
    (tmp_path / testgen.JUNIT_REL).write_text(
        junit([("byoi.tests.test_spec", "test_quiet_zone", True)])
    )
    report = testgen.grade(PLAN, tmp_path, {"runtime": "none", "output": "", "timeout": False})
    assert report["passed"] == 1
    assert report["failed"] == 1
    missing = report["cases"][-1]
    assert missing["name"] == "completeness: contrast >= 4.5"
    assert missing["pass"] is False
    assert "no test" in report["summary"]


def test_grade_counts_a_skipped_test_as_failed(tmp_path):
    (tmp_path / ".byoi").mkdir()
    (tmp_path / testgen.JUNIT_REL).write_text(
        '<?xml version="1.0"?><testsuite><testcase classname="byoi.tests.test_spec" '
        'name="test_quiet_zone"><skipped message="todo"/></testcase>'
        '<testcase classname="byoi.tests.test_spec" name="test_contrast"/></testsuite>'
    )
    report = testgen.grade(PLAN, tmp_path, {"runtime": "none", "output": "", "timeout": False})
    assert report["cases"][0]["pass"] is False


def test_grade_fails_a_requirement_if_any_of_its_tests_fail(tmp_path):
    plan = {
        **PLAN,
        "cases": [{"name": "quiet zone", "requirement": "quiet zone is 4 modules",
                   "node_id": "test_quiet_zone"}],
    }
    (tmp_path / ".byoi").mkdir()
    (tmp_path / testgen.JUNIT_REL).write_text(
        junit([("a", "test_quiet_zone", True), ("b", "test_quiet_zone", False)])
    )
    report = testgen.grade(plan, tmp_path, {"runtime": "none", "output": "", "timeout": False})
    assert report["failed"] == 1


def test_grade_without_a_report_explains_itself(tmp_path):
    report = testgen.grade(
        PLAN, tmp_path, {"runtime": "docker", "output": "ModuleNotFoundError", "timeout": False}
    )
    assert report["passed"] == 0
    assert "no JUnit report" in report["summary"]
    assert "ModuleNotFoundError" in report["summary"]


def test_grade_reports_a_timeout(tmp_path):
    report = testgen.grade(
        PLAN, tmp_path, {"runtime": "docker", "output": "", "timeout": True, "returncode": 124}
    )
    assert "timed out" in report["summary"]


# ------------------------------------------------------------------- end to end run


def test_run_pipeline_end_to_end(tmp_path, monkeypatch):
    """Generation stubbed; fetch, sandboxed run, and grading are real."""
    origin = tmp_path / "project"
    origin.mkdir()
    run = lambda *a: subprocess.run(a, cwd=str(origin), check=True, capture_output=True)
    run("git", "init", "-q", ".")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (origin / "app.py").write_text("VALUE = 42\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")

    from apps.seat.submission import capture

    info = capture(cwd=origin, session_id="sid-e2e")

    plan = {
        "framework": "pytest",
        "image": "python:3.12-slim",
        "setup": "",
        # Assert against the fetched solution, then emit JUnit ourselves so the
        # test does not depend on a runner existing inside the scrubbed env.
        "command": (
            f"{sys.executable} -c \""
            "import pathlib;"
            "ok = 'VALUE = 42' in pathlib.Path('app.py').read_text();"
            "cls = 'byoi.tests.test_spec';"
            "body = '<testcase classname=\\\"%s\\\" name=\\\"test_value\\\"/>' % cls if ok "
            "else '<testcase classname=\\\"%s\\\" name=\\\"test_value\\\"><failure message=\\\"wrong\\\"/></testcase>' % cls;"
            "pathlib.Path('.byoi').mkdir(exist_ok=True);"
            "pathlib.Path('.byoi/junit.xml').write_text('<testsuite>'+body+'</testsuite>')\""
        ),
        "files": [{"path": ".byoi/tests/test_spec.py", "content": "# generated\n"}],
        "cases": [
            {"name": "value is 42", "requirement": "VALUE must be 42",
             "node_id": ".byoi/tests/test_spec.py::test_value"}
        ],
    }
    monkeypatch.setattr(testgen, "generate_suite", lambda **k: plan)
    monkeypatch.setenv("BYOI_TESTGEN_RUNTIME", "none")
    monkeypatch.setenv("BYOI_VERIFY_RUNS_DIR", str(tmp_path / "runs"))

    report = testgen.run(
        spec="- VALUE must be 42",
        title="t",
        source=info["toplevel"],
        ref=info["ref"],
        session_id="sid-e2e",
    )
    assert report["passed"] == 1
    assert report["failed"] == 0
    dest = tmp_path / "runs" / "sid-e2e"
    assert (dest / "app.py").is_file()
    assert (dest / ".byoi" / "plan.json").is_file()
    assert (dest / ".byoi" / "report.json").is_file()


def test_run_without_spec_short_circuits():
    report = testgen.run(spec="  ", title="t", source="x", ref="y", session_id="z")
    assert report["cases"] == []
    assert "No spec" in report["summary"]


def test_fetch_submission_reports_a_bad_ref(tmp_path):
    origin = tmp_path / "empty"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=str(origin), check=True, capture_output=True)
    with pytest.raises(TestgenError):
        testgen.fetch_submission(source=str(origin), ref="refs/byoi/submissions/nope",
                                 dest=tmp_path / "dest")


# ------------------------------------------------------------------------ fallback


def test_complete_falls_back_to_seat_and_says_why(tmp_path: Path, monkeypatch):
    """No host account credentials -> grade on the seat, with the reason on the report."""
    monkeypatch.setattr(
        "apps.api.seat_sync.verify_solution",
        lambda *a, **k: {"summary": "seat graded it", "passed": 1, "failed": 0,
                         "cases": [{"name": "quiet zone", "pass": True, "detail": ""}]},
    )
    desk = TestClient(create_app(tmp_path), headers={"Authorization": "Bearer byoi-host"})
    brief = desk.post("/api/board", json={"title": "QR", "brief": "scan", "spec": "- quiet zone"}).json()
    sid = desk.post("/api/sessions/check-in",
                    json={"seat_id": "seat-1", "coder_name": "Ada"}).json()["session"]["id"]
    desk.post(f"/api/sessions/{sid}/claim", json={"board_id": brief["id"]})
    desk.post(f"/api/sessions/{sid}/complete")
    # The desk is told which grader ran and why; the guest is told the result.
    row = desk.get("/api/sessions/grading").json()["sessions"][0]
    assert row["test_report"]["summary"].startswith("Graded on the seat (")
    assert "seat graded it" in row["test_report"]["summary"]
    report = desk.get(f"/api/sessions/{sid}/tests").json()["test_report"]
    assert report["summary"] == "All 1 checks passed."
    assert report["cases"] == [{"name": "quiet zone", "pass": True}]


def test_an_unexpected_host_failure_still_produces_a_report(tmp_path: Path, monkeypatch):
    """A crash in the host pipeline must not leave the phone polling 'running' forever."""
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("apps.api.seat_sync.submit_solution", boom)
    monkeypatch.setattr(
        "apps.api.seat_sync.verify_solution",
        lambda *a, **k: {"summary": "seat graded it", "passed": 1, "failed": 0, "cases": []},
    )
    desk = TestClient(create_app(tmp_path), headers={"Authorization": "Bearer byoi-host"})
    brief = desk.post("/api/board", json={"title": "QR", "brief": "scan", "spec": "- quiet zone"}).json()
    sid = desk.post("/api/sessions/check-in",
                    json={"seat_id": "seat-1", "coder_name": "Ada"}).json()["session"]["id"]
    desk.post(f"/api/sessions/{sid}/claim", json={"board_id": brief["id"]})
    desk.post(f"/api/sessions/{sid}/complete")
    tests = desk.get(f"/api/sessions/{sid}/tests").json()
    assert tests["test_status"] != "running"
    # The guest learns the suite produced nothing; the crash text is the desk's.
    assert tests["test_report"]["note"]
    assert "disk on fire" not in repr(tests["test_report"])
    row = desk.get("/api/sessions/grading").json()["sessions"][0]
    assert "disk on fire" in row["test_report"]["summary"]


def test_a_total_failure_still_produces_a_report(tmp_path: Path, monkeypatch):
    """Both paths down: still a terminal status, not an endless spinner."""
    def boom(*a, **k):
        raise RuntimeError("everything is down")

    monkeypatch.setattr("apps.api.seat_sync.submit_solution", boom)
    monkeypatch.setattr("apps.api.seat_sync.verify_solution", boom)
    desk = TestClient(create_app(tmp_path), headers={"Authorization": "Bearer byoi-host"})
    brief = desk.post("/api/board", json={"title": "QR", "brief": "scan", "spec": "- quiet zone"}).json()
    sid = desk.post("/api/sessions/check-in",
                    json={"seat_id": "seat-1", "coder_name": "Ada"}).json()["session"]["id"]
    desk.post(f"/api/sessions/{sid}/claim", json={"board_id": brief["id"]})
    desk.post(f"/api/sessions/{sid}/complete")
    tests = desk.get(f"/api/sessions/{sid}/tests").json()
    assert tests["test_status"] == "failed"
    # A dead pipeline is not a failed check — the phone must not blame the guest.
    assert tests["test_report"]["blocked"] is True
    assert tests["test_report"]["cases"] == []
    assert "everything is down" not in repr(tests["test_report"])
    row = desk.get("/api/sessions/grading").json()["sessions"][0]
    assert "everything is down" in row["test_report"]["summary"]


# ------------------------------------------------------------------ smoke mode


def test_smoke_prompt_targets_the_env_var_not_a_hardcoded_host():
    text = testgen.smoke_prompt(title="Notes", spec="- health is 200", url="https://x.vercel.app")
    assert "BYOI_TARGET_URL" in text
    assert "never hard-code the host" in text
    assert "have NOT seen the code" in text


def test_smoke_run_opens_the_network_and_passes_the_url(tmp_path):
    plan = {**PLAN, "command": "true"}
    argv = testgen.run_argv(
        tmp_path, plan, "docker", network=True, env_extra={"BYOI_TARGET_URL": "https://x"}
    )
    assert argv[argv.index("--network") + 1] == "bridge"
    assert "BYOI_TARGET_URL=https://x" in argv


def test_the_sandboxed_suite_still_has_no_network(tmp_path):
    argv = testgen.run_argv(tmp_path, PLAN, "docker")
    assert argv[argv.index("--network") + 1] == "none"


def test_smoke_without_a_url_is_refused():
    with pytest.raises(TestgenError, match="no deployment URL"):
        testgen.run_smoke(spec="- x", title="t", url="", session_id="s")


def test_smoke_runs_only_generated_code_never_the_guest_tree(tmp_path, monkeypatch):
    """Network is open for this run, so the guest's code must not be in the directory."""
    plan = {
        **PLAN,
        "command": (
            "mkdir -p .byoi && printf '%s' "
            "'<testsuite><testcase classname=\"t\" name=\"test_quiet_zone\"/>"
            "<testcase classname=\"t\" name=\"test_contrast\"/></testsuite>' > .byoi/junit.xml"
        ),
    }
    monkeypatch.setattr(testgen, "generate_suite", lambda **k: plan)
    monkeypatch.setenv("BYOI_TESTGEN_RUNTIME", "none")
    monkeypatch.setenv("BYOI_VERIFY_RUNS_DIR", str(tmp_path / "runs"))

    report = testgen.run_smoke(
        spec="- quiet zone\n- contrast", title="t", url="https://x.vercel.app", session_id="sid"
    )
    assert report["passed"] == 2
    assert report["summary"].startswith("Against https://x.vercel.app")
    dest = tmp_path / "runs" / "sid-smoke"
    # Only what we generated: the tests and our bookkeeping, no project files.
    assert sorted(p.name for p in dest.iterdir()) == [".byoi"]
