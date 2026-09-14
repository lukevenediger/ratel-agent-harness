"""The zellij wrapper, against an injected runner — no real zellij server here.

Flags are pinned to what zellij 0.44.1 actually accepts (Task 0 spike): list-panes
has no --tab, dump-screen has no positional path, and isolation comes from TMPDIR,
not ZELLIJ_SOCKET_DIR.
"""
import json
import shutil
import subprocess

import pytest

from ratel.clan.zellij import Zellij, isolated_env, scrubbed_env

PANES = [
    {"id": 0, "is_plugin": True, "title": "zellij:tab-bar", "tab_id": 1, "tab_position": 1},
    {"id": 7, "is_plugin": False, "title": "cat", "tab_id": 1, "tab_position": 1},
    {"id": 9, "is_plugin": False, "title": "claude", "tab_id": 2, "tab_position": 2},
]


class FakeRun:
    """Records argv, replays canned results keyed by the zellij subcommand."""

    def __init__(self, **results):
        self.calls: list[list[str]] = []
        self.results = results

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        key = next((a for a in argv if a in self.results), None)
        out, rc = self.results.get(key, ("", 0)) if key else ("", 0)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    @property
    def argv(self):
        return self.calls[-1]


def zj(**results):
    run = FakeRun(**results)
    return Zellij("harbor-42", env={"TMPDIR": "/tmp/x/"}, run=run), run


def test_create_background():
    z, run = zj()
    z.create_background()
    assert run.argv == ["zellij", "attach", "--create-background", "harbor-42"]


# A real `zellij list-sessions --no-formatting` capture: liveness is only in
# the long form's `(EXITED - attach to resurrect)` suffix; `--short` drops it.
ZELLIJ_LONG_LISTING = (
    "implacable-accordion [Created 1month 26days 12h 24m ago] \n"
    "website-1-2204 [Created 18h 39m 8s ago] (EXITED - attach to resurrect)\n"
    "harbor-1-0911-0748 [Created 8h 55m 8s ago] (EXITED - attach to resurrect)\n"
    "harbor-42 [Created 50m 10s ago] (current)\n"
)


def test_live_sessions_excludes_exited_but_sessions_keeps_them():
    z, run = zj(**{"list-sessions": (ZELLIJ_LONG_LISTING, 0)})
    assert z.live_sessions() == {"implacable-accordion", "harbor-42"}
    assert run.argv == ["zellij", "list-sessions", "--no-formatting"]


def test_live_sessions_drops_this_clans_exited_session():
    z, _ = zj(**{"list-sessions": (
        "harbor-42 [Created 8h ago] (EXITED - attach to resurrect)\n", 0)})
    assert z.live_sessions() == set()


def test_live_sessions_is_empty_when_the_listing_fails():
    z, _ = zj(**{"list-sessions": ("", 1)})
    assert z.live_sessions() == set()


def test_new_tab_argv_and_returns_the_tab_id():
    z, run = zj(**{"new-tab": ("3\n", 0)})
    assert z.new_tab("developer", "/repo/wt", ["python", "-m", "ratel.cli", "clan", "launch"]) == 3
    assert run.argv == ["zellij", "--session", "harbor-42", "action", "new-tab",
                        "--name", "developer", "--cwd", "/repo/wt",
                        "--", "python", "-m", "ratel.cli", "clan", "launch"]


def test_list_panes_and_tab_names():
    z, run = zj(**{"list-panes": (json.dumps(PANES), 0), "query-tab-names": ("Tab #1\ndeveloper\n", 0)})
    assert z.list_panes() == PANES
    assert run.calls[0] == ["zellij", "--session", "harbor-42", "action", "list-panes", "-j"]
    assert z.tab_names() == ["Tab #1", "developer"]


def test_list_panes_tolerates_a_whitespace_only_body():
    """A warming server once answered `\\n` (measured, issue #7 polish round):
    falsy-guarding alone would let it reach json.loads and raise."""
    z, _ = zj(**{"list-panes": ("\n", 0)})
    assert z.list_panes() == []


def test_pane_for_tab_skips_plugin_panes():
    z, _ = zj(**{"list-panes": (json.dumps(PANES), 0),
                 "query-tab-names": ("orchestrator\ndeveloper\nreviewer\n", 0)})
    assert z.pane_for_tab("developer") == 7    # position 1: pane 0 is a plugin, 7 is the terminal
    assert z.pane_for_tab("reviewer") == 9


def test_pane_for_tab_polls_then_times_out():
    z, run = zj(**{"list-panes": (json.dumps([PANES[0]]), 0), "query-tab-names": ("x\ndeveloper\n", 0)})
    with pytest.raises(TimeoutError, match="developer"):
        z.pane_for_tab("developer", timeout_s=0.2, poll_s=0.05)
    assert len([c for c in run.calls if "list-panes" in c]) > 1   # it really polled


def test_pane_for_tab_rejects_an_unknown_tab():
    z, _ = zj(**{"list-panes": (json.dumps(PANES), 0), "query-tab-names": ("orchestrator\n", 0)})
    with pytest.raises(TimeoutError, match="developer"):
        z.pane_for_tab("developer", timeout_s=0.1, poll_s=0.05)


def test_nudge_writes_the_line_then_a_carriage_return():
    z, run = zj()
    z.nudge(7, "ratel: @developer 1 new — call catch_up now.")
    assert run.calls[-2] == ["zellij", "--session", "harbor-42", "action", "write-chars",
                             "-p", "7", "ratel: @developer 1 new — call catch_up now."]
    assert run.calls[-1] == ["zellij", "--session", "harbor-42", "action", "write", "-p", "7", "13"]


def test_dump_screen_returns_stdout_and_can_ask_for_scrollback():
    z, run = zj(**{"dump-screen": ("hello\n", 0)})
    assert z.dump_screen(7) == "hello\n"
    assert run.argv == ["zellij", "--session", "harbor-42", "action", "dump-screen", "-p", "7"]
    z.dump_screen(7, full=True)
    assert run.argv[-1] == "-f"


def test_close_tab_by_id_does_not_move_the_human_focus():
    z, run = zj()
    z.close_tab(3)
    assert run.calls == [["zellij", "--session", "harbor-42", "action", "close-tab", "-t", "3"]]


def test_go_to_tab_moves_focus_without_failing_on_a_missing_name():
    z, run = zj()
    z.go_to_tab("orchestrator")
    assert run.calls == [["zellij", "--session", "harbor-42", "action",
                          "go-to-tab-name", "orchestrator"]]


def test_kill():
    z, run = zj()
    z.kill()
    assert run.argv == ["zellij", "delete-session", "--force", "harbor-42"]


def test_a_failing_command_raises_with_stderr():
    class Failing(FakeRun):
        def __call__(self, argv, **kw):
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="no such session")
    z = Zellij("gone", run=Failing())
    with pytest.raises(ValueError, match="no such session"):
        z.new_tab("developer", "/tmp", ["cat"])


def test_run_is_called_without_a_tty_and_with_our_env():
    z, run = zj(**{"list-panes": ("[]", 0)})
    captured = {}
    z.run = lambda argv, **kw: captured.update(kw) or subprocess.CompletedProcess(argv, 0, "[]", "")
    z.list_panes()
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["env"] == {"TMPDIR": "/tmp/x/"}
    assert captured["capture_output"] and captured["text"]


def test_scrubbed_env_keeps_the_real_home_so_tabs_keep_their_credentials(tmp_path):
    """Production: claude and opencode read ~/.claude and ~/.config/opencode for auth."""
    parent = {"PATH": "/bin", "ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "live", "ZELLIJ_PANE_ID": "33",
              "HOME": "/home/real", "TMPDIR": "/var/t/"}
    env = scrubbed_env(parent=parent)
    assert env == {"PATH": "/bin", "HOME": "/home/real", "TMPDIR": "/var/t/"}


def test_scrubbed_env_drops_the_parent_claude_session_identity():
    """A clan zellij server born inside a Claude session leaks CLAUDECODE /
    CLAUDE_CODE_* into every role tab, making them child sessions that never
    write their own transcripts (measured on this live clan). CLAUDE_EFFORT
    is a preference, not identity — it stays."""
    parent = {"PATH": "/bin", "HOME": "/home/real", "CLAUDECODE": "1",
              "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_SESSION_ID": "u-1",
              "CLAUDE_CODE_MESSAGING_SOCKET": "s", "CLAUDE_CODE_MESSAGING_TOKEN": "t",
              "CLAUDE_CODE_BRIDGE_SESSION_ID": "b", "CLAUDE_PID": "9",
              "CLAUDE_EFFORT": "high", "ZELLIJ_PANE_ID": "7"}
    env = scrubbed_env(parent=parent)
    assert env == {"PATH": "/bin", "HOME": "/home/real", "CLAUDE_EFFORT": "high"}


def test_scrubbed_env_keeps_claude_configuration_not_identity():
    """The CLAUDE_CODE_* family also holds provider config a clan may need:
    Bedrock/Vertex routing and output limits survive; session/messaging
    identity does not (any variant spelling of those four leaks is caught)."""
    parent = {"PATH": "/bin", "HOME": "/home/real",
              "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1",
              "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
              "CLAUDE_CODE_ENTRYPOINT": "sdk", "CLAUDE_CODE_EXECPATH": "/x",
              "CLAUDE_CODE_SESSION_ID": "u-1", "CLAUDE_CODE_SOMETHING_SESSION": "x",
              "CLAUDE_CODE_SOMETHING_MESSAGING": "x", "CLAUDE_CODE_SOMETHING_CHILD": "x",
              "CLAUDE_CODE_SOMETHING_PID": "x"}
    env = scrubbed_env(parent=parent)
    assert env == {"PATH": "/bin", "HOME": "/home/real",
                   "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1",
                   "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
                   "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000"}


def test_isolated_env_moves_the_server_via_tmpdir_not_socket_dir(tmp_path):
    parent = {"PATH": "/bin", "ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "live", "ZELLIJ_PANE_ID": "33",
              "HOME": "/home/real"}
    env = isolated_env(tmp_path, parent=parent)
    assert not any(k.startswith("ZELLIJ") for k in env if k != "ZELLIJ_CONFIG_FILE")
    assert env["TMPDIR"] == str(tmp_path) + "/"          # trailing slash: zellij joins onto it
    assert env["HOME"] == "/home/real"                  # kept real: nested roles need logins
    assert env["PATH"] == "/bin"
    cfg = env["ZELLIJ_CONFIG_FILE"]
    assert "session_serialization false" in open(cfg).read()
    assert "show_startup_tips false" in open(cfg).read()


def test_isolated_env_defaults_to_the_real_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "live")
    monkeypatch.setenv("CLAN_MARKER", "keep-me")
    env = isolated_env(tmp_path)
    assert "ZELLIJ_SESSION_NAME" not in env and env["CLAN_MARKER"] == "keep-me"


def test_pane_or_none_tolerates_a_slow_tab_and_propagates_a_dead_server():
    z, _ = zj(**{"query-tab-names": ("", 0)})
    assert z.pane_or_none("developer", timeout_s=0.05) is None
    z2, _ = zj(**{"query-tab-names": ("developer\n", 0), "list-panes": ("boom", 2)})
    with pytest.raises(ValueError):        # a dead server keeps its own message
        z2.pane_or_none("developer")


def test_a_missing_zellij_binary_is_a_clear_error(monkeypatch):
    """Without zellij the clan must say so, not raise FileNotFoundError from
    inside the subprocess call."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    z = Zellij("harbor-42")
    with pytest.raises(SystemExit, match="zellij.*not on PATH"):
        z.sessions()
