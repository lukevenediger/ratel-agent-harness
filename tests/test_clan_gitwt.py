"""Worktree layout for a clan: one writer on issue-<n>, everyone else detached at its tip."""
import subprocess

import pytest

from ratel.clan import gitwt


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A checkout with a bare `origin`, one commit on `main`, and a real remote-tracking HEAD."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    git(work, "config", "user.email", "clan@example.com")
    git(work, "config", "user.name", "Clan")
    (work / "README.md").write_text("hello\n")
    git(work, "add", "README.md")
    git(work, "commit", "-m", "init")
    git(work, "push", "-u", "origin", "main")
    git(work, "remote", "set-url", "origin", "https://github.com/acme/widget.git")
    return work


def test_repo_slug_from_https_and_ssh(repo):
    assert gitwt.repo_slug(repo) == "acme/widget"
    git(repo, "remote", "set-url", "origin", "git@github.com:acme/widget.git")
    assert gitwt.repo_slug(repo) == "acme/widget"


def test_default_branch(repo):
    assert gitwt.default_branch(repo) == "main"


def test_add_writer_worktree_creates_then_reuses_the_branch(repo, tmp_path):
    wt = tmp_path / "wt" / "1-developer"
    gitwt.add_writer_worktree(repo, wt, "issue-1")
    assert (wt / "README.md").exists()
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "issue-1"
    (wt / "a.txt").write_text("work\n")
    git(wt, "add", "a.txt"); git(wt, "commit", "-m", "work")
    head = git(wt, "rev-parse", "HEAD")

    gitwt.remove_worktree(repo, wt)
    assert not wt.exists()
    again = tmp_path / "wt" / "1-developer-again"
    gitwt.add_writer_worktree(repo, again, "issue-1")   # branch already exists: reuse, don't recreate
    assert git(again, "rev-parse", "HEAD") == head


def test_the_same_branch_cannot_be_checked_out_twice(repo, tmp_path):
    gitwt.add_writer_worktree(repo, tmp_path / "wt" / "a", "issue-1")
    with pytest.raises(ValueError, match="issue-1"):
        gitwt.add_writer_worktree(repo, tmp_path / "wt" / "b", "issue-1")


def test_detached_worktree_sits_at_the_branch_tip(repo, tmp_path):
    writer = tmp_path / "wt" / "dev"
    gitwt.add_writer_worktree(repo, writer, "issue-1")
    (writer / "a.txt").write_text("one\n"); git(writer, "add", "a.txt"); git(writer, "commit", "-m", "one")

    reviewer = tmp_path / "wt" / "rev"
    gitwt.add_detached_worktree(repo, reviewer, "issue-1")
    assert git(reviewer, "rev-parse", "HEAD") == git(writer, "rev-parse", "HEAD")
    assert git(reviewer, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"   # detached


def test_sync_detached_moves_to_the_new_tip(repo, tmp_path):
    writer = tmp_path / "wt" / "dev"
    gitwt.add_writer_worktree(repo, writer, "issue-1")
    reviewer = tmp_path / "wt" / "rev"
    gitwt.add_detached_worktree(repo, reviewer, "issue-1")
    before = git(reviewer, "rev-parse", "HEAD")

    (writer / "b.txt").write_text("two\n"); git(writer, "add", "b.txt"); git(writer, "commit", "-m", "two")
    gitwt.sync_detached(repo, reviewer, "issue-1")
    assert git(reviewer, "rev-parse", "HEAD") == git(writer, "rev-parse", "HEAD") != before
    assert (reviewer / "b.txt").exists()


def test_sync_detached_refuses_a_dirty_worktree(repo, tmp_path):
    gitwt.add_writer_worktree(repo, tmp_path / "wt" / "dev", "issue-1")
    reviewer = tmp_path / "wt" / "rev"
    gitwt.add_detached_worktree(repo, reviewer, "issue-1")
    (reviewer / "README.md").write_text("scribbled on\n")
    with pytest.raises(ValueError, match="dirty|uncommitted"):
        gitwt.sync_detached(repo, reviewer, "issue-1")
    assert (reviewer / "README.md").read_text() == "scribbled on\n"   # nothing clobbered


def test_exclude_in_worktree_is_per_worktree(repo, tmp_path):
    wt = tmp_path / "wt" / "dev"
    gitwt.add_writer_worktree(repo, wt, "issue-1")
    gitwt.exclude_in_worktree(wt, ["AGENTS.md", ".clan/"])
    (wt / "AGENTS.md").write_text("brief\n")
    assert "AGENTS.md" not in git(wt, "status", "--porcelain")
    assert "AGENTS.md" not in (repo / ".git" / "info" / "exclude").read_text()   # not the main checkout's


def test_existing_non_worktree_directory_is_refused(repo, tmp_path):
    squatter = tmp_path / "wt" / "dev"
    squatter.mkdir(parents=True)
    (squatter / "mine.txt").write_text("do not touch\n")
    with pytest.raises(ValueError, match="not a worktree|exists"):
        gitwt.add_writer_worktree(repo, squatter, "issue-1")
    assert (squatter / "mine.txt").exists()


def test_adding_an_existing_worktree_again_is_a_no_op(repo, tmp_path):
    wt = tmp_path / "wt" / "dev"
    gitwt.add_writer_worktree(repo, wt, "issue-1")
    (wt / "scratch.txt").write_text("keep me\n")
    gitwt.add_writer_worktree(repo, wt, "issue-1")    # `clan up` is idempotent
    assert (wt / "scratch.txt").exists()


def test_remove_worktree_is_forgiving_when_already_gone(repo, tmp_path):
    wt = tmp_path / "wt" / "dev"
    gitwt.add_writer_worktree(repo, wt, "issue-1")
    gitwt.remove_worktree(repo, wt)
    gitwt.remove_worktree(repo, wt)   # `clan down --prune-worktrees` runs twice happily
    assert not wt.exists()
