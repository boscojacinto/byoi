"""What a guest is allowed to see of their own grading report.

The stored report quotes the suite verbatim: JUnit failure messages carry the
assertion source, test file paths and tracebacks, and a run that never produced
a report splices raw stdout into the summary. A guest may see which spec
requirements passed and why one failed — never the suite that judged them.

So nothing here passes a runner string through. Every sentence a guest reads is
either written by the host (the spec clause) or built from this module's own
templates, filled only with tokens that matched an allowlist. Text matching
nothing degrades to a generic reason rather than leaking; the requirement it
sits under still names the behaviour that was missing, which is the actionable
half anyway.
"""

from __future__ import annotations

import re
from typing import Any

# A scalar is safe to echo: it is a value the guest's own program produced or
# the spec named, not a fragment of the suite. Anything longer or more
# structured — a dict repr, a call expression, a path — is not on the allowlist.
_SCALAR = r"""(-?\d+(?:\.\d+)?|'[^'\n]{0,32}'|"[^"\n]{0,32}"|True|False|None)"""

_COMPARISON = re.compile(rf"^assert\s+{_SCALAR}\s*(==|!=|<|>|<=|>=)\s*{_SCALAR}\s*$")
_STATUS = re.compile(r"^assert\s+(?P<got>[1-5]\d\d)\s*==\s*(?P<want>[1-5]\d\d)\s*$")
_EXCEPTION = re.compile(r"^(?P<cls>[A-Z][A-Za-z0-9_]{0,40}(?:Error|Exception|Warning))\b")

_GENERIC = "This check did not pass."
_ASSERTION = "The behaviour the spec asks for did not hold."

# Phrasings the grader wrote itself, safe to hand straight back.
_OURS = {
    "the suite reported no result for this requirement": (
        "No check ever ran for this requirement."
    ),
}


def reason(detail: str) -> str:
    """One plain-English line for why a case failed — built, never quoted."""
    text = (detail or "").strip()
    if not text:
        return _GENERIC
    first = text.splitlines()[0].strip()
    lowered = first.lower()

    if lowered in _OURS:
        return _OURS[lowered]
    if "timed out" in lowered or "timeout" in lowered:
        return "The check did not finish in time."
    if lowered.startswith("skipped"):
        return "The check was skipped, so the requirement is unproven."

    status = _STATUS.match(first)
    if status:
        return (
            f"The response came back {status.group('got')} "
            f"where the spec requires {status.group('want')}."
        )

    compared = _COMPARISON.match(first)
    if compared:
        left, op, right = compared.group(1), compared.group(2), compared.group(3)
        if op == "==":
            return f"Expected {right}, but got {left}."
        return f"{left} and {right} did not satisfy `{op}` as the spec requires."

    raised = _EXCEPTION.match(first)
    if raised:
        cls = raised.group("cls")
        if cls == "AssertionError":
            return _ASSERTION
        return f"The code raised {cls} while the requirement was being checked."

    return _GENERIC


def label(case: dict[str, Any]) -> str:
    """Prefer the host's own spec clause; otherwise a de-pathed test name."""
    requirement = str(case.get("requirement") or "").strip()
    if requirement:
        return requirement
    name = str(case.get("name") or "").strip()
    if not name:
        return "Requirement"
    if name.lower().startswith("completeness: "):
        return name
    # `.byoi/tests/test_notes.py::test_unknown_id` reveals the suite's layout;
    # the trailing name on its own does not.
    name = name.split("::")[-1].strip()
    name = re.sub(r"^test[_\s-]+", "", name)
    name = name.replace("_", " ").strip()
    return name or "Requirement"


def redact(report: Any) -> dict[str, Any] | None:
    """The guest-facing shape of a stored grading report."""
    if not isinstance(report, dict):
        return None
    if report.get("grader_error"):
        # Neither grader ran. Reporting this as a failed case would blame the
        # guest for the salon's outage, and the exception text is ours.
        return {
            "summary": "We could not finish grading this one.",
            "passed": 0,
            "failed": 0,
            "blocked": True,
            "note": "Nothing to do with your code — ask the host to take a look.",
            "cases": [],
        }
    cases: list[dict[str, Any]] = []
    for case in report.get("cases") or []:
        if not isinstance(case, dict):
            continue
        ok = bool(case.get("pass"))
        entry: dict[str, Any] = {"name": label(case), "pass": ok}
        if not ok:
            entry["reason"] = reason(str(case.get("detail") or ""))
        cases.append(entry)

    passed = sum(1 for c in cases if c["pass"])
    failed = len(cases) - passed
    out: dict[str, Any] = {
        "summary": _summary(passed, failed),
        "passed": passed,
        "failed": failed,
        "cases": cases,
    }
    if not cases and str(report.get("summary") or "").strip():
        # The suite fell over before it graded anything. Say that much and no
        # more: the stored summary at this point is raw runner output.
        out["note"] = "The suite did not produce a result. The host has the details."
    return out


def _summary(passed: int, failed: int) -> str:
    total = passed + failed
    if not total:
        return "Nothing to check on this brief."
    if not failed:
        return f"All {total} checks passed."
    return f"{passed} of {total} checks passed."
