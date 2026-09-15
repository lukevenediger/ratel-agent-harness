"""Backend contracts, delivery safety, and optional real HerdR smoke tests."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ratel.bus import Bus
from ratel.clan import session as S
from ratel.clan.config import ClanConfig, ClanPaths, load_catalog, read_state, update_state
from ratel.clan.herdr import Herdr
from ratel.clan.terminal import backend_name, preference, terminal_id
from ratel.clan.watch import Watcher, queue_control
from tests.test_clan_zellij_integration import FAKE_PRESETS, ROLES_TOML
from tests.test_clan_zellij_integration import repo as repo


def test_preference_and_legacy_binding(home):
    assert preference(home) == "herdr"
    (home / "config.toml").write_text('[terminal]\nbackend = "zellij"\n')
    assert preference(home) == "zellij"
    assert preference(home, "herdr") == "herdr"
    assert backend_name({}) == "zellij"
    assert backend_name({"terminal_backend": "herdr"}) == "herdr"
    with pytest.raises(ValueError):
        preference(home, "auto")
    assert terminal_id("w1:p2") == "w1:p2"
    assert terminal_id(4) == 4
    assert terminal_id(True) is None


class Aware:
    agent_aware = True

    def __init__(self):
        self.status = "working"
        self.sent = []

    def observe(self, pane):
        from ratel.bus import now_iso
        return {"state": self.status, "at": now_iso(), "source": "herdr"}

    def nudge(self, pane, text):
        if self.status not in ("idle", "done"):
            raise ValueError("not ready")
        self.sent.append((pane, text))


@pytest.mark.parametrize("status", ["working", "blocked", "unknown", "unavailable"])
def test_busy_messages_and_verdicts_survive_restart(home, status):
    paths = ClanPaths(home, "test")
    bus = Bus(home, "test")
    update_state(paths, lambda s: s.update(tabs={"orchestrator": {"pane_id": "w1:p1"}}))
    adapter = Aware()
    adapter.status = status
    w = Watcher(bus, paths, adapter, debounce_s=0)
    w.seed()
    bus.post("reviewer", "@orchestrator VERDICT pending")
    bus.post("reviewer", "VERDICT: SIGN-OFF")
    w.once()
    assert adapter.sent == []
    assert read_state(paths)["terminal_controls"]
    adapter.status = "idle"
    w = Watcher(bus, paths, adapter, debounce_s=0)
    w.once()
    w.once()
    assert len(adapter.sent) == 2
    assert any('"VERDICT: SIGN-OFF"' in text for _, text in adapter.sent)
    assert not read_state(paths)["terminal_controls"]
    assert not read_state(paths)["watch"]["pending"]


def test_manual_control_is_not_lost_when_watcher_updates(home):
    paths = ClanPaths(home, "test")
    bus = Bus(home, "test")
    update_state(paths, lambda s: s.update(tabs={"developer": {"pane_id": "w1:p1"}}))
    adapter = Aware()
    watcher = Watcher(bus, paths, adapter)
    watcher.seed()
    queue_control(paths, "developer", "read the plan")
    watcher.once()
    adapter.status = "done"
    watcher.once()
    assert adapter.sent == [("w1:p1", "read the plan")]


def test_cli_handles_empty_success_and_rejects_error(home, monkeypatch):
    monkeypatch.setattr("ratel.clan.herdr.require_binary", lambda *a: None)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    h = Herdr(ClanPaths(home, "test"), {}, run=run)
    assert h.call("pane", "send-text", "w1:p1", "hello") == {}
    assert calls[0][:3] == ["herdr", "--session", h.session]
    h.run = lambda *a, **kw: SimpleNamespace(returncode=0, stdout='{"error":{}}', stderr="")
    with pytest.raises(ValueError):
        h.call("pane", "list")


def test_replaced_process_never_receives_input(home, monkeypatch):
    paths = ClanPaths(home, "test")
    update_state(paths, lambda s: s.update(tabs={"developer": {
        "pane_id": "w1:p1", "terminal_id": "term_original", "launch_pid": 42,
        "launch_started": "then", "harness": "claude"}}))
    monkeypatch.setattr("ratel.clan.harness._ps_lstart", lambda pid: "now")
    h = Herdr(paths, {})
    calls = []
    def call(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("pane", "list"):
            return {"panes": [{"terminal_id": "term_original", "pane_id": "w2:p3"}]}
        return {"process_info": {"foreground_processes": [{"pid": 42}]}}
    h.call = call
    with pytest.raises(ValueError):
        h.nudge("w1:p1", "must not reach shell")
    assert all(c[:2] not in [("pane", "send-text"), ("agent", "prompt")] for c in calls)


def wait_for(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.2)
    raise AssertionError("timed out waiting for HerdR condition")


@pytest.fixture
def real_herdr(home, monkeypatch):
    binary = os.environ.get("HERDR_TEST_BINARY") or shutil.which("herdr")
    if not binary:
        pytest.skip("herdr binary not installed; set HERDR_TEST_BINARY")
    bindir = home / "bin"
    bindir.mkdir()
    (bindir / "herdr").symlink_to(Path(binary).resolve())
    monkeypatch.setenv("PATH", f"{bindir}:{Path(sys.executable).parent}:{os.environ['PATH']}")
    (home / "presets.toml").write_text(FAKE_PRESETS)
    (home / "roles.toml").write_text(ROLES_TOML)
    h = Herdr(ClanPaths(home, "unused"), {})
    try:
        yield h
    finally:
        subprocess.run(h.command("server", "stop"), env=h.env, capture_output=True, timeout=10)


@pytest.mark.herdr
def test_real_two_clans_delivery_moves_and_shutdown(home, repo, real_herdr):
    one = S.new(home, repo, 42, session="one")
    two = S.new(home, repo, 43, session="two")
    assert one["session"] == two["session"]
    assert one["workspace_id"] != two["workspace_id"]
    p1, p2 = ClanPaths(home, "one"), ClanPaths(home, "two")
    cfg = ClanConfig.read(p1, load_catalog(home)).to_dict()
    cfg["roles"].update(developer={}, reviewer={})
    ClanConfig.from_dict(cfg, load_catalog(home)).write(p1)
    assert set(S.up(p1)["started"]) == {"developer", "reviewer"}
    h1, h2 = Herdr(p1), Herdr(p2)
    pane1 = read_state(p1)["tabs"]["orchestrator"]["pane_id"]
    pane2 = read_state(p2)["tabs"]["orchestrator"]["pane_id"]
    wait_for(lambda: h1.observe(pane1)["state"] == "idle")
    wait_for(lambda: h2.observe(pane2)["state"] == "idle")
    S.nudge(p1, "orchestrator", "run the first step")
    wait_for(lambda: Bus(home, "one").read_all())
    wait_for(lambda: any(m["text"] == "VERDICT: SIGN-OFF" for m in Bus(home, "one").read_all()))
    assert not Bus(home, "two").read_all()
    # Move an owned pane out of its workspace; its immutable terminal identity follows it.
    h1.call("pane", "move", pane1, "--new-workspace", "--label", "moved")
    assert h1.observe(pane1)["state"] == "idle"
    assert S.status(p1)["roles"][0]["terminal"] is not None
    S.down(p1)
    assert h2.observe(pane2)["state"] == "idle"
    # A cold restart restores shells, never launches or pending prompts.
    h2.call("server", "stop")
    h2.ensure_server()
    assert h2.observe(pane2)["state"] == "unavailable"
    with pytest.raises(ValueError):
        h2.nudge(pane2, "must not reach the restored shell")
    S.down(p2)


def test_missing_herdr_does_not_create_a_clan(home, repo, monkeypatch):
    monkeypatch.setattr("ratel.clan.tools.shutil.which", lambda binary: None)
    with pytest.raises(SystemExit, match="terminal zellij"):
        S.new(home, repo, 42)
    assert not (home / "channels").exists()


def test_observation_expiry_and_opaque_ids():
    from datetime import datetime, timezone
    state = {"terminal_backend": "herdr", "watch": {"terminal": {
        "developer": {"state": "done", "at": "2026-09-15T12:00:00Z"}}}}
    now = datetime(2026, 9, 15, 12, 0, 5, tzinfo=timezone.utc)
    assert S.terminal_observation(state, "developer", now)["stale"] is False
    now = now.replace(second=11)
    assert S.terminal_observation(state, "developer", now)["stale"] is True
    assert S.terminal_observation(state, "absent", now)["stale"] is True


def test_manual_agent_readiness_does_not_require_herdr_managed_launch(home):
    paths = ClanPaths(home, "test")
    h = Herdr(paths, {})
    h.resolve = lambda pane: (pane, {"harness": "claude"})
    calls = []
    def call(*args, **kw):
        calls.append(args)
        return {"agent": {"agent": "claude", "agent_status": "idle"}}
    h.call = call
    assert h.observe("w1:p1")["state"] == "idle"
    assert calls == [("agent", "get", "w1:p1")]
    h.nudge("w1:p1", "hello")
    assert calls[-1] == ("agent", "prompt", "w1:p1", "hello")


@pytest.mark.herdr
@pytest.mark.parametrize("kind", ["claude", "opencode"])
def test_native_agent_detection_without_model_turn(home, repo, real_herdr, kind):
    """Opt-in startup only: no prompt, model request, or tool execution."""
    if os.environ.get("HERDR_NATIVE_SMOKE") != "1":
        pytest.skip("set HERDR_NATIVE_SMOKE=1 for installed agent startup checks")
    binary = os.environ.get(f"HERDR_TEST_{kind.upper()}") or shutil.which(kind)
    if not binary:
        pytest.fail(f"native smoke requested but {kind} is missing")
    paths = ClanPaths(home, "native")
    h = Herdr(paths, {})
    h.create_background(repo)
    update_state(paths, lambda s: s.update(terminal_backend="herdr", session=h.session,
                                          workspace_id=h.workspace, tabs={}))
    code = ("import os; from ratel.clan.harness import record_launched; "
            "from ratel.clan.config import ClanPaths; "
            f"record_launched(ClanPaths({str(home)!r}, 'native'), 'orchestrator'); "
            f"os.execv({binary!r}, [{binary!r}])")
    h.new_tab("orchestrator", repo, [sys.executable, "-c", code])
    update_state(paths, lambda s: s["tabs"]["orchestrator"].update(harness=kind))
    pane = read_state(paths)["tabs"]["orchestrator"]["pane_id"]
    def detected():
        try:
            return h.call("agent", "get", pane).get("agent", {}).get("agent") == kind
        except ValueError:
            return False
    wait_for(detected, seconds=30)
    assert h.observe(pane)["state"] in ("idle", "done", "blocked", "working", "unknown")
    h.kill()
