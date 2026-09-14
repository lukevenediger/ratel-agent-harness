"""Rendered briefs, per-role harness configs, and the argv that launches a tab."""
import io
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ratel.clan import config as C
from ratel.clan import harness as H

PKG = Path(C.__file__).parent


@pytest.fixture
def clan(home):
    cfg = C.ClanConfig.from_dict(
        {"channel": "harbor-42", "issue": 42, "repo": "acme/widget",
         "checkout": str(home / "checkout"),
         "roles": {"orchestrator": {}, "developer": {}, "reviewer": {}}},
        C.load_catalog(home)).validate()
    (home / "checkout").mkdir()
    return cfg, C.ClanPaths(home, "harbor-42").ensure()


def test_clan_table_lists_every_role(clan):
    cfg, _ = clan
    table = H.clan_table(cfg)
    assert "developer" in table and "opencode" in table and "writer" in table.lower()
    assert table.count("\n") >= len(cfg.roles)


def test_render_brief_fills_the_fields(clan):
    cfg, _ = clan
    text = H.render_brief(cfg, "developer", "/tmp/harbor-wt/42-developer")
    assert "You are `developer` on ratel channel `harbor-42`" in text
    assert "acme/widget#42" in text and "issue-42" in text
    assert "/tmp/harbor-wt/42-developer" in text
    assert "reviewer" in text                       # the clan table came through
    assert len(text.encode()) < 64_000


def test_the_brief_names_the_real_clan_toml_path(clan):
    cfg, paths = clan
    hd = H.write_configs(paths, cfg, "orchestrator")
    brief = (hd / "brief.md").read_text()
    assert f"clan.toml: {paths.clan_toml}" in brief
    assert f"{paths.clan_toml}" == str(paths.channel_dir / "clan" / "clan.toml")


def test_render_brief_of_a_role_without_a_worktree(clan):
    cfg, _ = clan
    assert "the repository checkout" in H.render_brief(cfg, "orchestrator", None)


def test_the_unattended_brief_says_so_and_gives_the_channel_dir(clan):
    cfg, paths = clan
    brief = H.render_brief(cfg, "developer", "/tmp/wt", unattended=True)
    assert "This clan is unattended" in brief and "do not wait for approval" in brief
    attended = H.render_brief(cfg, "developer", "/tmp/wt")
    assert "unattended" not in attended
    hd = H.write_configs(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    assert f"Channel directory: {paths.channel_dir}" in (hd / "brief.md").read_text()


def test_write_configs_writes_only_under_the_channel_dir(clan):
    cfg, paths = clan
    hd = H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    assert hd == paths.harness_dir("developer")
    assert {p.name for p in hd.iterdir()} == {"mcp.json", "settings.json", "opencode.json", "brief.md"}
    assert list(Path(cfg.checkout).rglob("*")) == []          # nothing under the checkout
    assert str(hd).startswith(str(paths.channel_dir))


def test_mcp_json_runs_this_interpreter_not_a_path_lookup(clan):
    cfg, paths = clan
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    mcp = json.loads((paths.harness_dir("developer") / "mcp.json").read_text())
    server = mcp["mcpServers"]["ratel"]
    assert server["type"] == "stdio" and server["command"] == sys.executable
    assert server["args"] == ["-m", "ratel.mcp_server"]
    assert server["env"] == {"AGENT_NAME": "developer", "CHANNEL": "harbor-42",
                             "RATEL_HOME": str(paths.home)}


def test_settings_json_carries_the_unread_hook_with_env_inline(clan):
    cfg, paths = clan
    H.write_configs(paths, cfg, "reviewer", worktree="/tmp/wt")
    cmd = json.loads((paths.harness_dir("reviewer") / "settings.json").read_text()
                     )["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "AGENT_NAME=reviewer" in cmd and "CHANNEL=harbor-42" in cmd
    assert f"RATEL_HOME={paths.home}" in cmd and sys.executable in cmd
    assert "ratel.unread" in cmd


def test_settings_hook_command_survives_shell_metacharacters(tmp_path):
    import shlex
    home = tmp_path / "my; home"
    paths = C.ClanPaths(home, "chan-nel").ensure()
    cfg = C.ClanConfig.from_dict(
        {"channel": "chan-nel", "issue": 42, "repo": "o/r", "checkout": str(tmp_path),
         "roles": {"reviewer": {"model": "m"}, "developer": {}}},
        C.load_catalog(tmp_path)).validate()
    H.write_configs(paths, cfg, "reviewer", worktree="/tmp/wt")
    cmd = json.loads((paths.harness_dir("reviewer") / "settings.json").read_text()
                     )["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    parts = shlex.split(cmd)
    assert parts[:3] == ["AGENT_NAME=reviewer", "CHANNEL=chan-nel",
                         f"RATEL_HOME={home}"]
    assert parts[-3:] == [sys.executable, "-m", "ratel.unread"]


def _spellings(path):
    """Every spelling the harness pre-allows for one directory: the path as
    given plus its realpath when that differs (`/tmp` vs `/private/tmp` on
    macOS). On Linux the two coincide and there is exactly one — the expected
    set is computed the same way so the test holds on both."""
    return {f"{p}/**": "allow" for p in {path, os.path.realpath(path)}}


def test_opencode_json_points_at_the_brief_and_caps_model_output(clan):
    cfg, paths = clan
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    oc = json.loads((paths.harness_dir("developer") / "opencode.json").read_text())
    brief = paths.harness_dir("developer") / "brief.md"
    assert oc["instructions"] == [str(brief)] and Path(oc["instructions"][0]).is_absolute()
    assert oc["mcp"]["ratel"]["type"] == "local"
    assert oc["mcp"]["ratel"]["command"] == [sys.executable, "-m", "ratel.mcp_server"]
    assert oc["mcp"]["ratel"]["environment"]["AGENT_NAME"] == "developer"
    ds = oc["provider"]["deepseek"]["models"]["deepseek-flash"]
    assert ds["limit"]["output"] == 98304 and ds["options"]["reasoning"]["effort"] == "high"
    # attended: the clan's own channel dir, the role's worktree and the repo's
    # .git are pre-allowed, nothing else
    assert oc["permission"] == {"external_directory": {
        f"{paths.channel_dir}/**": "allow",
        **_spellings("/tmp/wt"),
        f"{cfg.checkout}/.git/**": "allow"}}


def test_unattended_opencode_json_denies_the_interactive_tools(clan):
    cfg, paths = clan
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    oc = json.loads((paths.harness_dir("developer") / "opencode.json").read_text())
    p = oc["permission"]
    assert p["bash"] == "allow" and p["edit"] == "allow" and p["webfetch"] == "allow"
    assert p["question"] == "deny" and p["task"] == "deny"
    assert p["external_directory"] == {"*": "deny",
                                       f"{paths.channel_dir}/**": "allow",
                                       **_spellings("/tmp/wt"),        # both spellings, where two exist
                                       f"{cfg.checkout}/.git/**": "allow"}


def test_opencode_external_directory_allows_the_worktree_and_repo_git(clan):
    cfg, paths = clan
    cfg.checkout = "/private/tmp/real-checkout"       # a realpath-style checkout
    H.write_configs(paths, cfg, "reviewer", worktree="/tmp/wt", unattended=False)
    oc = json.loads((paths.harness_dir("reviewer") / "opencode.json").read_text())
    rules = oc["permission"]["external_directory"]
    assert f"{cfg.checkout}/.git/**" in rules and "/private/tmp/real-checkout/.git/**" in rules
    assert _spellings("/tmp/wt").keys() <= rules.keys()   # both spellings, where two exist


def test_an_opencode_orchestrator_gets_the_skill_as_an_instruction(home):
    cfg = C.ClanConfig.from_dict(
        {"channel": "c", "issue": 7, "repo": "o/r", "checkout": str(home),
         "roles": {"orchestrator": {"harness": "opencode", "model": "openrouter/z-ai/glm-5.3-flash"},
                   "developer": {}}},
        C.load_catalog(home)).validate()
    paths = C.ClanPaths(home, "c").ensure()
    H.write_configs(paths, cfg, "orchestrator", worktree=None)
    oc = json.loads((paths.harness_dir("orchestrator") / "opencode.json").read_text())
    skill = PKG / "plugin" / "skills" / "dispatch-issue" / "SKILL.md"
    assert str(skill) in oc["instructions"]             # OpenCode cannot load --plugin-dir
    argv, _ = H.launch_argv(cfg, "orchestrator", paths.harness_dir("orchestrator"))
    assert argv[-1] == "Follow the dispatch-issue skill for issue 7."


def test_claude_argv_is_exact_and_ends_with_the_prompt(clan):
    cfg, paths = clan
    hd = H.write_configs(paths, cfg, "orchestrator", worktree=None)
    argv, extra = H.launch_argv(cfg, "orchestrator", hd)
    sid = extra["RATEL_SESSION_ID"]
    uuid.UUID(sid)                                      # a real uuid4
    assert argv[argv.index("--session-id") + 1] == sid  # exact file: <sid>.jsonl
    assert argv[0] == "claude"
    assert argv[-1] == "/clan:dispatch-issue 42"        # variadic flags must not swallow it
    assert argv[1:3] == ["--name", "orchestrator"]
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == str(hd / "mcp.json")
    assert argv[argv.index("--settings") + 1] == str(hd / "settings.json")
    assert argv[argv.index("--plugin-dir") + 1] == str(PKG / "plugin")
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"
    assert (hd / "brief.md").read_text() == argv[argv.index("--append-system-prompt") + 1]
    assert "--permission-mode" not in argv


def test_interactive_claude_gets_a_session_id_but_claude_p_round_two_does_not(clan):
    cfg, paths = clan
    hd = H.write_configs(paths, cfg, "reviewer", worktree="/tmp/wt")
    argv, extra = H.launch_argv(cfg, "reviewer", hd)
    assert extra["RATEL_SESSION_ID"] in argv        # the flag carries the same uuid
    cfg.roles["reviewer"].harness = "claude-p"
    argv_p2, extra_p2 = H.launch_argv(cfg, "reviewer", hd, round_n=2)
    assert "--session-id" not in argv_p2                # round 2+ resumes with --continue
    assert "RATEL_SESSION_ID" not in extra_p2


def test_claude_p_pins_a_session_id_on_round_one_and_continues_after(clan):
    cfg, paths = clan
    cfg.roles["reviewer"].harness = "claude-p"
    hd = H.write_configs(paths, cfg, "reviewer", worktree="/tmp/wt")
    argv1, extra1 = H.launch_argv(cfg, "reviewer", hd, round_n=1)
    sid = extra1["RATEL_SESSION_ID"]
    assert argv1[argv1.index("--session-id") + 1] == sid
    argv2, extra2 = H.launch_argv(cfg, "reviewer", hd, round_n=2)
    assert "--session-id" not in argv2 and "RATEL_SESSION_ID" not in extra2
    assert "--continue" in argv2


def test_record_session_pins_the_tab_for_measurement(clan):
    cfg, paths = clan
    H.write_configs(paths, cfg, "reviewer", worktree="/tmp/wt")
    H.record_session(paths, "reviewer", "abc-123")
    tab = C.read_state(paths)["tabs"]["reviewer"]
    assert tab["session_id"] == "abc-123"
    assert tab["launched"].endswith("Z")                # ISO UTC, same format as state["created"]


def test_claude_argv_takes_a_permission_mode_only_when_unattended(clan):
    cfg, paths = clan
    hd = H.write_configs(paths, cfg, "reviewer", worktree="/tmp/wt")
    argv, _ = H.launch_argv(cfg, "reviewer", hd, unattended=True)
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[-1].startswith("Start:")                 # still last
    assert "catch_up" in argv[-1]


def test_opencode_argv_and_env(clan):
    cfg, paths = clan
    hd = H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    argv, extra = H.launch_argv(cfg, "developer", hd)
    assert argv[0] == "opencode" and "--pure" in argv
    assert argv[argv.index("--model") + 1] == "deepseek/deepseek-flash"
    assert argv[argv.index("--prompt") + 1] == argv[-1] and argv[-1].startswith("Start:")
    assert extra == {"OPENCODE_CONFIG": str(hd / "opencode.json")}
    assert "--auto" not in argv
    assert "--auto" in H.launch_argv(cfg, "developer", hd, unattended=True)[0]


def test_launch_argv_accepts_an_explicit_prompt(clan):
    cfg, paths = clan
    hd = H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    argv, _ = H.launch_argv(cfg, "developer", hd, initial_prompt="read_thread 01ABC and fix 1-8")
    assert argv[-1] == "read_thread 01ABC and fix 1-8"


def test_install_skills_argv_and_excludes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(H.subprocess, "run",
                        lambda argv, **kw: calls.append((argv, kw)) or type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    excluded = []
    monkeypatch.setattr(H, "exclude_in_worktree", lambda wt, pats: excluded.append((wt, pats)))
    H.install_skills(tmp_path, ["superpowers:tdd", "obra/skill-x"])
    assert calls[0][0] == ["npx", "skills", "add", "superpowers:tdd", "-a", "claude,opencode", "-y"]
    assert calls[1][0][3] == "obra/skill-x"
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert excluded and ".agents/" in excluded[0][1]


def test_install_skills_does_nothing_without_specs(tmp_path, monkeypatch):
    monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: pytest.fail("should not run npx"))
    H.install_skills(tmp_path, [])


def test_install_skills_returns_the_specs_that_failed(tmp_path, monkeypatch):
    """The skills CLI is invoked with check=False: a non-zero exit is the only
    signal that an offline machine or a bad spec left a role without a skill it
    was promised. It has to reach the caller, not vanish."""
    monkeypatch.setattr(
        H.subprocess, "run",
        lambda argv, **kw: type("P", (), {
            "returncode": 1 if argv[3] == "obra/skill-x" else 0,
            "stdout": "", "stderr": "boom"})())
    monkeypatch.setattr(H, "exclude_in_worktree", lambda wt, pats: None)
    assert H.install_skills(tmp_path, ["superpowers:tdd", "obra/skill-x"]) == ["obra/skill-x"]


def test_install_skills_without_npx_reports_every_spec(tmp_path, monkeypatch, capsys):
    """A missing npx is surfaced like a non-zero exit, not a bare
    FileNotFoundError: the role is left without its skill either way."""
    monkeypatch.setattr(H.shutil, "which", lambda name: None)
    monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: pytest.fail("should not run npx"))
    assert H.install_skills(tmp_path, ["superpowers:tdd", "obra/skill-x"]) == \
        ["superpowers:tdd", "obra/skill-x"]
    assert "npx" in capsys.readouterr().err


@pytest.mark.parametrize("kind,binary", [("claude-p", "claude"), ("opencode-run", "opencode")])
def test_launch_names_the_missing_harness_binary(clan, monkeypatch, kind, binary):
    """A Linux user without the role's harness gets an instruction naming the
    binary, not a FileNotFoundError from inside execvpe."""
    cfg, paths = clan
    cfg.roles["developer"].harness = kind
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    monkeypatch.setattr(H.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match=f"{binary}.*not on PATH"):
        H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)


def test_launch_sets_the_agent_env_then_execs(clan, monkeypatch):
    cfg, paths = clan
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    seen = {}
    monkeypatch.setattr(os, "execvpe", lambda file, argv, env: seen.update(file=file, argv=argv, env=env))
    H.launch(paths, cfg, "developer", worktree="/tmp/wt")
    assert seen["file"] == "opencode" and seen["argv"][0] == "opencode"
    assert seen["env"]["AGENT_NAME"] == "developer"
    assert seen["env"]["CHANNEL"] == "harbor-42"
    assert seen["env"]["RATEL_HOME"] == str(paths.home)
    assert seen["env"]["OPENCODE_CONFIG"].endswith("opencode.json")
    assert "PATH" in seen["env"]                          # inherits the rest of the environment


# ---- headless kinds ------------------------------------------------------
def test_claude_p_argv_round_one(clan):
    cfg, paths = clan
    cfg.roles["orchestrator"].harness = "claude-p"
    hd = H.write_configs(paths, cfg, "orchestrator")
    argv, extra = H.launch_argv(cfg, "orchestrator", hd)
    assert argv[0] == "claude" and argv[1] == "-p"
    uuid.UUID(extra["RATEL_SESSION_ID"])
    assert argv[argv.index("--session-id") + 1] == extra["RATEL_SESSION_ID"]
    assert argv[-1] == "/clan:dispatch-issue 42"          # prompt last, still
    assert "--continue" not in argv                       # nothing to continue on round 1
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"
    assert argv[argv.index("--mcp-config") + 1] == str(hd / "mcp.json")
    assert argv[argv.index("--plugin-dir") + 1] == str(PKG / "plugin")


def test_claude_p_argv_round_two_unattended(clan):
    cfg, paths = clan
    cfg.roles["orchestrator"].harness = "claude-p"
    hd = H.write_configs(paths, cfg, "orchestrator")
    argv, _ = H.launch_argv(cfg, "orchestrator", hd, initial_prompt="fix findings 1-3",
                            unattended=True, round_n=2)
    assert "--continue" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    # the prompt comes BEFORE the variadic --add-dir/--allowedTools flags, or it is swallowed
    i = argv.index("fix findings 1-3")
    j = argv.index("--add-dir")
    cd = hd.parent.parent
    # geometry: only the writable trees are granted; the root is never add-dir'd
    assert i < j
    add_dirs = [argv[k + 1] for k, a in enumerate(argv) if a == "--add-dir"]
    assert add_dirs == [str(cd / "plans"), str(cd / "files"), str(hd), str(cd / "clan")]
    tools = [argv[k + 1] for k, a in enumerate(argv) if a == "--allowedTools"]
    # the add-dir set is the file boundary; acceptEdits is what makes writes
    # inside it possible at all; Bash/mcp rules DO narrow. No Read/Edit channel
    # rules: inert under both modes (measured on claude 2.1.263).
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert not any(t.startswith(("Read(", "Edit(", "Write(")) for t in tools)
    assert "mcp__ratel__*" in tools                 # measured on claude 2.1.263
    assert i < argv.index("--allowedTools")             # prompt precedes the variadic list
    for bare in ("Bash(git:*)", "Bash(gh:*)", "Bash(uv:*)", "Bash(npx:*)", "Bash(gh api:*)"):
        assert bare not in tools                        # narrowed, see UNATTENDED_TOOLS
    assert "--dangerously-skip-permissions" not in argv
    argv_attended, _ = H.launch_argv(cfg, "orchestrator", hd, initial_prompt="x", round_n=2)
    assert "--allowedTools" not in argv_attended and "--permission-mode" not in argv_attended
    assert argv_attended[-1] == "x"                     # attended: nothing variadic trails


def test_opencode_run_argv_and_env(clan):
    cfg, paths = clan
    cfg.roles["developer"].harness = "opencode-run"
    hd = H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    argv, extra = H.launch_argv(cfg, "developer", hd)
    assert argv[:8] == ["opencode", "run", "--dir", str(cfg.checkout),
                        "--pure", "--auto", "--format", "json"]
    assert argv[argv.index("--model") + 1] == "deepseek/deepseek-flash"
    assert argv[-1].startswith("Start:") and "-c" not in argv
    assert extra == {"OPENCODE_CONFIG": str(hd / "opencode.json")}
    argv2, _ = H.launch_argv(cfg, "developer", hd, initial_prompt="nudge 2", round_n=2,
                             session_id="ses_abc")
    assert "-s" in argv2 and "ses_abc" in argv2 and "-c" not in argv2
    assert argv2[-1] == "nudge 2"
    argv3, _ = H.launch_argv(cfg, "developer", hd, initial_prompt="n", round_n=2)
    assert "-c" in argv3                                  # no captured id: best effort


class FakePopen:
    """A child whose stdout arrives as given lines; records how it was started."""
    procs: list["FakePopen"] = []
    lines_for: list[str] = []
    next_rc: int | None = 0

    def __init__(self, argv, stdin=None, stdout=None, stderr=None, env=None, text=False,
                 **kw):
        self.argv, self.stdin, self.env, self.text = argv, stdin, env, text
        self.pid = 42424 + len(self.__class__.procs)      # killpg fallback path needs it
        self.returncode: int | None = FakePopen.next_rc
        self.killed = False
        self.stdout = io.StringIO("".join(FakePopen.lines_for))
        self.stderr = io.StringIO()
        if argv[0] != "ps":                  # _ps_lstart's ps calls are not rounds
            type(self).procs.append(self)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class HangingPopen(FakePopen):
    """A child that never exits until it is killed."""

    def __init__(self, argv, **kw):
        super().__init__(argv, **kw)
        self.returncode = None
        self.stdout = io.StringIO()

    def poll(self):
        return None if not self.killed else -9

    def wait(self, timeout=None):
        if timeout is not None and not self.killed:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode


def install_popen(monkeypatch, cls):
    monkeypatch.setattr(H.subprocess, "Popen", cls)
    cls.procs = []
    return cls


def test_headless_launch_runs_a_round_per_nudge(clan, monkeypatch, capsys):
    cfg, paths = clan
    cfg.roles["developer"].harness = "opencode-run"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    cls = install_popen(monkeypatch, FakePopen)
    cls.lines_for = ["out\n"]

    class Flaky(FakePopen):                          # round 2 fails…
        def __init__(self, argv, **kw):
            super().__init__(argv, **kw)
            self.returncode = 3 if len(Flaky.procs) == 2 else 0

    install_popen(monkeypatch, Flaky)
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True,
             stdin=io.StringIO("nudge one\n\nnudge two\n"))   # blank line is skipped
    hd = paths.harness_dir("developer")
    recs = [json.loads(line) for line in (hd / "rounds.jsonl").read_text().splitlines()]
    assert [r["round"] for r in recs] == [1, 2, 3]
    assert recs[0]["prompt"] == H.default_prompt(cfg, "developer")   # the kickoff round
    assert [r["prompt"] for r in recs[1:]] == ["nudge one", "nudge two"]
    assert recs[1]["returncode"] == 3 and recs[2]["returncode"] == 0   # …and did not stop the loop
    assert recs[1]["output"] == "out\n"
    for rec, proc in zip(recs, Flaky.procs):
        assert proc.stdin is subprocess.DEVNULL      # never the pane tty the watcher types into
        assert rec["prompt"] in proc.argv
        assert proc.env["CLAN_UNATTENDED"] == "1" and proc.env["AGENT_NAME"] == "developer"
    assert "nudge one" in (hd / "nudges.log").read_text()
    assert "nudge two" in (hd / "nudges.log").read_text()
    assert capsys.readouterr().out.count("out\n") == 3   # stdout reached the pane each round


def test_headless_launched_is_recorded_once_per_session_not_per_round(clan, monkeypatch):
    """`launched` is the measurement's launch gate: a session begins at round 1
    (and again after a reset rewinds to round 1), not on every headless round.
    `record_round_session` falls through to `record_launched` when the round's
    argv carries no session id, so a per-round call would move the gate past
    the session row / pinned transcript and un-measure the role."""
    cfg, paths = clan
    cfg.roles["developer"].harness = "opencode-run"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    cls = install_popen(monkeypatch, FakePopen)
    cls.lines_for = ["out\n"]
    calls: list[str] = []
    real_launched, real_round = H.record_launched, H.record_round_session
    monkeypatch.setattr(H, "record_launched",
                        lambda *a: calls.append("launched") or real_launched(*a))
    monkeypatch.setattr(H, "record_round_session",
                        lambda *a: calls.append("round") or real_round(*a))

    class Clock:                                 # second-granularity ISO: make it move
        counter = 0

        @classmethod
        def now(cls, tz):
            cls.counter += 1
            return datetime(2026, 9, 8, 14, 0, cls.counter % 60, tzinfo=timezone.utc)

    monkeypatch.setattr(H, "datetime", Clock)
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True,
             stdin=io.StringIO("nudge one\n\nnudge two\n"))   # kickoff + 2 nudges = 3 rounds
    recs = [json.loads(line) for line in (paths.harness_dir("developer")
                                          / "rounds.jsonl").read_text().splitlines()]
    assert len(recs) == 3
    assert calls == ["launched", "round", "launched"]   # launch gate, then round 1 only
    first = C.read_state(paths)["tabs"]["developer"]["launched"]
    (paths.harness_dir("developer") / "reset").touch()
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True,
             stdin=io.StringIO("after reset\n"))
    assert calls == ["launched", "round", "launched"] * 2        # reset re-records
    assert C.read_state(paths)["tabs"]["developer"]["launched"] != first


def test_reset_file_restarts_headless_rounds_at_round_one(clan, monkeypatch):
    """`clan checkpoint` writes `reset`; the next round must launch as round 1
    (no --continue / -s) with the remembered session wiped, and log it."""
    cfg, paths = clan
    cfg.roles["developer"].harness = "opencode-run"
    hd = H.write_configs(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    (hd / "opencode-session").write_text("sess-1\n")
    (hd / "reset").touch()
    cls = install_popen(monkeypatch, FakePopen)
    cls.lines_for = ["out\n"]
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True,
             stdin=io.StringIO("nudge\n"))
    recs = [json.loads(line) for line in (hd / "rounds.jsonl").read_text().splitlines()]
    assert [r["round"] for r in recs] == [1, 2]        # the reset round, then the nudge round
    assert recs[0]["reset"] is True and recs[1]["reset"] is False
    assert "--continue" not in recs[0]["argv"] and "-s" not in recs[0]["argv"] and "-c" not in recs[0]["argv"]
    assert not (hd / "opencode-session").exists()
    tab = C.read_state(paths)["tabs"]["developer"]
    assert tab["launched"].endswith("Z")               # opencode-run gets a launch gate
    (hd / "reset").touch()                             # a second reset, same again
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True,
             stdin=io.StringIO("nudge\n"))
    recs = [json.loads(line) for line in (hd / "rounds.jsonl").read_text().splitlines()]
    assert [r["round"] for r in recs] == [1, 2, 1, 2]


def test_a_wedged_round_is_killed_at_the_timeout_and_the_loop_continues(clan, monkeypatch):
    cfg, paths = clan
    cfg.roles["developer"].harness = "opencode-run"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    monkeypatch.setenv("CLAN_ROUND_TIMEOUT", "0.1")
    cls = install_popen(monkeypatch, HangingPopen)
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True,
             stdin=io.StringIO("second nudge\n"))
    assert len(cls.procs) == 2 and cls.procs[0].killed
    recs = [json.loads(line) for line in (paths.harness_dir("developer")
                                          / "rounds.jsonl").read_text().splitlines()]
    assert [r["returncode"] for r in recs] == ["timeout", "timeout"]   # the loop kept going


def test_orchestrator_brief_keys_the_skill_and_the_plugin_dir(home):
    """The skill load paths key on the BRIEF, not the role name: a role named
    orchestrator running a probe brief must load no skill anywhere."""
    cfg = C.ClanConfig.from_dict(
        {"channel": "c", "issue": 7, "repo": "o/r", "checkout": str(home),
         "roles": {"orchestrator": {"harness": "opencode", "brief": "probe"},
                   "developer": {}}},
        C.load_catalog(home)).validate()
    paths = C.ClanPaths(home, "c").ensure()
    H.write_configs(paths, cfg, "orchestrator", worktree=None)
    oc = json.loads((paths.harness_dir("orchestrator") / "opencode.json").read_text())
    skill = PKG / "plugin" / "skills" / "dispatch-issue" / "SKILL.md"
    assert str(skill) not in oc["instructions"]         # probe brief: no skill injection
    cfg.roles["orchestrator"].harness = "claude-p"
    argv, _ = H.launch_argv(cfg, "orchestrator", paths.harness_dir("orchestrator"))
    assert "--plugin-dir" not in argv


def test_a_probe_brief_gets_no_ratel_cli_and_renders(clan):
    cfg, paths = clan
    cfg.roles["developer"].brief = "probe"
    tools = H.unattended_tools(cfg.roles["developer"])
    assert "Bash(ratel:*)" not in tools
    assert "mcp__ratel__*" in tools                 # the channel MCP stays
    assert "Bash(git diff:*)" in tools
    for t in ("Bash(uv run pytest:*)", "Bash(python -m pytest:*)"):   # conftest at
        assert t not in tools                           # collection is arbitrary code
    assert "Bash(git add:*)" not in tools and "Bash(git push:*)" not in tools
    text = H.render_brief(cfg, "developer", "/tmp/wt")
    assert "Never run `ratel clan`" in text and "You are `developer`" in text
    assert "clan_table" not in text                     # trimmed: no clan idea handed over


def test_a_probe_named_orchestrator_gets_no_dispatch_prompt_or_clan_dir(clan):
    """Every arming decision keys on the brief: the role NAME must arm nothing
    — not the prompt, not the clan/ add-dir. (Review of issue #6.)"""
    cfg, paths = clan
    for harness in ("claude-p", "opencode-run"):
        cfg.roles["orchestrator"].brief = "probe"
        cfg.roles["orchestrator"].harness = harness
        assert H.default_prompt(cfg, "orchestrator") == H.ROLE_PROMPT
        H.write_configs(paths, cfg, "orchestrator", unattended=True)
        argv, _ = H.launch_argv(cfg, "orchestrator", paths.harness_dir("orchestrator"),
                                initial_prompt="x", unattended=True, round_n=2)
        add_dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        assert not any(a.endswith("/clan") for a in add_dirs)


def test_unattended_tools_differ_by_role(clan):
    cfg, _ = clan
    writer = H.unattended_tools(cfg.roles["developer"])
    assert "Bash(ratel:*)" in writer
    assert "Bash(git add:*)" in writer and "Bash(git commit:*)" in writer
    assert "Bash(git push:*)" in writer
    reviewer = H.unattended_tools(cfg.roles["reviewer"])
    assert "Bash(ratel:*)" in reviewer
    assert "Bash(git status:*)" in reviewer and "Bash(git add:*)" not in reviewer
    assert "Bash(git branch:*)" not in reviewer              # security: branch -D lives here
    assert "Bash(python -m pytest:*)" in reviewer and "Bash(uv run pytest:*)" in reviewer
    orchestrator = H.unattended_tools(cfg.roles["orchestrator"])
    assert "Bash(gh issue:*)" in orchestrator and "Bash(gh api graphql:*)" in orchestrator


def test_no_role_ever_gets_the_channel_root_as_a_working_directory(clan):
    """The regression guard: the channel root holds clan.state.json, so it is
    excluded from every role's --add-dir by design. A role that cannot find a
    file there needs its brief corrected, not the root granted."""
    cfg, paths = clan      # three roles: peer pairs include reviewer <-> developer
    assert len(cfg.roles) >= 3
    for role in cfg.roles:                    # geometry probes are claude-p shaped
        cfg.roles[role].harness = "claude-p"
        H.write_configs(paths, cfg, role)
        argv, _ = H.launch_argv(cfg, role, paths.harness_dir(role),
                                initial_prompt="x", unattended=True, round_n=2)
        dirs = [Path(argv[i + 1]).resolve() for i, a in enumerate(argv) if a == "--add-dir"]
        # containment, not equality: no granted tree may contain the control
        # plane (state anchor) or any peer's harness dir; .resolve() so /tmp vs
        # /private/tmp cannot quietly falsify the check on macOS
        assert dirs
        for d in dirs:
            assert not paths.state_json.resolve().is_relative_to(d)
            for peer in cfg.roles:
                if peer != role:
                    assert not paths.harness_dir(peer).resolve().is_relative_to(d)


def test_a_non_orchestrator_cannot_edit_the_clans_control_plane(clan):
    """Geometry, not rules: nothing inside the channel root or peer harness
    dirs is add-dir'd, so acceptEdits never reaches them."""
    cfg, paths = clan
    cfg.roles["reviewer"].harness = "claude-p"
    hd = H.write_configs(paths, cfg, "reviewer")
    argv, _ = H.launch_argv(cfg, "reviewer", hd, initial_prompt="x", unattended=True, round_n=2)
    cd = hd.parent.parent
    add_dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert add_dirs == [str(cd / "plans"), str(cd / "files"), str(hd)]
    for forbidden in (cd, paths.harness_dir("developer"), paths.channel_dir / "clan"):
        assert str(forbidden) not in add_dirs
    assert not any(t.startswith("Read(") for t in
                   [argv[i + 1] for i, a in enumerate(argv) if a == "--allowedTools"])


def test_mark_trusted_sets_one_key_and_preserves_the_rest(tmp_path, monkeypatch):
    cfgdir = tmp_path / "claude-config2"                 # autouse fixture owns claude-config
    cfgdir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfgdir))
    cfg = cfgdir / ".claude.json"
    cfg.write_text(json.dumps({"numStartups": 7, "projects": {"/other": {
        "allowedTools": ["Bash(ls:*)"], "hasTrustDialogAccepted": True}}}))
    assert H.mark_trusted("/new/project") is True
    data = json.loads(cfg.read_text())
    assert data["numStartups"] == 7                            # everything else survives
    assert data["projects"]["/other"] == {"allowedTools": ["Bash(ls:*)"],
                                          "hasTrustDialogAccepted": True}
    assert data["projects"]["/new/project"] == {"hasTrustDialogAccepted": True}
    assert H.mark_trusted("/new/project") is False             # idempotent


def test_mark_trusted_never_clobbers_an_unparsable_config(tmp_path, monkeypatch):
    cfgdir = tmp_path / "claude-config2"                 # autouse fixture owns claude-config
    cfgdir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfgdir))
    (cfgdir / ".claude.json").write_text("{not json")
    assert H.mark_trusted("/x") is False
    assert (cfgdir / ".claude.json").read_text() == "{not json"


def test_mark_trusted_defaults_to_home_claude_json(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    H.mark_trusted("/p")
    assert json.loads((tmp_path / ".claude.json").read_text())["projects"]["/p"] == {
        "hasTrustDialogAccepted": True}


def test_an_interactive_prompt_round_is_killed_and_labelled(clan, monkeypatch):
    """A round that prints an interactive prompt is a config error, not a timeout."""
    cfg, paths = clan
    cfg.roles["developer"].harness = "opencode-run"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    monkeypatch.setenv("CLAN_ROUND_TIMEOUT", "30")
    real_popen = subprocess.Popen

    def popen(argv, **kw):
        return real_popen(["sh", "-c", "echo 'Do you trust the files in this folder?'; "
                                       "echo 'No, exit'; sleep 30"],
                          stdin=kw["stdin"], stdout=kw["stdout"], stderr=kw["stderr"],
                          env=kw["env"], text=True, start_new_session=True)
    monkeypatch.setattr(subprocess, "Popen", popen)
    start = time.monotonic()
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True,
             stdin=io.StringIO())
    assert time.monotonic() - start < 15                       # killed long before the timeout
    rec = json.loads((paths.harness_dir("developer") / "rounds.jsonl").read_text())
    assert rec["returncode"] == "prompt"
    assert "Do you trust the files in this folder?" in rec["output"]


def test_concurrent_claude_config_writers_do_not_lose_updates(tmp_path, monkeypatch):
    """flock: two callers updating the live config at once both land."""
    import threading
    cfgdir = tmp_path / "claude-config2"
    cfgdir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfgdir))
    barrier = threading.Barrier(2)
    results = {}

    def writer(name):
        barrier.wait()
        results[name] = H.mark_trusted(f"/p/{name}")
    threads = [threading.Thread(target=writer, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads((cfgdir / ".claude.json").read_text())
    assert sorted(data["projects"]) == ["/p/a", "/p/b"]        # both survived
    assert all(results.values())


def test_untrust_removes_only_the_clans_own_entries(tmp_path, monkeypatch):
    cfgdir = tmp_path / "claude-config2"
    cfgdir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfgdir))
    (cfgdir / ".claude.json").write_text(json.dumps(
        {"numStartups": 3, "projects": {"/tmp/x/run1/sandbox": {"hasTrustDialogAccepted": True},
                                        "/tmp/x/run1/sandbox-wt/9-dev": {"hasTrustDialogAccepted": True},
                                        "/tmp/x/run1-other": {"hasTrustDialogAccepted": True}}}))
    removed = H.untrust("/tmp/x/run1")
    assert removed == 2                                        # the sibling dir survives
    data = json.loads((cfgdir / ".claude.json").read_text())
    assert sorted(data["projects"]) == ["/tmp/x/run1-other"]
    assert data["numStartups"] == 3
    assert H.untrust("/tmp/x/clone") == 0                      # idempotent


def test_a_broken_kickoff_does_not_kill_the_tab(clan, monkeypatch):
    cfg, paths = clan
    cfg.roles["developer"].harness = "opencode-run"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    real_popen = subprocess.Popen
    install_popen(monkeypatch, FakePopen)               # placeholder, replaced below

    def missing(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", argv[0])
    monkeypatch.setattr(H.subprocess, "Popen", missing)
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True,
             stdin=io.StringIO("nudge after the crash\n"))
    recs = [json.loads(line) for line in (paths.harness_dir("developer")
                                          / "rounds.jsonl").read_text().splitlines()]
    assert recs[0]["returncode"] == "error"
    assert "FileNotFoundError" in recs[0]["output"] and "opencode" in recs[0]["output"]
    assert "nudge after the crash" in (paths.harness_dir("developer")
                                       / "nudges.log").read_text()
    assert real_popen                                   # silence unused-import lints


def test_attended_headless_launch_carries_no_clan_unattended(clan, monkeypatch):
    cfg, paths = clan
    cfg.roles["developer"].harness = "opencode-run"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    cls = install_popen(monkeypatch, FakePopen)
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=False,
             stdin=io.StringIO())
    assert "CLAN_UNATTENDED" not in cls.procs[0].env


def test_round_output_caps_to_the_last_40_lines_unless_clan_round_log_full(clan, monkeypatch):
    cfg, paths = clan
    cfg.roles["developer"].harness = "opencode-run"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    cls = install_popen(monkeypatch, FakePopen)
    cls.lines_for = [f"line{i}\n" for i in range(50)]
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True, stdin=io.StringIO())
    rec = json.loads((paths.harness_dir("developer") / "rounds.jsonl").read_text())
    assert rec["output"] == "".join(f"line{i}\n" for i in range(10, 50))

    monkeypatch.setenv("CLAN_ROUND_LOG", "full")
    (paths.harness_dir("developer") / "rounds.jsonl").unlink()
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True, stdin=io.StringIO())
    rec = json.loads((paths.harness_dir("developer") / "rounds.jsonl").read_text())
    assert rec["output"] == "".join(f"line{i}\n" for i in range(10, 50))
    full = paths.harness_dir("developer") / rec["logs"]["stdout"]
    assert full.read_text() == "".join(f"line{i}\n" for i in range(50))
    assert rec["truncated"]["stdout"] is True


def test_round_argv_logs_the_brief_by_path_not_text(clan, monkeypatch):
    cfg, paths = clan
    cfg.roles["developer"].harness = "claude-p"
    hd = H.write_configs(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    cls = install_popen(monkeypatch, FakePopen)
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True, stdin=io.StringIO())
    rec = json.loads((hd / "rounds.jsonl").read_text())
    i = rec["argv"].index("--append-system-prompt")
    assert rec["argv"][i + 1] == str(hd / "brief.md")
    assert (hd / "brief.md").read_text() not in rec["argv"]
    assert rec["prompt"] not in rec["argv"]             # prompt-free even when it is not last


def test_timeout_kills_the_whole_process_group_and_the_loop_continues(clan, monkeypatch):
    """Real child: a background grandchild must not outlive or wedge the round."""
    cfg, paths = clan
    cfg.roles["developer"].harness = "opencode-run"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    monkeypatch.setenv("CLAN_ROUND_TIMEOUT", "1")
    real_popen = subprocess.Popen

    def popen(argv, **kw):                              # rewrite the agent argv to the shell line
        return real_popen(["sh", "-c", "sleep 30 & echo started; sleep 30"],
                          stdin=kw["stdin"], stdout=kw["stdout"], stderr=kw["stderr"],
                          env=kw["env"], text=True, start_new_session=True)
    monkeypatch.setattr(subprocess, "Popen", popen)
    start = time.monotonic()
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True,
             stdin=io.StringIO("second nudge\n"))
    elapsed = time.monotonic() - start
    assert elapsed < 15                                 # killpg freed the pump threads quickly
    recs = [json.loads(line) for line in (paths.harness_dir("developer")
                                          / "rounds.jsonl").read_text().splitlines()]
    assert [r["returncode"] for r in recs] == ["timeout", "timeout"]


def test_a_round_that_closes_stdout_then_exits_later_is_not_a_timeout(clan, monkeypatch):
    cfg, paths = clan
    cfg.roles["developer"].harness = "opencode-run"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    real_popen = subprocess.Popen

    def popen(argv, **kw):
        return real_popen(["sh", "-c", "echo hi; exec 1>&-; sleep 0.4"],
                          stdin=kw["stdin"], stdout=kw["stdout"], stderr=kw["stderr"],
                          env=kw["env"], text=True, start_new_session=True)
    monkeypatch.setattr(subprocess, "Popen", popen)
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True, stdin=io.StringIO())
    rec = json.loads((paths.harness_dir("developer") / "rounds.jsonl").read_text())
    assert rec["returncode"] == 0                       # the process exited; not a timeout
    assert rec["output"] == "hi\n"


def test_exec_launch_carries_clan_unattended_when_unattended(clan, monkeypatch):
    cfg, paths = clan
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    seen = {}
    monkeypatch.setattr(os, "execvpe", lambda file, argv, env: seen.update(env=env))
    H.launch(paths, cfg, "developer", worktree="/tmp/wt", unattended=True)
    assert seen["env"]["CLAN_UNATTENDED"] == "1"


def test_opencode_session_id_extraction_pins_the_real_output_shape():
    fixture = (Path(__file__).parent / "fixtures" / "opencode-run-session.jsonl").read_text()
    assert H.extract_opencode_session(fixture) == "ses_f84fde45effe9DDqtj1EnCKZ4C"
    assert H.extract_opencode_session("no session here") is None


def test_permission_output_does_not_depend_on_the_harness_kind(home):
    """Every emitted file except brief.md is invariant under the harness kind
    (brief.md embeds clan_table and is meant to vary). One shared ClanPaths:
    the external_directory patterns embed the channel dir, so a per-kind
    channel dir would differ for reasons that have nothing to do with kind."""
    paths = C.ClanPaths(home, "ch").ensure()
    worktree = home / "wt"
    for unattended in (False, True):
        perms, hooks, mcps = set(), set(), set()
        for kind in C.HARNESSES:
            cfg = C.ClanConfig.from_dict(
                {"channel": "ch", "issue": 1, "repo": "o/r", "checkout": str(home),
                 "roles": {"reviewer": {"harness": kind, "model": "m"},
                           "developer": {"harness": kind, "model": "m"}}},
                C.load_catalog(home)).validate()
            hd = H.write_configs(paths, cfg, "reviewer", worktree=worktree,
                                 unattended=unattended)
            perms.add(json.dumps(json.loads((hd / "opencode.json").read_text())["permission"],
                                 sort_keys=True))
            hooks.add((hd / "settings.json").read_text())
            mcps.add((hd / "mcp.json").read_text())
        assert len(perms) == 1 and len(hooks) == 1 and len(mcps) == 1


# ---- Task 2: provider block + effort from the models catalog ---------------
def test_glm_role_provider_block_comes_from_the_catalog(clan):
    cfg, paths = clan
    cfg.roles["developer"].model = "openrouter/z-ai/glm-5.3-flash"
    cfg.roles["developer"].effort = "low"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    oc = json.loads((paths.harness_dir("developer") / "opencode.json").read_text())
    assert oc["provider"] == {"openrouter": {"models": {"z-ai/glm-5.3-flash": {
        "limit": {"context": 1048576, "output": 98304},
        "options": {"reasoning": {"effort": "low"}, "max_tokens": 98304}}}}}


def test_deepseek_role_provider_block_is_exact(clan):
    cfg, paths = clan
    cfg.roles["developer"].model = "deepseek/deepseek-flash"
    cfg.roles["developer"].effort = "max"     # the provider's own word, not a level
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    oc = json.loads((paths.harness_dir("developer") / "opencode.json").read_text())
    assert oc["provider"] == {"deepseek": {
        "npm": "@ai-sdk/openai-compatible",
        "options": {"baseURL": "https://api.deepseek.com"},
        "models": {"deepseek-flash": {
            "limit": {"context": 1000000, "output": 98304},
            "options": {"reasoning": {"effort": "max"}, "max_tokens": 98304},
            "reasoning": True, "tool_call": True,
            "interleaved": {"field": "reasoning_content"}}}}}


def test_the_openrouter_deepseek_id_keys_the_provider_block_by_its_last_two_segments(clan):
    """`_opencode_provider` splits once: a three-segment id must key the block
    as `deepseek/deepseek-v4.1-flash`, under the `openrouter` provider."""
    cfg, paths = clan
    cfg.roles["developer"].model = "openrouter/deepseek/deepseek-v4.1-flash"
    cfg.roles["developer"].effort = "xhigh"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    oc = json.loads((paths.harness_dir("developer") / "opencode.json").read_text())
    assert oc["provider"] == {"openrouter": {"models": {"deepseek/deepseek-v4.1-flash": {
        "limit": {"context": 1000000, "output": 98304},
        "options": {"reasoning": {"effort": "xhigh"}, "max_tokens": 98304}}}}}


def test_an_effort_the_model_does_not_declare_reaches_no_provider_block(clan):
    """Defence in depth below validate, for a hand-edited clan.toml: DeepSeek
    declares only high and max, so a stale "medium" adds no reasoning options."""
    cfg, paths = clan
    cfg.roles["developer"].model = "deepseek/deepseek-flash"
    cfg.roles["developer"].effort = "medium"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    oc = json.loads((paths.harness_dir("developer") / "opencode.json").read_text())
    ds = oc["provider"]["deepseek"]["models"]["deepseek-flash"]
    assert ds["options"] == {"max_tokens": 98304}      # no reasoning key at all


def test_a_claude_only_clan_writes_no_provider_key(clan):
    cfg, paths = clan
    H.write_configs(paths, cfg, "reviewer", worktree="/tmp/wt")
    oc = json.loads((paths.harness_dir("reviewer") / "opencode.json").read_text())
    assert "provider" not in oc


def test_an_unknown_model_writes_no_provider_key(clan):
    cfg, paths = clan
    cfg.roles["developer"].model = "vendor/mystery"
    H.write_configs(paths, cfg, "developer", worktree="/tmp/wt")
    oc = json.loads((paths.harness_dir("developer") / "opencode.json").read_text())
    assert "provider" not in oc


def test_claude_argv_carries_effort_from_the_catalog(clan):
    cfg, paths = clan
    assert cfg.roles["orchestrator"].effort == "high"        # catalog default
    hd = H.write_configs(paths, cfg, "orchestrator", worktree=None)
    argv, _ = H.launch_argv(cfg, "orchestrator", hd)
    assert argv[argv.index("--effort") + 1] == "high"


def test_a_home_efforts_list_reaches_the_claude_argv(home):
    """The claude effort lookup must read the HOME models.toml, not just the
    packaged one: the id below exists nowhere else, so the asserted --effort
    value can only come from the home file (kills both the wrong-dir and the
    packaged-only catalog mutations)."""
    paths = C.ClanPaths(home, "chan").ensure()
    (home / "models.toml").write_text(
        '[models."acme/claude-x"]\nname = "Acme X"\nharness = ["claude", "claude-p"]\n'
        'efforts = ["balanced", "extreme"]\n')
    cfg = C.ClanConfig.from_dict(
        {"channel": "chan", "issue": 1, "repo": "o/r", "checkout": str(home),
         "roles": {"reviewer": {"model": "acme/claude-x", "effort": "extreme"},
                   "developer": {}}},
        C.load_catalog(home)).validate(models=C.load_models(home))
    hd = H.write_configs(paths, cfg, "reviewer", worktree="/tmp/wt")
    argv, _ = H.launch_argv(cfg, "reviewer", hd)
    assert argv[argv.index("--effort") + 1] == "extreme"
    argv2, _ = H.launch_argv(cfg, "reviewer", hd, home=home)   # explicit home: same
    assert argv2[argv2.index("--effort") + 1] == "extreme"
    # a catalogued model that declares NO efforts sends no flag, for any word
    (home / "models.toml").write_text((home / "models.toml").read_text() +
        '\n[models."acme/claude-y"]\nname = "Acme Y"\nharness = ["claude", "claude-p"]\n')
    cfg.roles["reviewer"].model = "acme/claude-y"
    argv3, _ = H.launch_argv(cfg, "reviewer", hd, home=home)
    assert "--effort" not in argv3


def test_an_effort_the_model_does_not_declare_sends_no_claude_effort_flag(clan):
    """Defence in depth below validate, for a hand-edited clan.toml."""
    cfg, paths = clan
    cfg.roles["reviewer"].effort = "extreme"       # claude-opus-5 declares low/medium/high
    hd = H.write_configs(paths, cfg, "reviewer", worktree="/tmp/wt")
    argv, _ = H.launch_argv(cfg, "reviewer", hd)
    assert "--effort" not in argv


def test_an_unknown_model_gets_no_effort_flag(clan):
    cfg, paths = clan
    cfg.roles["reviewer"].model = "vendor/mystery"
    hd = H.write_configs(paths, cfg, "reviewer", worktree="/tmp/wt")
    argv, _ = H.launch_argv(cfg, "reviewer", hd)
    assert "--effort" not in argv


def test_missing_env_reports_unset_keys_per_role(clan):
    cfg, _ = clan
    cfg.roles["developer"].model = "deepseek/deepseek-flash"
    models = C.load_models(str(clan[1].home))
    unset = H.missing_env(cfg, models, environ={})
    assert unset == {"developer": ["DEEPSEEK_API_KEY"]}
    assert H.missing_env(cfg, models, environ={"DEEPSEEK_API_KEY": "sk-x"}) == {}


def test_write_configs_refuses_symlink_file(home, tmp_path):
    paths = C.ClanPaths(home, 'test').ensure()
    cfg = C.ClanConfig.from_dict(
        {'channel': 'test', 'issue': 42, 'repo': 'o/r', 'checkout': str(tmp_path),
         'roles': {'developer': {}}}, C.load_catalog(home)).validate()
    hd = paths.harness_dir('developer')
    hd.mkdir()
    outside = tmp_path / 'keep'
    outside.write_text('untouched')
    (hd / 'settings.json').symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        H.write_configs(paths, cfg, 'developer')
    assert outside.read_text() == 'untouched'


def test_supervisor_keeps_early_session_id_after_large_output(clan, monkeypatch):
    cfg, paths = clan
    cfg.roles['developer'].harness = 'opencode-run'
    H.write_configs(paths, cfg, 'developer', worktree='/tmp/wt')
    cls = install_popen(monkeypatch, FakePopen)
    cls.lines_for = ['{"sessionID":"ses_early"}\n'] + ['x' * 4096] * 30
    H.launch(paths, cfg, 'developer', worktree='/tmp/wt', unattended=True, stdin=io.StringIO('next\n'))
    assert 'ses_early' in cls.procs[1].argv
    records = [json.loads(line) for line in (paths.harness_dir('developer') / 'rounds.jsonl').read_text().splitlines()]
    assert all(len(r['output']) <= 65536 and r['truncated']['stdout'] for r in records)


def test_supervisor_keeps_stderr_failure_excerpt(clan, monkeypatch):
    cfg, paths = clan
    cfg.roles['developer'].harness = 'opencode-run'
    H.write_configs(paths, cfg, 'developer', worktree='/tmp/wt')
    class Failure(FakePopen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.returncode = 1
            self.stderr = io.StringIO('failure detail\n')
    install_popen(monkeypatch, Failure)
    H.launch(paths, cfg, 'developer', worktree='/tmp/wt', unattended=True, stdin=io.StringIO())
    record = json.loads((paths.harness_dir('developer') / 'rounds.jsonl').read_text())
    assert record['returncode'] == 1 and record['stderr'] == 'failure detail\n'


def test_supervisor_reports_capture_failure(clan, monkeypatch):
    cfg, paths = clan
    cfg.roles['developer'].harness = 'opencode-run'
    H.write_configs(paths, cfg, 'developer', worktree='/tmp/wt')
    class BrokenStream:
        def readline(self, size):
            raise OSError('capture failed')
    class Failure(FakePopen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stdout = BrokenStream()
    install_popen(monkeypatch, Failure)
    H.launch(paths, cfg, 'developer', worktree='/tmp/wt', unattended=True, stdin=io.StringIO())
    record = json.loads((paths.harness_dir('developer') / 'rounds.jsonl').read_text())
    assert record['returncode'] == 'error'
    assert 'output capture failed' in record['output']


@pytest.mark.parametrize('limit,rc,reason', [('CLAN_MAX_ROUNDS', 0, 'max_rounds'),
                                          ('CLAN_MAX_FAILURES', 1, 'max_failures')])
def test_supervisor_stops_launching_at_budget(clan, monkeypatch, limit, rc, reason):
    cfg, paths = clan
    cfg.roles['developer'].harness = 'opencode-run'
    H.write_configs(paths, cfg, 'developer', worktree='/tmp/wt')
    monkeypatch.setenv(limit, '2')
    monkeypatch.setattr(FakePopen, 'next_rc', rc)
    cls = install_popen(monkeypatch, FakePopen)
    H.launch(paths, cfg, 'developer', worktree='/tmp/wt', unattended=True,
             stdin=io.StringIO('next\nnext\nnext\n'))
    assert len(cls.procs) == 2
    run = C.read_state(paths)['runs']['developer']
    assert run['reason'] == reason and run['rounds'] == 2


def test_supervisor_passes_provider_spend_limit(clan, monkeypatch):
    cfg, paths = clan
    cfg.roles['developer'].harness = 'claude-p'
    H.write_configs(paths, cfg, 'developer', worktree='/tmp/wt')
    monkeypatch.setenv('CLAN_ROUND_BUDGET_USD', '1.25')
    cls = install_popen(monkeypatch, FakePopen)
    H.launch(paths, cfg, 'developer', worktree='/tmp/wt', unattended=True, stdin=io.StringIO())
    argv = cls.procs[0].argv
    assert argv[argv.index('--max-budget-usd') + 1] == '1.25'


def test_launch_deadline_kills_running_round_and_stops(clan, monkeypatch):
    cfg, paths = clan
    cfg.roles['developer'].harness = 'opencode-run'
    H.write_configs(paths, cfg, 'developer', worktree='/tmp/wt')
    monkeypatch.setenv('CLAN_MAX_SECONDS', '0.1')
    monkeypatch.setenv('CLAN_ROUND_TIMEOUT', '60')
    cls = install_popen(monkeypatch, HangingPopen)
    H.launch(paths, cfg, 'developer', worktree='/tmp/wt', unattended=True,
             stdin=io.StringIO('retry\nretry\n'))
    assert len(cls.procs) == 1 and cls.procs[0].killed
    assert C.read_state(paths)['runs']['developer']['reason'] == 'max_seconds'


def test_checkpoint_does_not_reset_launch_budget(clan, monkeypatch):
    cfg, paths = clan
    cfg.roles['developer'].harness = 'opencode-run'
    hd = H.write_configs(paths, cfg, 'developer', worktree='/tmp/wt')
    monkeypatch.setenv('CLAN_MAX_ROUNDS', '2')
    class Checkpoint(FakePopen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            (hd / 'reset').touch()
    cls = install_popen(monkeypatch, Checkpoint)
    H.launch(paths, cfg, 'developer', worktree='/tmp/wt', unattended=True,
             stdin=io.StringIO('next\nnext\nnext\n'))
    assert len(cls.procs) == 2
    records = [json.loads(line) for line in (hd / 'rounds.jsonl').read_text().splitlines()]
    assert [r['round'] for r in records] == [1, 1]
    assert C.read_state(paths)['runs']['developer']['reason'] == 'max_rounds'
