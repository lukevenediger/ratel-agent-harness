"""`ratel-tui [--home] [--channel] [--no-persist-colours]`.

Flags win over the environment (`RATEL_HOME`, `CHANNEL`); a home with no
channels exits with a `ratel demo` hint instead of an empty console."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..bus import default_home
from ..paths import list_channels
from .app import RatelTui


def resolve(argv: list[str] | None, environ=None) -> argparse.Namespace:
    env = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(prog="ratel-tui", description="ratel terminal console (read-only)")
    ap.add_argument("--home", help="ratel home (default: $RATEL_HOME, then ~/.ratel)")
    ap.add_argument("--channel", help="channel to open (default: $CHANNEL, then the newest)")
    ap.add_argument("--no-persist-colours", action="store_true", help="keep agent colours in memory only")
    opts = ap.parse_args(argv)
    home = opts.home or env.get("RATEL_HOME")
    opts.home = (Path(home).expanduser() if home else default_home()).resolve()
    opts.channel = opts.channel or env.get("CHANNEL") or None
    opts.persist_colours = not opts.no_persist_colours
    return opts


def main(argv: list[str] | None = None) -> int:
    opts = resolve(argv)
    channels = list_channels(opts.home)
    if not channels:
        print(f"ratel-tui: no channels in {opts.home}.\n"
              f"Seed synthetic data first: ratel demo --home {opts.home / 'demo'}", file=sys.stderr)
        raise SystemExit(1)
    if opts.channel and opts.channel not in channels:
        print(f"ratel-tui: no channel {opts.channel!r} in {opts.home}; known: {', '.join(channels)}", file=sys.stderr)
        raise SystemExit(1)
    RatelTui(opts.home, opts.channel, persist_colours=opts.persist_colours).run()
    return 0
