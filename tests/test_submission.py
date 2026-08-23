import subprocess
from pathlib import Path

import pytest

from apps.seat.submission import SubmissionError, capture, push_hint, ref_for


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


def test_push_hint_explains_an_https_remote_with_no_credentials():
    msg = push_hint("fatal: Authentication failed for 'https://github.com/o/r/'", "https://github.com/o/r")
    assert "gh auth login" in msg
    assert "git remote set-url origin git@github.com" in msg
    # The original git error is kept for anyone who wants it.
    assert "Authentication failed" in msg


def test_push_hint_points_at_the_ssh_key_for_an_ssh_remote():
    msg = push_hint("git@github.com: Permission denied (publickey).", "git@github.com:o/r.git")
    assert "SSH key is not authorised" in msg
    assert "gh auth login" not in msg


def test_push_hint_passes_unrelated_errors_through():
    assert push_hint("fatal: the remote end hung up", "git@github.com:o/r.git") == (
        "fatal: the remote end hung up"
    )


def test_push_failure_is_reported_with_the_hint(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path / "proj")
    (repo / "f.txt").write_text("x\n")
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/o/r"],
        cwd=str(repo), check=True, capture_output=True,
    )
    real = subprocess.run

    def fake(argv, **kwargs):
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(
                argv, 128, "", "fatal: Authentication failed for 'https://github.com/o/r/'"
            )
        return real(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(SubmissionError, match="gh auth login"):
        capture(cwd=repo, session_id="sid", push=True)
