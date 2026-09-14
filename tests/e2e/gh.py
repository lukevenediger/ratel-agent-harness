"""`gh` helpers for the e2e cases.

Every call is scoped to `CLAN_SANDBOX_REPO` — nothing here may touch another
repository. All commands are argv lists with `-R` set from the environment and
`check=True` unless they are explicitly best-effort (cleanup).

Credential design, so nobody "fixes" it backwards: the TEST process runs on the
operator's own `gh` login (cloning, issue creation, cleanup), while the NESTED
CLAN gets only `CLAN_SANDBOX_TOKEN` — a fine-grained PAT scoped to the sandbox
repo — passed as its `GH_TOKEN`. The asymmetric split is deliberate: the clan
must never hold the operator's full credential (see docs/DECISIONS.md,
Decision 28).
"""
from __future__ import annotations

import json
import os
import subprocess
import time


def sandbox() -> str:
    repo = os.environ.get("CLAN_SANDBOX_REPO")
    if not repo:
        raise RuntimeError("CLAN_SANDBOX_REPO is not set")
    return repo


def auth_ok() -> bool:
    return subprocess.run(["gh", "auth", "status"],
                          capture_output=True, text=True).returncode == 0


def _gh(*args: str, check: bool = True) -> str:
    # `gh api` takes the repo in the endpoint path and rejects the global -R
    cmd = ["gh"] + ([] if args[0] == "api" else ["-R", sandbox()]) + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p.stdout


def create_issue(title: str, body: str) -> int:
    out = _gh("issue", "create", "--title", title, "--body", body)
    return int(out.strip().rstrip("/").split("/")[-1])


def comment(n: int, body: str) -> None:
    _gh("issue", "comment", str(n), "--body", body)


def add_labels(n: int, *labels: str) -> None:
    for label in labels:
        _gh("issue", "edit", str(n), "--add-label", label)


def issue(n: int) -> dict:
    return json.loads(_gh("issue", "view", str(n), "--json", "labels,assignees,state"))


def pr_for_branch(branch: str) -> dict | None:
    prs = json.loads(_gh("pr", "list", "--state", "all", "--head", branch,
                         "--json", "number,state,body,statusCheckRollup"))
    return prs[0] if prs else None


def wait_for_pr(branch: str, timeout_s: float = 300, poll_s: float = 10) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pr = pr_for_branch(branch)
        if pr:
            return pr
        time.sleep(poll_s)
    raise TimeoutError(f"no PR for branch {branch!r} after {timeout_s}s")


BAD_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def checks_succeeded(pr: dict) -> bool:
    """Every check finished and none failed; SKIPPED third-party checks do not
    count against the run."""
    roll = pr.get("statusCheckRollup") or []
    verdicts = [(c.get("conclusion") or c.get("state")) for c in roll]
    if not verdicts or any(v in BAD_CONCLUSIONS for v in verdicts):
        return False
    return "SUCCESS" in verdicts


def checks_completed(pr: dict) -> bool:
    roll = pr.get("statusCheckRollup") or []
    return bool(roll) and all((c.get("conclusion") or c.get("state")) is not None
                              for c in roll)


def wait_for_checks(branch: str, timeout_s: float = 600, poll_s: float = 20) -> bool:
    """Poll until every check has a verdict, then report success."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pr = pr_for_branch(branch)
        if pr and checks_completed(pr):
            return checks_succeeded(pr)
        time.sleep(poll_s)
    raise TimeoutError(f"checks for {branch!r} never completed within {timeout_s}s")


def cleanup(n: int, branch: str) -> list[str]:
    """Best-effort: close the PR, delete the remote branch, close + unassign the
    issue. Returns problems instead of raising — the caller's finaliser must
    never mask the real test failure."""
    problems = []
    pr = pr_for_branch(branch)
    if pr and pr.get("state") == "OPEN":
        _gh("pr", "close", str(pr["number"]), check=False)
    _gh("issue", "edit", str(n), "--remove-assignee", "@me", check=False)
    _gh("issue", "close", str(n), check=False)
    for _ in range(3):                            # a just-closed PR's ref can linger
        _gh("api", "-X", "DELETE", f"repos/{sandbox()}/git/refs/heads/{branch}", check=False)
        remaining = subprocess.run(["git", "ls-remote", "--heads",
                                    f"https://github.com/{sandbox()}.git", branch],
                                   capture_output=True, text=True, check=False)
        if not remaining.stdout.strip():
            return problems
        time.sleep(5)
    problems.append(f"branch {branch!r} survived cleanup")
    return problems
