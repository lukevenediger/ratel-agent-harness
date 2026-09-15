"""Thin wrapper over the `zellij` CLI: one session per clan, one tab per role.

Flags here are the ones zellij 0.44.1 actually accepts, pinned by the Task 0
spike: `list-panes` has no `--tab` (it returns a flat pane list carrying
tab_position), `dump-screen` takes `--path` rather than a positional path, and
a test server is isolated by TMPDIR + HOME — ZELLIJ_SOCKET_DIR does nothing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .terminal import scrubbed_env as scrubbed_env
from .tools import require_binary

# Every failure a live zellij subprocess can produce: non-zero exit (ValueError
# from _action), a vanished binary/socket (OSError), a hung server (TimeoutExpired).
ZELLIJ_ERRORS = (ValueError, OSError, subprocess.TimeoutExpired)

CONFIG_KDL = 'session_serialization false\nshow_startup_tips false\ndefault_shell "/bin/sh"\n'

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


SOCKET_PATH_MAX = 103          # a unix socket path caps here; measured on zellij 0.44.1
SESSION_STAMPS = ("%m%d-%H%M", "%H%M")   # tried in order: our stamp is the cheapest thing to lose
MIN_CHANNEL_CHARS = 4          # below this a truncated name says nothing
SESSION_TRIES = 100


_CS_DARWIN_USER_TEMP_DIR = 65537   # confstr(3): the per-user /var/folders/.../T dir


def _darwin_temp_dir() -> str:
    """macOS's per-user temp dir, which is what `$TMPDIR` normally holds."""
    if sys.platform != "darwin":
        return ""
    try:
        return os.confstr(_CS_DARWIN_USER_TEMP_DIR) or ""
    except (ValueError, OSError):
        return ""


def _socket_prefix(tmp: str) -> int:
    return len(f"{tmp.rstrip('/')}/zellij-{os.getuid()}/contract_version_1/".encode())


def session_name_budget(env: dict[str, str] | None = None) -> int:
    """How many characters a zellij session name may have on this machine.

    zellij binds its IPC socket at `$TMPDIR/zellij-<uid>/contract_version_1/<session>`,
    and a unix socket path caps at 103 bytes. On macOS `$TMPDIR` alone is ~49
    bytes, which leaves about 24 characters for the whole session name — a
    stamped `<repo>-<issue>` name overruns it and zellij refuses to start.

    `TMPDIR` set is taken at its word: zellij uses what it is given, and a
    short one must not be second-guessed into shortening names for nothing.
    Unset, we cannot see what zellij will resolve — Python would say `/tmp`
    while zellij's process may well have the 49-byte per-user dir — so budget
    for the longest it could be. `budget_from_error` corrects either way.
    """
    env = os.environ if env is None else env
    tmp = env.get("TMPDIR")
    if tmp:
        return SOCKET_PATH_MAX - _socket_prefix(tmp)
    return SOCKET_PATH_MAX - max(_socket_prefix(d) for d in ("/tmp", _darwin_temp_dir() or "/tmp"))


SOCKET_TOO_LONG = re.compile(r"socket path is too long \((\d+) bytes, max (\d+)\)")


def budget_from_error(message: str, name: str) -> int | None:
    """The exact budget, taken from zellij's own arithmetic in its refusal.

    Predicting the socket path means guessing at zellij's layout and at what
    `$TMPDIR` its process sees. When it refuses it states both numbers, and
    subtracting the name we sent gives the prefix it actually used. None when
    the message is some other failure.
    """
    m = SOCKET_TOO_LONG.search(message)
    if not m:
        return None
    used, cap = int(m.group(1)), int(m.group(2))
    return cap - (used - len(name.encode()))


def _fit(channel: str, suffix: str, budget: int) -> str | None:
    """`channel + suffix`, trimming the channel's HEAD when the pair is over
    budget. The head is what goes: the tail carries the issue number, which is
    what tells two clans on one repo apart. None when nothing legible fits."""
    room = budget - len(suffix)
    if room < MIN_CHANNEL_CHARS:
        return None
    return (channel if len(channel) <= room else channel[-room:]) + suffix


def unique_session(channel: str, taken: Callable[[str], bool],
                   now: datetime | None = None, budget: int | None = None) -> str:
    """A zellij session name for `channel` that nothing on the server holds.

    The channel keeps its stable `<repo>-<issue>` name — it is the bus, the
    board's identity and what `--channel` addresses — while the zellij session
    it runs in is stamped with the local start time. They are no longer the
    same string: a clan that exited still owns its session name (zellij lists
    EXITED sessions for `attach` to resurrect), and a restart on the same issue
    must not be refused because its predecessor is still listed.

    The name must also fit `session_name_budget()`. Order of sacrifice: the
    stamp's date first (`0910-2041` → `2041`), the channel's head last.
    """
    now = now or datetime.now()
    budget = session_name_budget() if budget is None else budget
    stamps = [now.strftime(f) for f in SESSION_STAMPS]
    for i, stamp in enumerate(stamps):
        whole = len(channel) + 1 + len(stamp) <= budget
        if not whole and i < len(stamps) - 1:
            continue               # a shorter stamp beats a truncated channel
        for n in range(1, SESSION_TRIES + 1):
            suffix = f"-{stamp}" if n == 1 else f"-{stamp}-{n}"
            name = _fit(channel, suffix, budget)
            if name is None:
                break
            if not taken(name):
                if name != f"{channel}{suffix}":
                    print(f"ratel clan: session name shortened to {name!r} — "
                          f"a zellij socket path caps at {SOCKET_PATH_MAX} bytes",
                          file=sys.stderr)
                return name
    if _fit(channel, f"-{stamps[-1]}", budget) is None:
        sys.exit(f"ratel clan new: $TMPDIR is too long to name a session under it "
                 f"({budget} characters left of the {SOCKET_PATH_MAX}-byte socket path "
                 f"cap) — set a shorter TMPDIR")
    sys.exit(f"ratel clan new: no free session name after {SESSION_TRIES} tries "
             f"for channel {channel!r} — `zellij list-sessions` and clean up")


def create_session(channel: str, env: dict, factory=Zellij) -> Zellij:
    held = factory(channel, env=env).sessions()
    name = unique_session(channel, held.__contains__, budget=session_name_budget(env))
    terminal = factory(name, env=env)
    try:
        terminal.create_background()
    except ValueError as exc:
        budget = budget_from_error(str(exc), name)
        if budget is None:
            raise
        name = unique_session(channel, held.__contains__, budget=budget)
        terminal = factory(name, env=env)
        terminal.create_background()
    return terminal
