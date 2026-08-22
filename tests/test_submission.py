import subprocess
from pathlib import Path

import pytest

from apps.seat.submission import SubmissionError, capture, ref_for


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(a, cwd=str(path), check=True, capture_output=True)
    run("git", "init", "-q", ".")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    return path


def _git(path: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(path), capture_output=True, text=True)
    return (out.stdout or "").strip()


def test_ref_is_namespaced_and_sanitised():
    assert ref_for("ab/cd 12") == "refs/byoi/submissions/ab-cd-12"
    assert ref_for("").startswith("refs/byoi/submissions/")


def test_capture_leaves_the_guest_working_state_untouched(tmp_path: Path):
    repo = _repo(tmp_path / "proj")
    (repo / "a.txt").write_text("base\n")
    subprocess.run(["git", "add", "a.txt"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=str(repo), check=True, capture_output=True)

    # The guest is mid-edit: dirty tracked file, a staged file, and an untracked one.
    (repo / "a.txt").write_text("base\ndirty\n")
    (repo / "staged.txt").write_text("staged\n")
    (repo / "untracked.txt").write_text("new\n")
    subprocess.run(["git", "add", "staged.txt"], cwd=str(repo), check=True, capture_output=True)

    before = (_git(repo, "status", "--porcelain"), _git(repo, "rev-parse", "HEAD"),
              _git(repo, "ls-files", "-s"))

    info = capture(cwd=repo, session_id="sid1")

    assert (_git(repo, "status", "--porcelain"), _git(repo, "rev-parse", "HEAD"),
            _git(repo, "ls-files", "-s")) == before
    # Everything the guest had is on the ref, including what they never staged.
    listed = _git(repo, "ls-tree", "-r", "--name-only", info["ref"]).split()
    assert set(listed) == {"a.txt", "staged.txt", "untracked.txt"}
    assert _git(repo, "show", f"{info['ref']}:a.txt") == "base\ndirty"
    # And no branch tracks it.
    assert "byoi" not in _git(repo, "branch", "-a")
    assert info["pushed"] is False


def test_capture_works_before_the_first_commit(tmp_path: Path):
    repo = _repo(tmp_path / "fresh")
    (repo / "only.txt").write_text("hello\n")
    info = capture(cwd=repo, session_id="sid2")
    assert _git(repo, "show", f"{info['ref']}:only.txt") == "hello"


def test_capture_outside_a_repo_is_a_precondition_error(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SubmissionError, match="not a git repository"):
        capture(cwd=plain, session_id="sid3")


def test_push_without_an_origin_is_a_precondition_error(tmp_path: Path):
    repo = _repo(tmp_path / "noremote")
    (repo / "f.txt").write_text("x\n")
    with pytest.raises(SubmissionError, match="no origin"):
        capture(cwd=repo, session_id="sid4", push=True)
