"""HerdR 0.9 CLI adapter. One owned workspace per clan, shared server per home.

Terminal IDs and launch PIDs pin identity across moves. Restored shells and
replacement agents never inherit permission to receive a previous launch's input.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import time
from pathlib import Path

from ..bus import now_iso
from ..paths import atomic_write
from .config import ClanPaths, read_state, update_state
from .terminal import TERMINAL_ERRORS, scrubbed_env
from .tools import require_binary


class HerdrError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


CONFIG = '''onboarding = false
[terminal]
default_shell = "/bin/sh"
shell_mode = "non_login"
[session]
resume_agents_on_restore = false
[update]
version_check = false
manifest_check = false
'''


class Herdr:
    agent_aware = True

    def __init__(self, paths: ClanPaths, state: dict | None = None, run=subprocess.run):
        self.paths = paths
        self.state = state if state is not None else read_state(paths)
        digest = hashlib.sha256(str(paths.home).encode()).hexdigest()[:12]
        self.session = f"ratel-{digest}"
        if self.state.get("session", self.session) != self.session:
            raise ValueError("HerdR session does not belong to this RATEL_HOME")
        self.workspace = self.state.get("workspace_id")
        self.run = run
        self.runtime = paths.home / "terminal" / "herdr"
        # Short socket paths also work on macOS with long per-test home paths.
        self.socket_root = Path(f"/tmp/ratel-herdr-{os.getuid()}")
        self.env = {**scrubbed_env(), "XDG_CONFIG_HOME": str(self.socket_root),
                    "XDG_STATE_HOME": str(self.runtime / "state"),
                    "HERDR_CONFIG_PATH": str(self.runtime / "config.toml")}
        self.created: dict[str, dict] = {}

    def command(self, *args: str) -> list[str]:
        return ["herdr", "--session", self.session, *args]

    def call(self, *args: str, raw: bool = False):
        require_binary("herdr", "this clan (or select --terminal zellij for a new clan)")
        result = self.run(self.command(*args), env=self.env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=5)
        if result.returncode:
            # CLI errors can include terminal content; callers decide whether to expose them.
            try:
                error = json.loads(result.stderr).get("error", {})
            except (ValueError, AttributeError):
                error = {}
            raise HerdrError(error.get("code", "cli_error"),
                             f"HerdR {' '.join(args[:2])} failed: {result.stderr.strip()}")
        if raw:
            return result.stdout
        if not result.stdout.strip():
            return {}
        doc = json.loads(result.stdout)
        if not isinstance(doc, dict) or "error" in doc or not isinstance(doc.get("result"), dict):
            raise ValueError("invalid HerdR response")
        required = {
            ("pane", "list"): ("panes", list),
            ("workspace", "list"): ("workspaces", list),
            ("pane", "process-info"): ("process_info", dict),
            ("agent", "get"): ("agent", dict),
            ("workspace", "create"): ("root_pane", dict),
            ("tab", "create"): ("root_pane", dict),
        }.get(args[:2])
        if required and not isinstance(doc["result"].get(required[0]), required[1]):
            raise ValueError("incompatible HerdR response; expected " + required[0])
        return doc["result"]

    def attach_command(self) -> str:
        return shlex.join(["env", f"XDG_CONFIG_HOME={self.socket_root}",
                           f"XDG_STATE_HOME={self.runtime / 'state'}",
                           f"HERDR_CONFIG_PATH={self.runtime / 'config.toml'}",
                           *self.command()])

    def server_process_alive(self) -> bool:
        from .harness import _ps_lstart
        record = self.runtime / "server.json"
        if not record.exists():
            return False
        try:
            doc = json.loads(record.read_text())
            return bool(doc.get("started")) and _ps_lstart(doc["pid"]) == doc["started"]
        except (ValueError, KeyError, TypeError):
            raise ValueError("invalid HerdR server identity record") from None

    def ensure_server(self) -> None:
        require_binary("herdr", "new clans (or pass --terminal zellij)")
        version = self.run(["herdr", "--version"], capture_output=True, text=True,
                           env=self.env, timeout=5, check=True).stdout.strip()
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (0, 9, 0):
            raise ValueError("HerdR 0.9.0 or newer is required; or use --terminal zellij")
        self.socket_root.mkdir(mode=0o700, exist_ok=True)
        info = self.socket_root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("HerdR socket directory must be an owned private directory")
        self.runtime.mkdir(parents=True, exist_ok=True)
        with (self.runtime / "server.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            atomic_write(self.runtime / "config.toml", CONFIG)
            try:
                self.call("workspace", "list")
                return
            except TERMINAL_ERRORS:
                pass
            if self.server_process_alive():
                raise ValueError("HerdR server is alive but unreachable; restore its socket before retrying")
            with (self.runtime / "server.log").open("a") as log:
                process = subprocess.Popen(self.command("server"), env=self.env,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           start_new_session=True)
            from .harness import _ps_lstart
            atomic_write(self.runtime / "server.json", json.dumps(
                {"pid": process.pid, "started": _ps_lstart(process.pid)}))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    self.call("workspace", "list")
                    return
                except TERMINAL_ERRORS:
                    if process.poll() is not None:
                        break
                    time.sleep(.1)
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            raise ValueError(f"HerdR failed to start; inspect {self.runtime / 'server.log'}")

    def create_background(self, cwd: Path) -> None:
        self.ensure_server()
        result = self.call("workspace", "create", "--cwd", str(cwd),
                           "--label", self.paths.channel, "--no-focus")
        self.workspace = result["workspace"]["workspace_id"]
        self.root = result

    def live_sessions(self) -> set[str]:
        workspaces = self.call("workspace", "list")["workspaces"]
        return {self.session} if any(w["workspace_id"] == self.workspace for w in workspaces) else set()

    def new_tab(self, name: str, cwd: str | Path, argv: list[str]) -> str:
        if not self.workspace:
            raise ValueError("no owned HerdR workspace")
        result = getattr(self, "root", None)
        if result is not None:
            del self.root
            self.call("tab", "rename", result["tab"]["tab_id"], name)
        else:
            result = self.call("tab", "create", "--workspace", self.workspace,
                               "--cwd", str(cwd), "--label", name, "--no-focus")
        pane = result["root_pane"]
        info = {"tab_id": pane["tab_id"], "pane_id": pane["pane_id"],
                "terminal_id": pane["terminal_id"], "cwd": str(cwd)}
        if name not in ("orchestrator", "watch", "bus"):
            info["worktree"] = str(cwd)
        self.created[name] = info
        # Persist identity before the child starts. launch() merges its PID later.
        update_state(self.paths, lambda s: s.setdefault("tabs", {}).setdefault(name, {}).update(info))
        # Server-only XDG isolation must not hide the agent's provider configuration.
        agent_env = {"XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")),
                     "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))}
        command = "exec " + shlex.join(["env", *[f"{k}={v}" for k, v in agent_env.items()], *argv])
        self.call("pane", "report-metadata", pane["pane_id"], "--source", "custom:ratel-display",
                  "--display-agent", f"{self.paths.channel}/{name}", "--token", f"role={name}")
        self.call("pane", "run", pane["pane_id"], command)
        return pane["tab_id"]

    def pane_or_none(self, name: str, timeout_s: float = 5) -> str | None:
        tab = self.created.get(name) or read_state(self.paths).get("tabs", {}).get(name, {})
        if not tab.get("terminal_id"):
            return None
        matches = [p for p in self.call("pane", "list")["panes"]
                   if p["terminal_id"] == tab["terminal_id"]]
        return matches[0]["pane_id"] if len(matches) == 1 else None

    def resolve(self, pane: str) -> tuple[str, dict]:
        state = read_state(self.paths)
        matches = [(r, t) for r, t in state.get("tabs", {}).items() if t.get("pane_id") == pane]
        if len(matches) != 1:
            raise ValueError("unowned or ambiguous HerdR pane")
        role, tab = matches[0]
        current = self.pane_or_none(role)
        if current is None:
            raise ValueError("terminal gone; run clan down, then clan new to recover")
        processes = self.call("pane", "process-info", "--pane", current)["process_info"]
        # PID plus OS process start time prevents PID reuse after a cold restart.
        from .harness import _ps_lstart
        pid = tab.get("launch_pid")
        if not pid or not tab.get("launch_started") or _ps_lstart(pid) != tab.get("launch_started") or not any(
                p["pid"] == pid for p in processes.get("foreground_processes", [])):
            raise ValueError("launch lost or replaced; run clan down, then clan new to recover")
        return current, tab

    def observe(self, pane: str) -> dict:
        try:
            current, tab = self.resolve(pane)
            if tab.get("harness") in ("claude-p", "opencode-run", "fake"):
                state = read_state(self.paths)
                life = state.get("terminal_lifecycle", {}).get(next(
                    r for r, t in state["tabs"].items() if t.get("pane_id") == pane), {})
                status = life.get("state", "unknown")
            else:
                agent = self.call("agent", "get", current)["agent"]
                expected = tab.get("harness")
                status = agent["agent_status"] if agent.get("agent") == expected else "unknown"
                if status in ("idle", "done") and agent.get("launch_pending", False):
                    status = "unknown"
            return {"state": status, "at": now_iso(), "pane_id": current, "source": "herdr"}
        except TERMINAL_ERRORS:
            return {"state": "unavailable", "at": now_iso(), "source": "herdr"}

    def report_observation(self, pane: str, observation: dict) -> None:
        """Watcher-only display updates; observing and doctor remain read-only."""
        if observation["state"] == "unavailable":
            return
        current, tab = self.resolve(pane)
        if tab.get("harness") in ("claude-p", "opencode-run", "fake"):
            status = observation["state"]
            self.call("pane", "report-agent", current, "--source", "custom:ratel",
                      "--agent", "ratel", "--state", status if status in (
                          "idle", "working", "blocked") else "unknown")

    def nudge(self, pane: str, text: str) -> None:
        observation = self.observe(pane)
        if observation["state"] not in ("idle", "done"):
            raise ValueError("agent is not ready; nudge retained for retry")
        current, tab = self.resolve(pane)
        if tab.get("harness") in ("claude-p", "opencode-run", "fake"):
            self.call("pane", "send-text", current, text)
            self.call("pane", "send-keys", current, "enter")
        else:
            self.call("agent", "prompt", current, text)

    def dump_screen(self, pane: str, full: bool = False) -> str:
        current, _ = self.resolve(pane)
        return self.call("pane", "read", current, "--source", "recent" if full else "detection", "--raw",
                         raw=True)

    def kill(self) -> None:
        # Include owned terminals moved elsewhere, never delete another workspace.
        state = read_state(self.paths)
        owned = {t.get("terminal_id") for t in state.get("tabs", {}).values()} - {None}
        try:
            panes = self.call("pane", "list")["panes"]
        except HerdrError as exc:
            if exc.code != "server_not_running" or self.server_process_alive():
                raise
            # The CLI explicitly confirmed no listening server, so no live panes
            # remain. session.down still reaps every recorded headless process group.
            return
        for pane in panes:
            if pane["terminal_id"] in owned and pane["workspace_id"] != self.workspace:
                self.call("pane", "close", pane["pane_id"])
        # Avoid removing a foreign pane manually moved into our workspace.
        foreign = [p for p in panes if p["workspace_id"] == self.workspace and p["terminal_id"] not in owned]
        if foreign:
            for pane in panes:
                if pane["workspace_id"] == self.workspace and pane["terminal_id"] in owned:
                    self.call("pane", "close", pane["pane_id"])
        elif self.session in self.live_sessions():
            self.call("workspace", "close", self.workspace)
