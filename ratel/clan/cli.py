"""`ratel clan …` — the argparse surface over `clan/session.py`.

Every command prints one JSON value, like the rest of the CLI. The channel and
the clan share a name, `<repo>-<issue>`; the zellij session is that name plus a
start-time stamp, so a restart never collides with a predecessor zellij still
lists. `clan new` prints the attach command; `clan.state.json` records it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ..bus import default_home
from . import session
from .config import ClanPaths


def add_parser(sub: argparse._SubParsersAction) -> None:
    clan = sub.add_parser("clan", help="run a clan of role agents on an issue")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", help="overrides RATEL_HOME")
    common.add_argument("--channel",
                        help="the clan's channel; its zellij session is recorded in "
                             "clan.state.json")
    common.add_argument("--pretty", action="store_true", help="indent the JSON output")
    cs = clan.add_subparsers(dest="clan_cmd", required=True)

    cs.add_parser("roles", parents=[common], help="the merged catalog (alias of catalog)")
    cs.add_parser("catalog", parents=[common],
                  help="harnesses, presets, roles and models, as the board serves them")

    p = cs.add_parser("propose", parents=[common],
                      help="post a clan proposal ({\"roles\": [...]}) from a JSON file or stdin")
    p.add_argument("--file", required=True, help="path to the proposal JSON, or - for stdin")
    p.add_argument("--auto-approve", action="store_true",
                   help="land the proposal immediately, with no board round trip")

    p = cs.add_parser("approve", parents=[common],
                      help="land the newest approved clan proposal on the bus as clan.toml")
    p.add_argument("msg_id", nargs="?", help="a specific bus message carrying an approved clan attachment")

    p = cs.add_parser("new", parents=[common], help="create the channel and the orchestrator's session")
    p.add_argument("checkout")
    p.add_argument("issue", type=int)
    p.add_argument("--orchestrator-harness", dest="oharness")
    p.add_argument("--orchestrator-model", dest="omodel")
    p.add_argument("--session", metavar="CHANNEL",
                   help="channel name (default: <repo>-<issue>); the zellij session is "
                        "this plus a timestamp")
    p.add_argument("--unattended", action="store_true", help="no human in the tab: take defaults")
    p.add_argument("--zellij-tmp", dest="zellij_tmp", metavar="DIR",
                   help="run this clan's zellij server under DIR instead of the user's (tests)")

    cs.add_parser("up", parents=[common], help="start every role in clan.toml that is not up yet")

    p = cs.add_parser("launch", parents=[common], help="exec one role's harness (runs inside its tab)")
    p.add_argument("role")

    cs.add_parser("watch", parents=[common], help="tail the channel and nudge mentioned roles")

    p = cs.add_parser("status", parents=[common], help="roles, panes, worktrees, presence")
    p.add_argument("--screen", action="store_true", help="include a dump of each pane")

    p = cs.add_parser("nudge", parents=[common], help="type a line into a role's pane by hand")
    p.add_argument("role")
    p.add_argument("text", nargs="?")

    p = cs.add_parser("checkpoint", parents=[common],
                      help="reset a role's context (clear/compact + re-orient)")
    p.add_argument("role")
    p.add_argument("--mode", choices=["clear", "compact"], default="clear")
    p.add_argument("--force", action="store_true",
                   help="checkpoint even when the role looks busy")

    p = cs.add_parser("sync", parents=[common], help="fast-forward a detached worktree to the branch tip")
    p.add_argument("role")

    p = cs.add_parser("down", parents=[common], help="kill the session")
    p.add_argument("--prune-worktrees", action="store_true", help="also remove clean worktrees it made")
    p.add_argument("--force", action="store_true", help="with --prune-worktrees, discard uncommitted changes")


def _home(args) -> Path:
    return Path(args.home).expanduser() if args.home else default_home()


def _paths(args) -> ClanPaths:
    if not args.channel:
        sys.exit("ratel clan: pass --channel (the clan's channel and zellij session)")
    return ClanPaths(_home(args), args.channel)


def run(args: argparse.Namespace) -> Any:
    cmd = args.clan_cmd
    if cmd in ("roles", "catalog"):
        return session.roles(_home(args))
    if cmd == "new":
        from .zellij import isolated_env
        return session.new(_home(args), args.checkout, args.issue, session=args.session or args.channel,
                           oharness=args.oharness, omodel=args.omodel, unattended=args.unattended,
                           zellij_env=isolated_env(args.zellij_tmp) if args.zellij_tmp else None)
    paths = _paths(args)
    if cmd == "propose":
        return session.propose(paths, args.file, auto_approve=args.auto_approve)
    if cmd == "approve":
        return session.approve(paths, args.msg_id)
    if cmd == "up":
        return session.up(paths)
    if cmd == "launch":
        return session.launch(paths, args.role)
    if cmd == "watch":
        return session.watch(paths)
    if cmd == "status":
        return session.status(paths, screen=args.screen)
    if cmd == "nudge":
        return session.nudge(paths, args.role, args.text)
    if cmd == "checkpoint":
        return session.checkpoint(paths, args.role, mode=args.mode, force=args.force)
    if cmd == "sync":
        return session.sync(paths, args.role)
    if cmd == "down":
        return session.down(paths, prune_worktrees=args.prune_worktrees, force=args.force)
    raise AssertionError(f"unhandled clan command {cmd}")  # pragma: no cover
