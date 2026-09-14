"""The scripted stand-in for a real agent: one playbook step per nudge line.

Tier 1 integration tests drive real zellij tabs running this, so it has to
behave like an agent from the channel's point of view — and be driven the same
way a real one is, by lines arriving on stdin.
"""
import json
import subprocess
import sys

from ratel.bus import Bus
from ratel.clan.loop import NudgeLoop


def run_fake(home, agent, playbook, lines, channel="harbor-42", log=None, timeout=30):
    env = {"RATEL_HOME": str(home), "AGENT_NAME": agent, "CHANNEL": channel,
           "PATH": "/usr/bin:/bin"}
    argv = [sys.executable, "-m", "ratel.clan.fake_harness", "--playbook", str(playbook)]
    if log:
        argv += ["--log", str(log)]
    p = subprocess.run(argv, input="".join(line + "\n" for line in lines), capture_output=True,
                       text=True, env=env, timeout=timeout)
    assert p.returncode == 0, p.stderr
    return p


def playbook(tmp_path, name, body):
    p = tmp_path / f"{name}.toml"
    p.write_text(body)
    return p


# ---- the loop itself ---------------------------------------------------
def test_nudge_loop_calls_the_runner_once_per_line(tmp_path):
    seen, log = [], tmp_path / "log.txt"
    import io
    NudgeLoop(seen.append, stdin=io.StringIO("one\ntwo\n\nthree\n"), log=log).run()
    assert seen == ["one", "two", "three"]          # blank lines are not nudges
    assert log.read_text().splitlines() == ["one", "two", "three"]


def test_nudge_loop_keeps_going_when_the_runner_raises(tmp_path):
    seen = []
    def runner(line):
        seen.append(line)
        raise RuntimeError("step blew up")
    import io
    NudgeLoop(runner, stdin=io.StringIO("a\nb\n"), log=tmp_path / "l.txt").run()
    assert seen == ["a", "b"]                        # a bad step must not kill the agent


# ---- the fake harness --------------------------------------------------
def test_one_step_per_nudge_line(home, tmp_path):
    pb = playbook(tmp_path, "dev", '''
[[steps]]
action = "post"
text = "round one done"

[[steps]]
action = "post"
text = "round two done"
''')
    run_fake(home, "developer", pb, ["nudge 1", "nudge 2"])
    bus = Bus(home, "harbor-42")
    assert [m["text"] for m in bus.read_all()] == ["round one done", "round two done"]


def test_steps_run_only_as_far_as_the_nudges(home, tmp_path):
    pb = playbook(tmp_path, "dev", '''
[[steps]]
action = "post"
text = "first"

[[steps]]
action = "post"
text = "second"
''')
    run_fake(home, "developer", pb, ["only one nudge"])
    assert [m["text"] for m in Bus(home, "harbor-42").read_all()] == ["first"]


def test_extra_nudges_past_the_end_of_the_playbook_are_harmless(home, tmp_path):
    pb = playbook(tmp_path, "dev", '[[steps]]\naction = "post"\ntext = "only step"\n')
    run_fake(home, "developer", pb, ["a", "b", "c"])
    assert [m["text"] for m in Bus(home, "harbor-42").read_all()] == ["only step"]


def test_verdict_reply_lands_in_the_thread_of_the_newest_mention(home, tmp_path):
    bus = Bus(home, "harbor-42")
    top = bus.post("orchestrator", "round 1 kickoff")
    dispatch = bus.post("orchestrator", "@reviewer review round 1", parent=top["id"])
    pb = playbook(tmp_path, "rev", '''
[[steps]]
action = "reply"
text = """VERDICT: SIGN-OFF

1. minor — naming in config.py:12; evidence: reads fine; fix: none needed."""
''')
    run_fake(home, "reviewer", pb, ["ratel: @reviewer 1 new — call catch_up now."])
    posted = [m for m in bus.read_all() if m["from"] == "reviewer"]
    assert len(posted) == 1
    assert posted[0]["text"].startswith("VERDICT: SIGN-OFF")
    assert posted[0]["parent"] == top["id"]         # the thread, not the dispatch message
    assert dispatch["id"] != posted[0]["parent"]


def test_reply_falls_back_to_a_top_level_post_when_nothing_mentions_it(home, tmp_path):
    Bus(home, "harbor-42").post("orchestrator", "nothing for you here")
    pb = playbook(tmp_path, "rev", '[[steps]]\naction = "reply"\ntext = "VERDICT: SIGN-OFF"\n')
    run_fake(home, "reviewer", pb, ["nudge"])
    posted = [m for m in Bus(home, "harbor-42").read_all() if m["from"] == "reviewer"]
    assert posted[0]["parent"] is None


def test_attach_copies_the_file_into_the_channel(home, tmp_path):
    art = tmp_path / "report.md"
    art.write_text("# findings\n")
    pb = playbook(tmp_path, "dev", f'''
[[steps]]
action = "attach"
text = "here is the report"
file = "{art}"
''')
    run_fake(home, "developer", pb, ["nudge"])
    (msg,) = Bus(home, "harbor-42").read_all()
    (att,) = msg["attachments"]
    assert att["type"] == "file" and att["name"] == "report.md"
    assert (home / "channels" / "harbor-42" / att["ref"]).read_text() == "# findings\n"


def test_pin_step_pins_the_message(home, tmp_path):
    pb = playbook(tmp_path, "orc", '[[steps]]\naction = "pin"\ntext = "the plan"\n')
    run_fake(home, "orchestrator", pb, ["nudge"])
    bus = Bus(home, "harbor-42")
    assert [p["text"] for p in bus.pins()] == ["the plan"]


def test_noop_step_posts_nothing(home, tmp_path):
    pb = playbook(tmp_path, "dev", '[[steps]]\naction = "noop"\n')
    run_fake(home, "developer", pb, ["nudge"])
    assert Bus(home, "harbor-42").read_all() == []


def test_the_log_holds_the_literal_nudge_lines(home, tmp_path):
    log = tmp_path / "rounds.log"
    nudge = ("ratel: @developer 2 new on #harbor-42 from orchestrator, reviewer — "
             "call catch_up now, act on the latest mention, reply in thread 01ABC. "
             "Do not wait_for_mention.")
    pb = playbook(tmp_path, "dev", '[[steps]]\naction = "noop"\n')
    run_fake(home, "developer", pb, [nudge], log=log)
    assert log.read_text().splitlines() == [nudge]


def test_it_refuses_to_start_without_the_agent_environment(home, tmp_path):
    pb = playbook(tmp_path, "dev", '[[steps]]\naction = "noop"\n')
    p = subprocess.run([sys.executable, "-m", "ratel.clan.fake_harness", "--playbook", str(pb)],
                       input="", capture_output=True, text=True,
                       env={"RATEL_HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=30)
    assert p.returncode != 0 and "AGENT_NAME" in p.stderr


def test_console_script_is_declared():
    scripts = (tmp := __import__("tomllib")).loads(
        (__import__("pathlib").Path(__file__).parents[1] / "pyproject.toml").read_text()
    )["project"]["scripts"]
    assert scripts["ratel-fake-harness"] == "ratel.clan.fake_harness:main"


# ---- propose / approve steps (Task 8) -----------------------------------
from ratel.clan import config as C

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

ROLES = [
    {"name": "developer", "preset": "fake-low",
     "writer": True, "skills": [], "why": "writes"},
    {"name": "reviewer", "preset": "fake-high",
     "writer": False, "skills": [], "why": "checks"},
]


def _seed_channel(home):
    (home / "presets.toml").write_text(FAKE_PRESETS)
    paths = C.ClanPaths(home, "harbor-42")
    C.ClanConfig.from_dict({"channel": "harbor-42", "issue": 42, "repo": "acme/widget",
                            "checkout": "/repo", "roles": {"orchestrator": {}}},
                           C.load_catalog(home)).write(paths)
    return paths


def test_propose_step_posts_the_proposal(home, tmp_path):
    paths = _seed_channel(home)
    pf = tmp_path / "proposal.json"
    pf.write_text(json.dumps({"roles": ROLES}))
    pb = playbook(tmp_path, "orch", f'''
[[steps]]
action = "propose"
file = "{pf}"
''')
    run_fake(home, "orchestrator", pb, ["kickoff"])
    bus = Bus(home, "harbor-42")
    atts = [a for m in bus.read_all() for a in m.get("attachments", [])
            if a.get("type") == "clan"]
    assert atts and atts[0]["status"] == "proposed"
    assert atts[0]["roles"] == ROLES
    assert (paths.clan_toml.parent / "proposal.json").exists()


def test_approve_step_lands_clan_toml_and_writer_bits(home, tmp_path):
    paths = _seed_channel(home)
    bus = Bus(home, "harbor-42")
    proposal = bus.post("orchestrator", "Clan proposal", pin=True,
                        attachments=[{"type": "clan", "status": "proposed", "issue": 42,
                                      "roles": ROLES}])
    bus.post("stakeholder", "@orchestrator clan approved", pin=True,
             attachments=[{"type": "clan", "status": "approved", "issue": 42,
                           "roles": ROLES, "supersedes": proposal["id"]}])
    pb = playbook(tmp_path, "orch", '''
[[steps]]
action = "approve"
''')
    run_fake(home, "orchestrator", pb, ["the board approved"])
    cfg = C.ClanConfig.read(paths, C.load_catalog(home))
    assert set(cfg.roles) == {"orchestrator", "developer", "reviewer"}
    assert C.read_state(paths)["writers"]["developer"] is True
