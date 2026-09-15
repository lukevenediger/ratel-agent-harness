"""Headless round supervision, bounded output capture, and optional streamed logs."""
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import timezone
from pathlib import Path
from typing import TextIO

from ..paths import confined
from ..ulid import ulid
from .budget import STOP_REASONS, Budget, Limits
from .config import ClanConfig, ClanPaths, update_state
from .loop import NudgeLoop
from .output import OutputTail
from .terminal import publish_lifecycle


def run(runtime, cfg: ClanConfig, role: str, hd: Path, channel_dir: Path,
                  env: dict[str, str], unattended: bool, stdin: TextIO | None,
                  paths: ClanPaths, project_dir: Path | None = None) -> None:
    """One subprocess per round: the kickoff, then one per nudge line.

    The child gets stdin from /dev/null — it must never see the pane tty the
    watcher types nudges into, or it eats the next round's line. stdout is
    pumped through to the pane as it arrives (the pane is the agent's face)
    while being buffered for the rounds.jsonl record; CLAN_ROUND_TIMEOUT
    seconds (default 3600) kill a wedged round and the loop moves on.
    """
    rounds_path = confined(hd, "rounds.jsonl")
    session_file = confined(hd, "opencode-session")
    timeout_s = float(os.environ.get("CLAN_ROUND_TIMEOUT") or runtime.DEFAULT_ROUND_TIMEOUT_S)
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("CLAN_ROUND_TIMEOUT must be a finite positive number")
    state = {"n": 0, "session_id": session_file.read_text().strip()
             if session_file.exists() else None}
    current: dict = {}
    budget = Budget(Limits.from_env(env, cfg.roles[role].harness), time.monotonic)

    def publish(reason=None):
        record = {**budget.snapshot(), 'reason': reason,
                  'at': runtime.datetime.now(timezone.utc).isoformat(timespec='seconds')}
        update_state(paths, lambda s: s.setdefault('runs', {}).__setitem__(role, record))
        if reason:
            print(f"ratel: {role} stopped: {STOP_REASONS[reason]}", file=sys.stderr)

    publish()

    def _kill_current() -> None:
        pid = current.get("pid")
        if pid:
            try:                                 # the round group, not just the child
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def _on_signal(signum, _frame) -> None:
        # `clan down` kills the pane; the round, in its own session, would
        # otherwise outlive it. Take the group with us.
        _kill_current()
        sys.exit(f"ratel clan launch: round killed by signal {signum}")

    previous_handlers = {}
    for sig in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, sig):
            number = getattr(signal, sig)
            previous_handlers[number] = signal.signal(number, _on_signal)

    def run_round(prompt: str):
        if reason := budget.reason():
            publish(reason)
            return False
        publish_lifecycle(paths, role, "working")
        budget.rounds += 1
        reset = False
        if confined(hd, "reset").exists():          # `clan checkpoint` left a rewind marker
            confined(hd, "reset").unlink()
            reset = True
            state["n"] = 0                   # this round launches as round 1: no --continue / -s
            state["session_id"] = None
            session_file.unlink(missing_ok=True)
        state["n"] += 1
        round_start = time.monotonic()
        logged: list[str] = []
        out_lines = OutputTail(lines=runtime.ROUND_OUTPUT_LINES)
        err_lines = OutputTail(lines=runtime.ROUND_OUTPUT_LINES)
        session_probe = OutputTail(lines=4, characters=8192)
        captured_session = {"id": None}
        full_logs = {}
        log_files = {}
        timed_out = False
        error: str | None = None
        prompt_line: list[str] = []                 # first interactive-prompt line, if any
        prompt_hit = threading.Event()
        auth_hit = threading.Event()
        pump_failed = threading.Event()
        candidate: list[str] = []                   # last marker line, cleared by any output
        last_output = {"t": time.monotonic()}

        def pump(stream, kind) -> None:
            tail = out_lines if kind == "stdout" else err_lines
            terminal = sys.stdout if kind == "stdout" else sys.stderr
            try:
                for line in iter(lambda: stream.readline(4096), ""):
                    if kind == "stdout":
                        session_probe.append(line)
                        found = runtime.extract_opencode_session(session_probe.text())
                        captured_session["id"] = found or captured_session["id"]
                    last_output["t"] = time.monotonic()
                    tail.append(line)
                    if kind in log_files:
                        log_files[kind].write(line)
                        log_files[kind].flush()
                    terminal.write(line)
                    terminal.flush()
                    if time.monotonic() - round_start <= runtime.PROMPT_WINDOW_S:
                        if any(m in line for m in runtime.PROMPT_MARKERS):
                            if not candidate:
                                candidate[:] = [line.strip()]
                        else:
                            candidate.clear()
                    if any(m in line for m in runtime.AUTH_MARKERS):
                        auth_hit.set()
            except (OSError, ValueError):
                pump_failed.set()

        try:
            if os.environ.get("CLAN_ROUND_LOG") == "full":
                prefix = ulid()
                for kind in ("stdout", "stderr"):
                    log_path = confined(hd, f"{prefix}-{kind}.log")
                    log_files[kind] = log_path.open("x")
                    full_logs[kind] = log_path.name
            argv, extra = runtime.launch_argv(cfg, role, hd, initial_prompt=prompt,
                                      unattended=unattended, round_n=state["n"],
                                      channel_dir=channel_dir, project_dir=project_dir,
                                      session_id=state.get("session_id"),
                                      home=paths.home)
            if budget.limits.round_usd is not None:
                argv[1:1] = ['--max-budget-usd', str(budget.limits.round_usd)]
            if state["n"] == 1:                     # a session begins: round 1, or the reset round
                runtime.record_round_session(paths, role, extra)   # (round 1's --session-id, or the gate)
            logged = list(argv)                     # prompt-free, wherever it sits
            logged.pop(argv.index(prompt))
            if "--append-system-prompt" in logged:  # the brief as its path, not its text
                logged[logged.index("--append-system-prompt") + 1] = str(confined(hd, "brief.md"))
            pid_file = confined(hd, "round.pid")
            p = subprocess.Popen(argv, env={**env, **extra}, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 start_new_session=True)
            current["pid"] = p.pid               # `clan down` finds us here if the pane dies
            try:
                pgid = os.getpgid(p.pid)         # start_new_session: pgid == pid
            except ProcessLookupError:
                pgid = p.pid
            pid_file.write_text(json.dumps(
                {"pid": p.pid, "pgid": pgid, "started": runtime._ps_lstart(p.pid)}))
            t_out = threading.Thread(daemon=True, target=pump, args=(p.stdout, "stdout"))
            t_err = threading.Thread(daemon=True, target=pump, args=(p.stderr, "stderr"))
            t_out.start()
            t_err.start()
            deadline = min(time.monotonic() + timeout_s, budget.deadline)
            while True:
                try:
                    p.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if candidate and time.monotonic() - last_output["t"] >= runtime.PROMPT_QUIET_S:
                    prompt_line.append(candidate[0])          # quiet on the marker: a real dialog
                    prompt_hit.set()
                if prompt_hit.is_set() or auth_hit.is_set() or pump_failed.is_set():   # fail fast, say why
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
            if timed_out or prompt_hit.is_set() or auth_hit.is_set() or pump_failed.is_set():
                try:                                # kill the whole group: a grandchild holding
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)   # the pipe must not wedge us
                except (ProcessLookupError, PermissionError, AttributeError):
                    p.kill()
            p.wait()
            for t in (t_out, t_err):                # bounded: a surviving grandchild cannot hang us
                t.join(timeout=runtime.PUMP_JOIN_TIMEOUT_S)
        except Exception as exc:                    # even the kickoff round must not kill the tab
            _kill_current()
            if current.get("pid"):
                try:
                    p.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            traceback.print_exc(file=sys.stderr)
            error = f"{type(exc).__name__}: {exc}"
        workers = [t for t in (locals().get("t_out"), locals().get("t_err")) if t is not None]
        if any(t.is_alive() for t in workers):
            _kill_current()
            for t in workers:
                t.join(timeout=1)
        if not any(t.is_alive() for t in workers):
            for log in log_files.values():
                try:
                    log.close()
                except OSError:
                    pump_failed.set()
        if pump_failed.is_set() and not error:
            error = "round output capture failed; check log storage and terminal"
        output = out_lines.text()
        if prompt_hit.is_set() and not error:
            error = f"interactive prompt the round cannot answer: {prompt_line[0]}"
        if auth_hit.is_set() and not error:
            error = "the harness is not authenticated — run its login once as the operator"
        current.pop("pid", None)
        confined(hd, "round.pid").unlink(missing_ok=True)
        session_id = captured_session["id"]
        if session_id:                           # opencode: resume THIS session, never a global -c
            state["session_id"] = session_id
            session_file.write_text(session_id)
        record = {"ts": runtime.datetime.now(timezone.utc).isoformat(timespec="seconds")
                       .replace("+00:00", "Z"),
                  "round": state["n"], "prompt": prompt, "argv": logged,
                  "returncode": "prompt" if prompt_hit.is_set() else
                        "auth" if auth_hit.is_set() else
                        "timeout" if timed_out else
                        "error" if error else p.returncode,
                  "output": error or output, "stderr": err_lines.text(),
                  "truncated": {"stdout": out_lines.truncated, "stderr": err_lines.truncated},
                  "logs": full_logs, "reset": reset}
        with open(rounds_path, "a") as f:
            f.write(json.dumps(record) + "\n")
        budget.finish(record['returncode'])
        reason = budget.reason()
        publish(reason)
        publish_lifecycle(paths, role, "blocked" if reason or record["returncode"] in ("prompt", "auth") else "idle")
        return False if reason else None

    try:
        if run_round(runtime.default_prompt(cfg, role)) is not False:
            NudgeLoop(run_round, stdin=stdin, log=confined(hd, "nudges.log"),
                      deadline=budget.deadline, fail_fast=True).run()
            if budget.reason() == 'max_seconds':
                publish('max_seconds')
    finally:
        publish_lifecycle(paths, role, "unknown")
        _kill_current()
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)
