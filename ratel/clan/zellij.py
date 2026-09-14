"""Thin wrapper over the `zellij` CLI: one session per clan, one tab per role.

Flags here are the ones zellij 0.44.1 actually accepts, pinned by the Task 0
spike: `list-panes` has no `--tab` (it returns a flat pane list carrying
tab_position), `dump-screen` takes `--path` rather than a positional path, and
a test server is isolated by TMPDIR + HOME — ZELLIJ_SOCKET_DIR does nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .tools import require_binary

# Every failure a live zellij subprocess can produce: non-zero exit (ValueError
# from _action), a vanished binary/socket (OSError), a hung server (TimeoutExpired).
ZELLIJ_ERRORS = (ValueError, OSError, subprocess.TimeoutExpired)

CONFIG_KDL = 'session_serialization false\nshow_startup_tips false\ndefault_shell "/bin/sh"\n'
ZELLIJ_VARS = ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID")
CLAUDE_IDENTITY_VARS = ("CLAUDECODE", "CLAUDE_PID")
# The CLAUDE_CODE_* members that mark a child session. The family also holds
# provider configuration a tab may need — USE_BEDROCK / USE_VERTEX /
# SKIP_*_AUTH / MAX_OUTPUT_TOKENS survive — so identity is dropped by name and
# by any *other* CLAUDE_CODE_ var carrying one of these leak words.
_CLAUDE_CODE_IDENTITY = {"CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                         "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
                         "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT",
                         "CLAUDE_CODE_EXECPATH"}
_CLAUDE_CODE_LEAK_WORDS = ("SESSION", "MESSAGING", "CHILD", "PID")


def _is_parent_identity(k: str) -> bool:
    """Zellij vars would address the wrong server; the Claude identity vars
    mark the tab as a child of whoever launched the clan, so it never writes
    its own transcript. HOME stays: it is auth. Provider configuration inside
    CLAUDE_CODE_* stays too — it is not identity."""
    if k in ZELLIJ_VARS or k in CLAUDE_IDENTITY_VARS:
        return True
    if not k.startswith("CLAUDE_CODE_"):
        return False
    return k in _CLAUDE_CODE_IDENTITY or any(w in k for w in _CLAUDE_CODE_LEAK_WORDS)


def scrubbed_env(parent: dict[str, str] | None = None) -> dict[str, str]:
    """Production: drop this pane's identity, keep everything else.

    A clan launched from inside a zellij pane must not inherit that pane's
    ZELLIJ_* vars, or the new session's commands address the old server — and
    one launched from inside a Claude session must not inherit its parent's
    session identity, or the role tabs are child sessions with no transcripts
    of their own (measured on a live clan). HOME stays real — claude and
    opencode read ~/.claude and ~/.config/opencode for their credentials, and
    a tab without them is a tab that cannot start.
    """
    return {k: v for k, v in (parent if parent is not None else os.environ).items()
            if not _is_parent_identity(k)}


def isolated_env(tmp: Path | str, parent: dict[str, str] | None = None) -> dict[str, str]:
    """Tests: a zellij server nobody else can see.

    Sockets live under $TMPDIR/zellij-<uid>/, so the TMPDIR moves and stays
    SHORT — the socket path is capped at 103 bytes and pytest's tmp_path blows
    through it. HOME deliberately stays real: the nested clan's roles need real
    credentials (gh, claude, opencode), and with `session_serialization false`
    plus `delete-session --force` nothing lands in ~/.cache/zellij or
    ~/.config/zellij (measured). ZELLIJ_SOCKET_DIR is ignored by zellij 0.44.1
    and is deliberately not used.
    """
    tmp = Path(tmp)
    env = scrubbed_env(parent)
    cfg = tmp / "zellij-config.kdl"
    if not cfg.exists():
        cfg.write_text(CONFIG_KDL)
    env["TMPDIR"] = str(tmp) + "/"     # zellij joins onto this; the slash matters
    env["ZELLIJ_CONFIG_FILE"] = str(cfg)
    return env


class Zellij:
    def __init__(self, session: str, env: dict[str, str] | None = None,
                 run: Callable[..., Any] = subprocess.run):
        self.session = session
        self.env = env
        self.run = run

    # ---- plumbing ------------------------------------------------------
    def _zellij(self, *args: str, check: bool = True) -> str:
        require_binary("zellij", "the clan layer")
        p = self.run(["zellij", *args], capture_output=True, text=True,
                     stdin=subprocess.DEVNULL, env=self.env, timeout=30)
        if check and p.returncode != 0:
            raise ValueError(f"zellij {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
        return p.stdout

    def _action(self, *args: str, check: bool = True) -> str:
        return self._zellij("--session", self.session, "action", *args, check=check)

    # ---- session -------------------------------------------------------
    def sessions(self) -> set[str]:
        """Every session name the server lists — EXITED ones included. zellij
        keeps an exited session around for `attach` to resurrect, so its name
        stays taken long after the clan behind it is gone."""
        out = self._zellij("list-sessions", "--no-formatting", "--short", check=False)
        return set(out.split())

    def live_sessions(self) -> set[str]:
        """The sessions that are running, not merely listed. `sessions()` must
        keep EXITED names (they are still taken for `clan new`'s collision
        check), but an action against an EXITED session does not work — `up`
        asks this. Liveness is only in the long listing's `(EXITED ...)` suffix;
        `--short` discards it."""
        out = self._zellij("list-sessions", "--no-formatting", check=False)
        live = set()
        for line in out.splitlines():
            name, _, rest = line.partition(" ")
            if name and "(EXITED" not in rest:
                live.add(name)
        return live

    def create_background(self) -> None:
        """Detached session: `clan new` runs from a script, the human attaches later."""
        self._zellij("attach", "--create-background", self.session)

    def kill(self) -> None:
        self._zellij("delete-session", "--force", self.session, check=False)

    # ---- tabs and panes ------------------------------------------------
    def new_tab(self, name: str, cwd: str | Path, argv: list[str]) -> int:
        """Returns the new tab's id, which zellij prints synchronously."""
        out = self._action("new-tab", "--name", name, "--cwd", str(cwd), "--", *argv)
        return int(out.strip())

    def list_panes(self) -> list[dict]:
        return json.loads(self._action("list-panes", "-j").strip() or "[]")

    def tab_names(self) -> list[str]:
        return self._action("query-tab-names").strip().splitlines()

    def pane_for_tab(self, name: str, timeout_s: float = 5.0, poll_s: float = 0.2) -> int:
        """The tab's terminal pane. Polls: a tab exists before its pane is ready."""
        deadline = time.monotonic() + timeout_s
        while True:
            names = self.tab_names()
            if name in names:
                pos = names.index(name)
                panes = [p for p in self.list_panes()
                         if not p.get("is_plugin") and p.get("tab_position") == pos]
                if panes:
                    return int(panes[0]["id"])
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no terminal pane for tab {name!r} in session {self.session}")
            time.sleep(poll_s)

    def pane_or_none(self, name: str, timeout_s: float = 5.0) -> int | None:
        """A pane can lag its tab on a slow server; the tab is open either way.
        Anything else — a dead session, a missing binary — propagates."""
        try:
            return self.pane_for_tab(name, timeout_s=timeout_s)
        except TimeoutError:
            return None

    def go_to_tab(self, name: str) -> None:
        self._action("go-to-tab-name", str(name), check=False)

    def close_tab(self, tab_id: int) -> None:
        """By id only: a bare `close-tab` acts on the focused tab — destructive with
        a human attached, inert on a detached session (measured, issue #7)."""
        self._action("close-tab", "-t", str(tab_id), check=False)

    # ---- typing into a pane --------------------------------------------
    def write_chars(self, pane: int, text: str) -> None:
        self._action("write-chars", "-p", str(pane), text)

    def press_enter(self, pane: int) -> None:
        self._action("write", "-p", str(pane), "13")

    def nudge(self, pane: int, text: str) -> None:
        """One line typed into an idle agent's prompt, then Enter."""
        self.write_chars(pane, text)
        self.press_enter(pane)

    def dump_screen(self, pane: int, full: bool = False) -> str:
        return self._action("dump-screen", "-p", str(pane), *(["-f"] if full else []))
