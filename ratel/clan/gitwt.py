"""Git worktrees for a clan.

One writer holds `issue-<n>` in its own worktree; every other role gets a
detached worktree at that branch's tip, fast-forwarded with `sync_detached`
before a review round. Git itself enforces the one-writer rule — a branch can
only be checked out in one worktree — so this module mostly turns git's errors
into ones a clan operator can act on.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

SLUG_RE = re.compile(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$")


def git(cwd: Path | str, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise ValueError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def repo_slug(repo: Path | str) -> str:
    """`owner/name` from origin, https or ssh."""
    url = git(repo, "remote", "get-url", "origin")
    m = SLUG_RE.search(url)
    if not m:
        raise ValueError(f"cannot read an owner/name slug from origin url {url!r}")
    return m.group(1)


def default_branch(repo: Path | str) -> str:
    head = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    return head.rsplit("/", 1)[-1] if head else git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _worktrees(repo: Path | str) -> dict[Path, dict]:
    out: dict[Path, dict] = {}
    cur: dict = {}
    for line in git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur:
                out[Path(cur["worktree"]).resolve()] = cur
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value
    return out


def _branch_exists(repo: Path | str, branch: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False) != ""


def _guard_path(repo: Path | str, path: Path) -> bool:
    """True if `path` is already this repo's worktree; raise if something else lives there."""
    path = Path(path)
    if path.resolve() in _worktrees(repo):
        return True
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"{path} exists and is not a worktree of this repo — refusing to touch it")
    return False


def add_writer_worktree(repo: Path | str, path: Path | str, branch: str,
                        base: str | None = None) -> Path:
    """Check `branch` out at `path`, creating it from `base` the first time."""
    path = Path(path)
    if _guard_path(repo, path):
        return path
    for other, info in _worktrees(repo).items():
        if info.get("branch", "").rsplit("/", 1)[-1] == branch:
            raise ValueError(f"branch {branch} is already checked out at {other} — one writer per branch")
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add"]
    if _branch_exists(repo, branch):
        args += [str(path), branch]
    else:
        args += ["-b", branch, str(path), base or default_branch(repo)]
    git(repo, *args)
    return path


def add_detached_worktree(repo: Path | str, path: Path | str, at: str) -> Path:
    """A read-only-ish worktree parked at `at`'s current commit."""
    path = Path(path)
    if _guard_path(repo, path):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", str(path), at)
    return path


def is_dirty(path: Path | str) -> bool:
    return bool(git(path, "status", "--porcelain"))


def sync_detached(repo: Path | str, path: Path | str, at: str) -> str:
    """Move a detached worktree to `at`'s tip. Refuses rather than discard local edits."""
    if is_dirty(path):
        raise ValueError(f"{path} has uncommitted changes (dirty) — commit or discard them before sync")
    rev = git(repo, "rev-parse", at)
    git(path, "checkout", "--detach", rev)
    return rev


def remove_worktree(repo: Path | str, path: Path | str, force: bool = True) -> None:
    path = Path(path)
    if path.resolve() in _worktrees(repo):
        git(repo, "worktree", "remove", *(["--force"] if force else []), str(path))
    git(repo, "worktree", "prune")


def exclude_in_worktree(path: Path | str, patterns: list[str]) -> Path:
    """Ignore paths in this worktree only — harness files must not dirty the clan's diff.

    `info/exclude` lives in the common git dir and is shared by every worktree,
    so a per-worktree ignore has to go through a worktree-scoped
    `core.excludesFile` (which needs `extensions.worktreeConfig`). Verified: the
    main checkout's status is unaffected.
    """
    path = Path(path)
    git_dir = Path(git(path, "rev-parse", "--absolute-git-dir"))
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    have = exclude.read_text().splitlines() if exclude.exists() else []
    exclude.write_text("\n".join(have + [p for p in patterns if p not in have]) + "\n")
    git(path, "config", "--local", "extensions.worktreeConfig", "true")
    git(path, "config", "--worktree", "core.excludesFile", str(exclude))
    return exclude
