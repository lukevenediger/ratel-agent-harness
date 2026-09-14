"""`ratel`: the channel from a shell, for agents without an MCP host.

Same cursor semantics as the MCP tools (both go through AgentOps), so presence
and unread stay truthful no matter which surface an agent uses.

Every command prints JSON to stdout: a single object, or a JSON array for the
list-shaped ones (`read`, `wait`, `pins`). `--pretty` indents it for humans.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Any

from .bus import Bus, default_home
from .legacy import migrate
from .ops import AgentOps

PROG = "ratel"


def _emit(obj: Any, pretty: bool) -> None:
    if pretty:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def _ops(args: argparse.Namespace) -> AgentOps:
    agent = args.agent or os.environ.get("AGENT_NAME")
    channel = args.channel or os.environ.get("CHANNEL")
    if not agent:
        sys.exit(f"{PROG}: set AGENT_NAME or pass --agent")
    if not channel:
        sys.exit(f"{PROG}: set CHANNEL or pass --channel")
    return AgentOps(Bus(args.home or default_home(), channel), agent)


def _attachments(ops: AgentOps, args: argparse.Namespace) -> list[dict]:
    atts = [ops.attach_file(p) for p in (args.attach or [])]
    for raw in args.attachment_json or []:
        att = json.loads(raw)
        if not isinstance(att, dict):
            sys.exit(f"{PROG}: --attachment-json must be a JSON object, got {type(att).__name__}")
        atts.append(att)
    return atts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__.splitlines()[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", help="overrides RATEL_HOME")
    common.add_argument("--agent", help="overrides AGENT_NAME")
    common.add_argument("--channel", help="overrides CHANNEL")
    common.add_argument("--pretty", action="store_true", help="indent the JSON output")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("post", parents=[common], help="post a message; prints its id")
    p.add_argument("text", help="message text, or - to read it from stdin")
    p.add_argument("--parent", help="id of the message to reply under")
    p.add_argument("--pin", action="store_true", help="pin this message")
    p.add_argument("--attach", action="append", metavar="PATH", help="attach a local file (repeatable)")
    p.add_argument("--attachment-json", action="append", metavar="JSON",
                   help="raw attachment object, e.g. code/link/tasks; type is inferred if omitted (repeatable)")

    p = sub.add_parser("read", parents=[common], help="new messages from others; advances your cursor")
    p.add_argument("--since", help="start after this id instead of your cursor")
    p.add_argument("--limit", type=int)

    sub.add_parser("catch-up", parents=[common], help="pins plus everything since your cursor")

    p = sub.add_parser("thread", parents=[common], help="a message and its replies")
    p.add_argument("id")

    p = sub.add_parser("wait", parents=[common], help="block until @mentioned; [] on timeout")
    p.add_argument("--timeout", type=float, default=60, metavar="S")
    p.add_argument("--any", action="store_true", help="wake on any new message, not just mentions")

    for name, help_ in (("pin", "pin a message by id"), ("unpin", "unpin a message by id")):
        p = sub.add_parser(name, parents=[common], help=help_)
        p.add_argument("id")

    sub.add_parser("pins", parents=[common], help="the current pins")

    p = sub.add_parser("attach", parents=[common], help="copy a file into the channel; prints the attachment")
    p.add_argument("path")
    p.add_argument("--name", help="name to show instead of the file's own")

    sub.add_parser("migrate", parents=[common],
                   help="import a stopped legacy channel into SQLite; retains original files")
    sub.add_parser("diagnostics", parents=[common],
                   help="count malformed messages and cursors without modifying storage")
    sub.add_parser("export", parents=[common], help="write channel messages as JSONL without advancing cursors")
    p = sub.add_parser("tail", parents=[common], help="follow channel messages as JSONL without advancing cursors")
    p.add_argument("--since", help="replay after this message id before following")

    from .clan.cli import add_parser as add_clan_parser
    add_clan_parser(sub)
    return parser


def run(args: argparse.Namespace) -> Any:
    if args.cmd == "clan":
        from .clan.cli import run as clan_run
        return clan_run(args)
    if args.cmd in ("migrate", "export", "tail", "diagnostics"):
        channel = args.channel or os.environ.get("CHANNEL")
        if not channel:
            raise ValueError("set CHANNEL or pass --channel")
        bus = Bus(args.home or default_home(), channel, read_only=True)
        if args.cmd == "diagnostics":
            return bus.diagnostics()
        if args.cmd == "migrate":
            return migrate(bus.channel_dir)
        cursor = args.since if args.cmd == "tail" else None
        end = bus.tip() if args.cmd == "export" else None
        if args.cmd == "export" and end is None:
            return None
        while True:
            msgs = bus.read_since(cursor, limit=200)
            for msg in msgs:
                print(json.dumps(msg, ensure_ascii=False), flush=True)
                cursor = msg["id"]
                if args.cmd == "export" and cursor == end:
                    return None
            if not msgs:
                if args.cmd == "export":
                    return None
                time.sleep(0.5)
    ops = _ops(args)
    if args.cmd == "post":
        text = sys.stdin.read() if args.text == "-" else args.text
        return {"id": ops.post(text, parent=args.parent,
                               attachments=_attachments(ops, args) or None, pin=args.pin)}
    if args.cmd == "read":
        return ops.read_channel(args.since, args.limit)
    if args.cmd == "catch-up":
        return ops.catch_up()
    if args.cmd == "thread":
        return ops.read_thread(args.id)
    if args.cmd == "wait":
        return ops.wait_for_mention(args.timeout, args.any)
    if args.cmd == "pin":
        return {"id": ops.pin(args.id)}
    if args.cmd == "unpin":
        return {"id": ops.unpin(args.id)}
    if args.cmd == "pins":
        return ops.pins()
    if args.cmd == "attach":
        return ops.attach_file(args.path, args.name)
    raise AssertionError(f"unhandled command {args.cmd}")  # pragma: no cover


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except (ValueError, OSError, sqlite3.Error) as e:
        sys.exit(f"{PROG} {args.cmd}: {e}")
    except KeyboardInterrupt:
        return
    if result is not None:              # `clan launch` execs; `clan watch` blocks
        _emit(result, args.pretty)


if __name__ == "__main__":
    main()
