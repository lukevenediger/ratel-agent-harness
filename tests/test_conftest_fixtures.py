"""The teardown fixtures themselves — no real harness binaries needed."""
import os
import subprocess
import time

import pytest

from tests import conftest as cf

ZELLIJ_STUB = "/usr/bin/zellij"


def test_an_ambient_agent_name_is_stripped(monkeypatch):
    """The suite runs from inside clan roles, whose harness exports AGENT_NAME
    (`launch()` sets it). The checkpoint self-guard keys off it (Decision 38),
    so a leaked identity fails every test that checkpoints that same role — the
    seven `test_clan_cli.py` checkpoint tests under `AGENT_NAME=orchestrator`,
    the clear-mode ones under `AGENT_NAME=developer`. No test inherits one."""
    monkeypatch.setenv("AGENT_NAME", "orchestrator")
    cf.strip_ambient_agent_name(monkeypatch)
    assert "AGENT_NAME" not in os.environ


def test_no_stray_sessions_deletes_and_fails_naming_the_armed_stray(monkeypatch):
    monkeypatch.setattr(cf.shutil, "which", lambda name: ZELLIJ_STUB)
    monkeypatch.setattr(cf, "live_zellij_sessions", lambda: ["base"])
    deleted = []
    monkeypatch.setattr(cf.subprocess, "run",
                        lambda argv, **kw: deleted.append(argv)
                        or subprocess.CompletedProcess(argv, 0))
    gen = cf.no_stray_sessions_impl({"smoke"})
    next(gen)                                   # fixture setup
    monkeypatch.setattr(cf, "live_zellij_sessions",
                        lambda: ["base", "smoke", "stray-1"])
    with pytest.warns(UserWarning, match="unrelated zellij session"):   # stray-1: warn only
        with pytest.raises(pytest.fail.Exception) as e:
            next(gen)                           # the finalizer
    assert "smoke" in str(e.value) and "stray-1" not in str(e.value)
    assert deleted == [["zellij", "delete-session", "--force", "smoke"]]


def test_no_stray_sessions_never_touches_a_session_it_did_not_arm(monkeypatch):
    """Concurrent clans are real: a session that appears but was not armed is
    warned about, not deleted, and does not fail the test."""
    monkeypatch.setattr(cf.shutil, "which", lambda name: ZELLIJ_STUB)
    monkeypatch.setattr(cf, "live_zellij_sessions", lambda: ["base"])
    deleted = []
    monkeypatch.setattr(cf.subprocess, "run",
                        lambda argv, **kw: deleted.append(argv)
                        or subprocess.CompletedProcess(argv, 0))
    gen = cf.no_stray_sessions_impl(set())
    next(gen)
    monkeypatch.setattr(cf, "live_zellij_sessions", lambda: ["base", "someone-else"])
    with pytest.warns(UserWarning, match="someone-else"):
        with pytest.raises(StopIteration):
            next(gen)                           # teardown: warn, never delete
    assert deleted == []


def test_no_stray_sessions_fails_without_deleting_when_the_listing_fails(monkeypatch):
    """A failed `zellij list-sessions` must never be read as an empty server:
    that bug would delete every live session on the machine."""
    monkeypatch.setattr(cf.shutil, "which", lambda name: ZELLIJ_STUB)
    monkeypatch.setattr(cf, "live_zellij_sessions", lambda: ["base"])
    deleted = []
    monkeypatch.setattr(cf.subprocess, "run",
                        lambda argv, **kw: deleted.append(argv)
                        or subprocess.CompletedProcess(argv, 0))
    gen = cf.no_stray_sessions_impl({"smoke"})
    next(gen)
    monkeypatch.setattr(cf, "live_zellij_sessions", lambda: None)
    with pytest.raises(pytest.fail.Exception) as e:
        next(gen)
    assert "could not verify" in str(e.value)
    assert deleted == []


def test_no_stray_sessions_yields_cleanly_when_nothing_appeared(monkeypatch):
    monkeypatch.setattr(cf.shutil, "which", lambda name: ZELLIJ_STUB)
    monkeypatch.setattr(cf, "live_zellij_sessions", lambda: ["base"])
    gen = cf.no_stray_sessions_impl({"smoke"})
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)                               # teardown: nothing new, no failure


def test_no_stray_sessions_skips_cleanly_without_the_binary(monkeypatch):
    monkeypatch.setattr(cf.shutil, "which", lambda name: None)
    gen = cf.no_stray_sessions_impl({"smoke"})
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)


def test_reaped_subprocesses_killpgs_the_group_on_teardown():
    gen = cf.reaped_subprocesses_impl()
    run = next(gen)
    p = run(["sh", "-c", "echo $$; sleep 30 >/dev/null 2>&1 &"],
            stdout=subprocess.PIPE, text=True)
    pgid = int(p.stdout.strip())                # sh is its own group leader
    os.killpg(pgid, 0)                          # the group is alive before teardown
    with pytest.raises(StopIteration):
        next(gen)                               # teardown killpgs whatever survives
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        # On macOS, killpg(..., 0) can report EPERM for a dying orphan group.
        # Inspect members instead: zombies have exited and cannot leak work.
        listing = subprocess.run(["ps", "-axo", "pgid=,stat="],
                                 capture_output=True, text=True, check=True)
        live = [line for line in listing.stdout.splitlines()
                if len(fields := line.split()) == 2
                and fields[0] == str(pgid) and not fields[1].startswith("Z")]
        if not live:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"live processes survived group teardown: {live}")


def test_live_zellij_sessions_returns_none_when_the_listing_fails(tmp_path, monkeypatch):
    """The source-level half of the blocker: a non-zero `list-sessions` must
    never be read as an empty server. A PATH-shadowed stub proves the helper,
    no server needed."""
    stub = tmp_path / "zellij"
    stub.write_text('#!/bin/sh\necho "No active zellij sessions found." >&2\nexit 1\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert cf.live_zellij_sessions() is None


def test_reaped_subprocesses_kills_the_group_then_reraises_on_timeout(tmp_path):
    gen = cf.reaped_subprocesses_impl()
    run = next(gen)
    pidfile = tmp_path / "pgid"
    start = time.monotonic()                    # the bound makes the kill load-bearing:
    with pytest.raises(subprocess.TimeoutExpired):   # without killpg, communicate() waits
        run(["sh", "-c", f"echo $$ > {pidfile}; sleep 30 >/dev/null 2>&1"],   # out the sleep
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=0.2)
    assert time.monotonic() - start < 5         # a 30s sleep returned in well under 5s
    pgid = int(pidfile.read_text())             # the victim was its own group leader
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break                               # killed before the re-raise
        time.sleep(0.05)
    else:
        pytest.fail("the timed-out group survived the kill before the re-raise")
    with pytest.raises(StopIteration):
        next(gen)


def test_the_zellij_sandbox_only_ever_deletes_its_own_sessions(monkeypatch, tmp_path):
    """`list-sessions` on the sandbox's server is NOT the sandbox's alone: HOME
    stays real, so ~/.cache/zellij is shared and the operator's own exited
    sessions are in the listing. Deleting one destroys its resurrect data."""
    box = cf.ZellijSandbox()
    listing = "\n".join([box.session, f"{box.session}-0910-1423",
                         "example-site-1", "oblong-pheasant"])
    monkeypatch.setattr(cf.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, listing, ""))
    assert box.own_sessions() == sorted([box.session, f"{box.session}-0910-1423"])


def test_the_zellij_sandbox_falls_back_to_its_own_name_when_the_listing_fails(monkeypatch):
    box = cf.ZellijSandbox()
    monkeypatch.setattr(cf.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom"))
    assert box.own_sessions() == [box.session]
