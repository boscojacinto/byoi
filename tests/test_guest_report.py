"""The guest sees which requirements failed and why — never the suite."""

from apps.api import guest_report

# What pytest actually puts in a JUnit <failure message="...">: the assertion
# as written in the test file, then the longrepr with the source and the path.
PYTEST_LONGREPR = """assert response.json()["items"][0]["id"] == 3
 +  where response = client.get("/notes?limit=1")
.byoi/tests/test_notes.py:42: AssertionError"""


def _leaks(text: str) -> bool:
    needles = [".byoi", "test_notes", "client.get", "response.json", "items", "where "]
    return any(n in text for n in needles)


def test_reason_never_echoes_the_suite():
    out = guest_report.reason(PYTEST_LONGREPR)
    assert not _leaks(out), out
    assert out == "This check did not pass."


def test_reason_explains_a_status_mismatch():
    assert guest_report.reason("assert 404 == 200") == (
        "The response came back 404 where the spec requires 200."
    )


def test_reason_explains_a_scalar_mismatch():
    assert guest_report.reason("assert 'draft' == 'published'") == (
        "Expected 'published', but got 'draft'."
    )


def test_reason_names_the_exception_but_not_the_frame():
    out = guest_report.reason("KeyError: 'title'\n  File \".byoi/tests/test_a.py\", line 9")
    assert out == "The code raised KeyError while the requirement was being checked."
    assert ".byoi" not in out


def test_reason_covers_assertion_timeout_skip_and_missing():
    assert guest_report.reason("AssertionError") == (
        "The behaviour the spec asks for did not hold."
    )
    assert guest_report.reason("the test run timed out") == (
        "The check did not finish in time."
    )
    assert "skipped" in guest_report.reason("skipped: needs a db").lower()
    assert guest_report.reason("the suite reported no result for this requirement") == (
        "No check ever ran for this requirement."
    )
    assert guest_report.reason("") == "This check did not pass."


def test_label_prefers_the_hosts_spec_clause():
    case = {"name": ".byoi/tests/test_notes.py::test_unknown_id", "requirement": "404 on unknown id"}
    assert guest_report.label(case) == "404 on unknown id"


def test_label_strips_the_suite_path_when_there_is_no_requirement():
    out = guest_report.label({"name": ".byoi/tests/test_notes.py::test_unknown_id"})
    assert out == "unknown id"
    assert guest_report.label({}) == "Requirement"


def test_redact_drops_detail_and_rebuilds_the_summary():
    stored = {
        "summary": "pytest in docker: 1 passed, 1 failed. .byoi/tests/test_notes.py::test_x",
        "passed": 1,
        "failed": 1,
        "cases": [
            {"name": "t_ok", "requirement": "lists notes", "pass": True, "detail": "unused"},
            {
                "name": ".byoi/tests/test_notes.py::test_bad",
                "requirement": "404 on unknown id",
                "pass": False,
                "detail": PYTEST_LONGREPR,
            },
        ],
    }
    out = guest_report.redact(stored)
    assert out["summary"] == "1 of 2 checks passed."
    assert out["passed"] == 1 and out["failed"] == 1
    assert [c["name"] for c in out["cases"]] == ["lists notes", "404 on unknown id"]
    assert "detail" not in out["cases"][0] and "detail" not in out["cases"][1]
    assert "reason" not in out["cases"][0]  # a pass needs no explanation
    assert out["cases"][1]["reason"] == "This check did not pass."
    assert not _leaks(repr(out)), out


def test_redact_hides_raw_runner_output_when_nothing_graded():
    stored = {
        "summary": "The suite produced no JUnit report. Traceback .byoi/tests/test_a.py line 3",
        "passed": 0,
        "failed": 0,
        "cases": [],
    }
    out = guest_report.redact(stored)
    assert out["summary"] == "Nothing to check on this brief."
    assert out["note"] == "The suite did not produce a result. The host has the details."
    assert ".byoi" not in repr(out)


def test_redact_passes_a_clean_sweep_and_a_missing_report():
    assert guest_report.redact(None) is None
    out = guest_report.redact({"cases": [{"name": "a", "pass": True}]})
    assert out["summary"] == "All 1 checks passed."
    assert "note" not in out
