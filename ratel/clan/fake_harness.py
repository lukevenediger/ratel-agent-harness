"""A scripted agent: plays one playbook step per nudge, through the real channel.

Tier 1 integration tests need something that behaves like an agent — reads the
channel, posts in threads, attaches files, emits VERDICT lines — without a
model. Steps are consumed one per nudge line, so a test drives the round
structure from outside.

    [[steps]]
    action = "post" | "reply" | "attach" | "pin" | "noop" | "propose" | "approve"
    text = "..."
    file = "/abs/path"        # attach; a proposal JSON for propose
"""
from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

from ..bus import Bus, default_home
from ..ops import AgentOps
from .loop import NudgeLoop


def newest_thread_for(ops: AgentOps) -> str | None:
    """The thread the agent is expected to answer in: newest message mentioning it."""
    mine = [m for m in ops.bus.read_all()
            if ops.agent in m.get("mentions", []) and m["from"] != ops.agent]
    return (mine[-1].get("parent") or mine[-1]["id"]) if mine else None


def play(ops: AgentOps, step: dict) -> None:
    action = step.get("action", "noop")
    if action == "noop":
        return
    if action in ("propose", "approve"):            # the clan gate, as the CLI runs it
        from . import session as clan_session
        from .config import ClanPaths
        paths = ClanPaths(default_home(), ops.bus.channel)
        if action == "propose":
            clan_session.propose(paths, step["file"],
                                 auto_approve=bool(step.get("auto_approve")))
        else:
            clan_session.approve(paths, step.get("msg_id"))
        return
    text = step.get("text", "")
    if action == "post":
        ops.post(text)
    elif action == "pin":
        ops.post(text, pin=True)
    elif action == "reply":
        ops.post(text, parent=newest_thread_for(ops))
    elif action == "attach":
        ops.post(text, attachments=[ops.attach_file(step["file"])])
    else:
        raise ValueError(f"unknown playbook action {action!r}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ratel-fake-harness", description=__doc__.splitlines()[0])
    ap.add_argument("--playbook", required=True, type=Path)
    ap.add_argument("--log", type=Path, help="append every nudge line here")
    args = ap.parse_args(argv)

    agent, channel = os.environ.get("AGENT_NAME"), os.environ.get("CHANNEL")
    if not agent or not channel:
        sys.exit("ratel-fake-harness: set AGENT_NAME and CHANNEL")
    ops = AgentOps(Bus(default_home(), channel), agent)

    steps = iter(tomllib.loads(args.playbook.read_text()).get("steps", []))
    NudgeLoop(lambda _line: play(ops, next(steps, {"action": "noop"})), log=args.log).run()


if __name__ == "__main__":
    main()
