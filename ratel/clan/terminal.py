"""Terminal backend contract and operator preferences (independent of harnesses)."""
from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path
from typing import Protocol

TerminalId = int | str
TERMINAL_ERRORS = (ValueError, OSError, subprocess.TimeoutExpired)
BACKENDS = ("herdr", "zellij")
OBSERVATION_TTL_S = 10


class Terminal(Protocol):
    session: str

    def new_tab(self, name: str, cwd: str | Path, argv: list[str]) -> TerminalId: ...
    def pane_or_none(self, name: str, timeout_s: float = 5) -> TerminalId | None: ...
    def live_sessions(self) -> set[str]: ...
    def nudge(self, pane: TerminalId, text: str) -> None: ...
    def dump_screen(self, pane: TerminalId, full: bool = False) -> str: ...
    def kill(self) -> None: ...


def preference(home: Path, override: str | None = None) -> str:
    if override is not None:
        backend = override
    else:
        path = Path(home).expanduser() / "config.toml"
        doc = tomllib.loads(path.read_text()) if path.exists() else {}
        settings = doc.get("terminal", {})
        if not isinstance(settings, dict):
            raise ValueError("config.toml [terminal] must be a table")
        backend = settings.get("backend", "herdr")
    if backend not in BACKENDS:
        raise ValueError("terminal backend must be herdr or zellij")
    return backend


def backend_name(state: dict) -> str:
    backend = state.get("terminal_backend", "zellij")
    if backend not in BACKENDS:
        raise ValueError("unknown recorded terminal backend")
    return backend


def terminal_id(value) -> TerminalId | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value and len(value) <= 128 and all(
            c.isascii() and (c.isalnum() or c in ":_-.") for c in value):
        return value
    return None


def publish_lifecycle(paths, role: str, status: str) -> None:
    """Authoritative headless lifecycle, separate from terminal heuristics."""
    from ..bus import now_iso
    from .config import read_state, update_state
    if backend_name(read_state(paths)) != "herdr":
        return
    update_state(paths, lambda s: s.setdefault("terminal_lifecycle", {}).__setitem__(
        role, {"state": status, "at": now_iso()}))


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
    if k in ZELLIJ_VARS or k in CLAUDE_IDENTITY_VARS or k.startswith("HERDR_"):
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

