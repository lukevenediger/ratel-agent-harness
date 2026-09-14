"""Tier 2: a real clan, on the real clan sandbox, with real models.

Opt in with `-m llm`; deselected by default. One case = one issue on
`CLAN_SANDBOX_REPO`, one nested clan (its own zellij server, its own
RATEL_HOME) driven headless, and assertions read off the bus and `gh` when
the session tears itself down. The sandbox is left clean either way.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import pytest

from ratel.bus import Bus
from ratel.clan import harness as clan_harness
from ratel.clan.zellij import Zellij, isolated_env

from . import gh

pytestmark = pytest.mark.llm

CASES = sorted(p for p in (Path(__file__).parent / "cases").iterdir() if p.is_dir())
POLL_S = 30


def _issue_body(case: Path) -> str:
    """The whole plan, as the issue body — the issue is the plan."""
    text = (case / "plan.md").read_text()
    return re.search(r"<!--\s*issue\n(.*?)-->", text, re.S).group(1)




@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_clan_runs_the_case(case, tmp_path):
    if not os.environ.get("CLAN_SANDBOX_REPO"):
        pytest.skip("CLAN_SANDBOX_REPO is not set")
    # once the sandbox is selected, every remaining precondition miss is an
    # error, not a skip: a run that silently skips here would be read as a
    # green verification on exactly the machine state that motivated this.
    token = os.environ.get("CLAN_SANDBOX_TOKEN")
    if not token:
        pytest.fail("CLAN_SANDBOX_TOKEN (a fine-grained PAT scoped to the sandbox "
                    "repo) is not set — the nested clan must not run on the "
                    "operator's own gh credential")
    if not gh.auth_ok():
        pytest.fail("gh is not authenticated — the test cannot clone or read the sandbox")
    expect = tomllib.loads((case / "expect.toml").read_text())
    issue_body = _issue_body(case)

    home = tmp_path / "home"
    home.mkdir()
    # zellij sockets cap at 103 bytes: the server's TMPDIR must stay short
    # (see tests/conftest.py ZellijSandbox) — pytest's tmp_path is too deep.
    ztmp = Path(tempfile.mkdtemp(prefix="zj", dir="/tmp"))
    sandbox = tmp_path / "sandbox"
    subprocess.run(["gh", "repo", "clone", gh.sandbox(), str(sandbox), "--", "--depth", "1"],
                   check=True, capture_output=True)

    n = gh.create_issue(f"e2e {case.name} (pid {os.getpid()})", issue_body)
    gh.add_labels(n, "lane:build", "p2")
    branch = expect["pr_branch"].format(n=n)
    channel = f"clan-sandbox-{n}"
    # Credentials, by env only: the nested clan gets the sandbox-scoped token,
    # never the operator's own gh credential. HOME stays real (see
    # isolated_env) and CLAUDE_CONFIG_DIR is dropped so the nested claude finds
    # the operator's real login — the autouse conftest fixture must not leak
    # into a Tier 2 clan. No secret is copied or written anywhere.
    env = {**os.environ, "RATEL_HOME": str(home), "CLAN_UNATTENDED": "1",
           "GH_TOKEN": token}
    env.pop("CLAUDE_CONFIG_DIR", None)

    def cli(*args: str, check=True) -> str:
        p = subprocess.run([sys.executable, "-m", "ratel.cli", "clan", *args],
                           env=env, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise RuntimeError(f"ratel clan {' '.join(args)}: {p.stderr.strip()}")
        return p.stdout

    z = Zellij(channel, env=isolated_env(ztmp))
    pr = None
    error: AssertionError | None = None
    try:
        cli("new", str(sandbox), str(n), "--unattended", "--zellij-tmp", str(ztmp),
            "--home", str(home), "--session", channel)
        deadline = time.monotonic() + expect["timeout_s"]
        while time.monotonic() < deadline:
            if channel not in z.sessions():
                break                                   # the clan tore itself down
            time.sleep(POLL_S)
        pr = gh.wait_for_pr(branch, timeout_s=120, poll_s=15)
        assert_expectations(expect, home, channel, n, pr, branch, z)
    except AssertionError as exc:
        error = exc
        raise
    finally:
        problems = []
        for step in (
                lambda: channel in z.sessions() and cli("down", "--home", str(home), "--channel",
                                           channel, "--prune-worktrees", check=False),
                lambda: gh.cleanup(n, branch),
                lambda: clan_harness.untrust(tmp_path),
                lambda: shutil.rmtree(ztmp, ignore_errors=True)):
            try:
                step()
            except Exception as exc:                # cleanup must never mask the real failure
                problems.append(f"{type(exc).__name__}: {exc}")
        if error is not None:
            print(f"\nchannel.sqlite3: {home / 'channels' / channel / 'channel.sqlite3'}")
            print(f"issue:     https://github.com/{gh.sandbox()}/issues/{n}")
            print(f"PR:        {pr and 'https://github.com/%s/pull/%s' % (gh.sandbox(), pr['number'])}")
        for problem in problems:
            print(f"CLEANUP PROBLEM (not the test's failure): {problem}")


def assert_expectations(expect: dict, home: Path, channel: str, n: int, pr: dict,
                        branch: str, z: Zellij) -> None:
    """The clan's own teardown happened, the issue was released, the work is there."""
    channel_dir = home / "channels" / channel
    assert channel not in z.sessions(), "the clan never ran `clan down`"
    st = gh.issue(n)
    if expect["no_assignee"]:
        assert st["assignees"] == []
    assert gh.wait_for_checks(branch), "the PR's checks did not all succeed"

    msgs = Bus(home, channel).read_all()
    # pins = substring match over pinned message text, attachment names/refs,
    # and the content of pinned file attachments (refs are channel-dir relative)
    parts = []
    for m in msgs:
        if not m.get("pin"):
            continue
        parts.append(m.get("text") or "")
        for a in (m.get("attachments") or []):
            parts.append(f"{a.get('name')} {a.get('ref')}")
            ref = a.get("ref")
            if a.get("type") == "file" and ref and ".." not in ref:
                f = channel_dir / ref
                if f.exists():
                    parts.append(f.read_text(errors="replace"))
    haystack = "\n".join(parts).lower()
    for needle in expect["pins"]:
        assert needle.lower() in haystack, f"no pinned message mentions {needle!r}"
    for role in expect["verdict_roles"]:
        assert any(m["from"] == role and "VERDICT: SIGN-OFF" in m.get("text", "")
                   for m in msgs), f"no SIGN-OFF from {role}"
