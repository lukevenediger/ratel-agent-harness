"""A whole clan, on a real zellij server, with scripted agents.

One round end to end: the orchestrator is nudged, it dispatches to the reviewer,
the watcher notices the mention and types into the reviewer's pane, the reviewer
replies VERDICT: SIGN-OFF, and the watcher routes that back to the orchestrator.
No model is involved and no token is spent — the agents are playbooks — but every
other moving part is the real one.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ratel.bus import Bus
from ratel.clan import config as C
from ratel.clan import session as S

pytestmark = pytest.mark.zellij

FAKE_PRESETS = """
[presets.fake-low]
order = 1
harness = "fake"
model = "fake"
effort = "low"
label = "fake \u00b7 low"

[presets.fake-high]
order = 2
harness = "fake"
model = "fake"
effort = "high"
label = "fake \u00b7 high"
"""

ROLES_TOML = '''
[roles.orchestrator]
preset = "fake-low"
writer = false
brief = "orchestrator"
playbook = """
[[steps]]
action = "post"
text = "@reviewer review round 1"
"""

[roles.developer]
preset = "fake-low"
writer = true
brief = "developer"
playbook = """
[[steps]]
action = "post"
text = "round 1 pushed"
"""

[roles.reviewer]
preset = "fake-low"
writer = false
brief = "reviewer"
playbook = """
[[steps]]
action = "reply"
text = "VERDICT: SIGN-OFF"
"""
'''


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    work = tmp_path / "harbor"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    git(work, "config", "user.email", "clan@example.com")
    git(work, "config", "user.name", "Clan")
    (work / "README.md").write_text("hello\n")
    git(work, "add", "README.md"); git(work, "commit", "-m", "init"); git(work, "push", "-u", "origin", "main")
    git(work, "remote", "set-url", "origin", "https://github.com/acme/widget.git")
    return work


def wait_for(predicate, timeout_s=20, poll_s=0.25, what="condition"):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        got = predicate()
        if got:
            return got
        time.sleep(poll_s)
    pytest.fail(f"timed out after {timeout_s}s waiting for {what}")


def zsession(paths):
    """The zellij session `clan new` opened — the channel name plus a timestamp,
    so it is never the channel name itself."""
    return C.read_state(paths)["session"]


def test_checkpoint_types_clear_enter_then_the_reorient_nudge(home, repo, zellij_env):
    (home / "presets.toml").write_text(FAKE_PRESETS)
    (home / "roles.toml").write_text(ROLES_TOML)
    paths = C.ClanPaths(home, zellij_env.session)
    S.new(home, repo, 42, session=zellij_env.session, terminal="zellij", zellij_env=zellij_env.env)
    cfg = C.ClanConfig.read(paths, C.load_catalog(home))
    data = cfg.to_dict()
    data["roles"].update({"developer": {}, "reviewer": {}})
    C.ClanConfig.from_dict(data, C.load_catalog(home)).write(paths)
    S.up(paths)

    rec = S.checkpoint(paths, "developer", force=True)   # the fake never posts: force
    assert rec["mode"] == "clear" and rec["reason"] == "manual"

    log = paths.harness_dir("developer") / "nudges.log"
    wait_for(lambda: "fresh context after checkpoint" in (log.read_text()
               if log.exists() else ""), what="the re-orient nudge")
    lines = log.read_text().splitlines()
    assert lines[0] == "/clear"                          # the command, then its Enter,
    assert "fresh context after checkpoint" in lines[1]  # then the re-orient nudge
    assert "catch_up" in lines[1]


def test_one_round_through_real_tabs(home, repo, zellij_env):
    (home / "presets.toml").write_text(FAKE_PRESETS)
    (home / "roles.toml").write_text(ROLES_TOML)
    live_before = zellij_env.live_sessions()
    paths = C.ClanPaths(home, zellij_env.session)

    out = S.new(home, repo, 42, session=zellij_env.session, terminal="zellij", zellij_env=zellij_env.env)
    assert out["channel"] == zellij_env.session
    assert out["session"].startswith(f"{zellij_env.session}-")   # channel + timestamp
    assert out["attach"] == f"zellij attach {out['session']}"

    z = zellij_env.zellij(zsession(paths))
    assert z.tab_names() == ["orchestrator", "watch", "bus"]   # the default Tab #1 is gone

    cfg = C.ClanConfig.read(paths, C.load_catalog(home))
    data = cfg.to_dict()
    data["roles"].update({"developer": {}, "reviewer": {}})
    C.ClanConfig.from_dict(data, C.load_catalog(home)).write(paths)
    assert set(S.up(paths)["started"]) == {"developer", "reviewer"}

    z = zellij_env.zellij(zsession(paths))
    assert z.tab_names() == ["orchestrator", "watch", "bus", "developer", "reviewer"]

    state = C.read_state(paths)
    live_panes = {p["id"] for p in z.list_panes() if not p.get("is_plugin")}
    for role in ("orchestrator", "developer", "reviewer"):
        assert state["tabs"][role]["pane_id"] in live_panes, role

    watcher = subprocess.Popen(
        [sys.executable, "-m", "ratel.cli", "clan", "watch", "--home", str(home),
         "--channel", zellij_env.session],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        env={**os.environ, "RATEL_HOME": str(home)})
    try:
        wait_for(lambda: "watch" in (C.read_state(paths) or {}), what="the watcher to seed")
        bus = Bus(home, zellij_env.session)

        S.nudge(paths, "orchestrator")                       # the human kicks the round off
        wait_for(lambda: [m for m in bus.read_all() if m["from"] == "orchestrator"],
                 what="the orchestrator to dispatch")

        reply = wait_for(lambda: next((m for m in bus.read_all()
                                       if m["from"] == "reviewer" and "VERDICT:" in m["text"]), None),
                         what="the reviewer's VERDICT")
        assert reply["text"].startswith("VERDICT: SIGN-OFF")

        log = paths.harness_dir("orchestrator") / "nudges.log"
        wait_for(lambda: log.exists() and "VERDICT: SIGN-OFF" in log.read_text(),
                 what="the VERDICT nudge to reach the orchestrator's pane")
        assert 'posted "VERDICT: SIGN-OFF"' in log.read_text()

        rev_log = (paths.harness_dir("reviewer") / "nudges.log").read_text()
        assert "ratel: @reviewer 1 new on #" in rev_log     # a real keystroke nudge, not a poll

        cursors = {p["agent"] for p in bus.presence()}
        assert cursors <= {"orchestrator", "developer", "reviewer"}
        assert "watch" not in cursors                          # the watcher owns no cursor
    finally:
        watcher.terminate()
        watcher.wait(timeout=10)

    wt = repo.parent / "harbor-wt"
    assert (wt / "42-developer").exists()
    removed = S.down(paths, prune_worktrees=True)
    assert sorted(Path(p).name for p in removed["worktrees_removed"]) == ["42-developer", "42-reviewer"]
    assert not (wt / "42-developer").exists() and not (wt / "42-reviewer").exists()
    gone = zellij_env.zellij(zsession(paths))
    assert gone.session not in gone.sessions()
    assert zellij_env.live_sessions() == live_before          # the user's server never saw us


import http.client
import threading

from ratel import board as board_mod

PROPOSAL_ROLES = [
    {"name": "developer", "preset": "fake-low",
     "writer": True, "skills": [], "why": "writes"},
    {"name": "reviewer", "preset": "fake-low",
     "writer": False, "skills": [], "why": "checks"},
]


def test_propose_board_approval_approve_up_round_through_real_tabs(home, repo,
                                                                   zellij_env):
    """The whole clan gate through real tabs: the fake orchestrator proposes
    (kicked off by the first nudge, like a human kicking off a round), the
    operator approves on the real board (HTTP POST with the token, roles
    edited), the watcher's nudge drives the orchestrator's `approve` step,
    `clan up` launches exactly the approved roles, and the round runs to
    VERDICT: SIGN-OFF."""
    proposal_file = home / "proposal.json"
    proposal_file.write_text(json.dumps({"roles": PROPOSAL_ROLES}))
    roles_toml = """
[roles.orchestrator]
preset = "fake-low"
writer = false
brief = "orchestrator"
playbook = \'\'\'
[[steps]]
action = "propose"
file = "__PROPOSAL_FILE__"

[[steps]]
action = "approve"

[[steps]]
action = "post"
text = "@reviewer review round 1"
\'\'\'

[roles.reviewer]
preset = "fake-low"
writer = false
brief = "reviewer"
playbook = \'\'\'
[[steps]]
action = "reply"
text = "VERDICT: SIGN-OFF"
\'\'\'
""".replace("__PROPOSAL_FILE__", str(proposal_file))
    (home / "presets.toml").write_text(FAKE_PRESETS)
    (home / "roles.toml").write_text(roles_toml)
    live_before = zellij_env.live_sessions()
    paths = C.ClanPaths(home, zellij_env.session)

    out = S.new(home, repo, 42, session=zellij_env.session, terminal="zellij", zellij_env=zellij_env.env)
    assert out["channel"] == zellij_env.session
    assert out["session"].startswith(f"{zellij_env.session}-")   # channel + timestamp
    assert out["attach"] == f"zellij attach {out['session']}"

    srv = board_mod.make_server(home, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    watcher = None
    bus = Bus(home, zellij_env.session)
    try:
        token = board_mod.board_token(home)

        # the human kicks the round off; the orchestrator's first step proposes
        S.nudge(paths, "orchestrator")
        proposal_msg = wait_for(lambda: next((m for m in bus.read_all()
                                              if m["from"] == "orchestrator"
                                              and any(a.get("type") == "clan"
                                                      and a.get("status") == "proposed"
                                                      for a in m.get("attachments", []))), None),
                                what="the orchestrator's proposal")
        proposal_json = json.loads((paths.clan_toml.parent / "proposal.json").read_text())
        assert proposal_json["status"] == "proposed"

        # the operator approves on the REAL board: token + HTTP POST, with an edit
        roles_edited = [dict(r) for r in PROPOSAL_ROLES]
        roles_edited[0]["preset"] = "fake-high"         # the operator's edit
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        body = json.dumps({"text": "@orchestrator clan approved",
                        "parent": proposal_msg["id"],
                        "attachments": [{"type": "clan", "status": "approved", "issue": 42,
                                         "roles": roles_edited,
                                         "supersedes": proposal_msg["id"]}]})
        conn.request("POST", f"/api/channels/{zellij_env.session}/post", body=body,
                     headers={"Authorization": "Bearer " + token,
                              "Content-Type": "application/json",
                              "Content-Length": str(len(body))})
        resp = conn.getresponse()
        assert resp.status == 201, resp.read()
        conn.close()

        # the watcher nudges the orchestrator; step 2 lands the approved clan
        watcher = subprocess.Popen(
            [sys.executable, "-m", "ratel.cli", "clan", "watch", "--home", str(home),
             "--channel", zellij_env.session],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            env={**os.environ, "RATEL_HOME": str(home)})
        wait_for(lambda: "watch" in (C.read_state(paths) or {}), what="the watcher to seed")
        wait_for(lambda: "developer" in C.ClanConfig.read(paths, C.load_catalog(home)).roles,
                 what="clan approve to write clan.toml")
        cfg = C.ClanConfig.read(paths, C.load_catalog(home))
        assert set(cfg.roles) == {"orchestrator", "developer", "reviewer"}
        assert cfg.roles["developer"].effort == "high"          # the POSTed edit landed
        assert cfg.roles["developer"].writer is True
        assert C.read_state(paths)["writers"] == {"orchestrator": False,
                                                  "developer": True, "reviewer": False}
        orch_log = paths.harness_dir("orchestrator") / "nudges.log"
        wait_for(lambda: orch_log.exists() and "from stakeholder" in orch_log.read_text(),
                 what="the approval nudge to reach the orchestrator's pane")
    finally:
        if watcher:
            watcher.terminate()
            watcher.wait(timeout=10)
        srv.stop.set()
        srv.shutdown()

    # `clan up` launches exactly the approved roles
    assert set(S.up(paths)["started"]) == {"developer", "reviewer"}
    z = zellij_env.zellij(zsession(paths))
    assert z.tab_names() == ["orchestrator", "watch", "bus", "developer", "reviewer"]

    # the existing round: dispatch -> reviewer VERDICT -> routed back
    watcher = subprocess.Popen(
        [sys.executable, "-m", "ratel.cli", "clan", "watch", "--home", str(home),
         "--channel", zellij_env.session],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        env={**os.environ, "RATEL_HOME": str(home)})
    try:
        wait_for(lambda: "watch" in (C.read_state(paths) or {}), what="the watcher to seed")
        S.nudge(paths, "orchestrator")                  # step 3: dispatch the reviewer
        reply = wait_for(lambda: next((m for m in bus.read_all()
                                       if m["from"] == "reviewer" and "VERDICT:" in m["text"]), None),
                         what="the reviewer's VERDICT")
        assert reply["text"].startswith("VERDICT: SIGN-OFF")
        log = paths.harness_dir("orchestrator") / "nudges.log"
        wait_for(lambda: log.exists() and "VERDICT: SIGN-OFF" in log.read_text(),
                 what="the VERDICT nudge to reach the orchestrator's pane")
    finally:
        watcher.terminate()
        watcher.wait(timeout=10)

    removed = S.down(paths, prune_worktrees=True)
    assert sorted(Path(p).name for p in removed["worktrees_removed"]) == \
        ["42-developer", "42-reviewer"]
    gone = zellij_env.zellij(zsession(paths))
    assert gone.session not in gone.sessions()
    assert zellij_env.live_sessions() == live_before
