"""Per-role context-window measurement, in tokens.

Dispatches on the role's harness and reads each harness's own usage record:
Claude's newest transcript under `~/.claude/projects`, OpenCode's newest
session in its sqlite DB, or a scripted number for the `fake` harness.
Anything unknown, missing or unparsable is `None` — never raises.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path


def _epoch(v) -> float | None:
    """ISO UTC string (tab records, transcript timestamps) or ms/epoch number
    (opencode `time_created`) → seconds. Unparsable → None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if float(v) > 1e11 else float(v)
    s = str(v).strip()
    if s.replace(".", "", 1).isdigit():       # opencode stores ms epoch numbers as text
        return _epoch(float(s))
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def context_tokens(role_tab: dict, home: Path | str | None = None) -> int | None:
    """Tokens currently in a role's context window, or None if unmeasurable."""
    harness = (role_tab.get("harness") or "").strip()
    try:
        h = Path(home) if home is not None else Path.home()
        if harness in ("claude", "claude-p"):
            return _claude(role_tab, h)
        if harness in ("opencode", "opencode-run"):
            return _opencode(role_tab, h)
        if harness == "fake":
            return _fake(role_tab)
    except Exception:
        return None
    return None


def _claude(role_tab: dict, home: Path) -> int | None:
    cwd = role_tab.get("cwd")
    if not cwd:
        return None
    projects = home / ".claude" / "projects" / str(cwd).replace("/", "-")
    if not projects.is_dir():
        return None
    launched = _epoch(role_tab.get("launched"))
    session_id = role_tab.get("session_id")
    if session_id:
        try:
            uuid.UUID(session_id)        # the id builds a path — never trust it blind
        except (ValueError, AttributeError, TypeError):
            return None
        # a tab that knows its session reads exactly its own file — never a
        # sibling from another session that shares the cwd
        files = [projects / f"{session_id}.jsonl"]
    elif launched is None:
        # several session files share one cwd; without a launch record the
        # answer could be a stale sibling's — None, never a guess
        return None
    else:
        files = projects.glob("*.jsonl")
    candidates = []
    for f in files:
        if not f.exists():
            continue
        identity, ts, usage = _claude_read(f)
        if identity != str(cwd) or usage is None:
            continue
        ts = _epoch(ts)
        if launched is not None and (ts is None or ts < launched):
            continue                # mtime lies (claude touches old files); the record timestamp does not
        candidates.append(usage)
    if len(candidates) != 1:        # 0: nothing matched; >1: several live sessions in this cwd
        return None
    return sum(candidates[0].get(k) or 0 for k in ("input_tokens",
                                                   "cache_creation_input_tokens",
                                                   "cache_read_input_tokens"))


_READ_CACHE: dict[str, tuple] = {}


def _claude_read(f: Path) -> tuple[str | None, str | None, dict | None]:
    """Memoised on (mtime_ns, size): an unchanged transcript costs one stat,
    which keeps the board's per-message meter re-fetch cheap."""
    st = f.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _READ_CACHE.get(str(f))
    if hit and hit[0] == key:
        return hit[1]
    if len(_READ_CACHE) > 64:
        _READ_CACHE.clear()
    val = _claude_scan(f)
    _READ_CACHE[str(f)] = (key, val)
    return val


def _claude_scan(f: Path) -> tuple[str | None, str | None, dict | None]:
    """(session cwd, last assistant timestamp, last assistant usage).

    Identity is the first record carrying a cwd field — the literal first
    line can be `custom-title` with `cwd: null` (claude 2.1.263), so a null
    cwd does not count as carrying.
    """
    identity = ts = None
    usage = None
    for line in f.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if identity is None and rec.get("cwd"):
            identity = rec["cwd"]
        if rec.get("type") == "assistant":
            ts = rec.get("timestamp")
            usage = (rec.get("message") or {}).get("usage")
    return identity, ts, usage if isinstance(usage, dict) else None


def _opencode(role_tab: dict, home: Path) -> int | None:
    cwd = role_tab.get("cwd")
    if not cwd:
        return None
    db = home / ".local" / "share" / "opencode" / "opencode.db"
    if not db.exists():
        return None
    launched = _epoch(role_tab.get("launched"))
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sessions = con.execute(
            "SELECT id, time_created FROM session WHERE directory = ? "
            "ORDER BY time_created DESC", (str(cwd),)).fetchall()
        for sid, tc in sessions:            # newest first; skip sessions born before the tab
            if launched is not None and (_epoch(tc) is None or _epoch(tc) < launched):
                continue
            for (data,) in con.execute(
                    "SELECT data FROM message WHERE session_id = ? "
                    "ORDER BY time_created DESC", (sid,)):
                try:
                    rec = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if rec.get("role") != "assistant" or "error" in rec:
                    continue                # aborted rows carry zeros + an error key
                if not (rec.get("time") or {}).get("completed"):
                    continue                # a turn still streaming has zero tokens
                tokens = rec.get("tokens") or {}
                cache = tokens.get("cache") or {}
                return ((tokens.get("input") or 0) + (cache.get("read") or 0)
                        + (cache.get("write") or 0))
        return None
    finally:
        con.close()


def _fake(role_tab: dict) -> int | None:
    hd = role_tab.get("harness_dir")
    if not hd:
        return None
    p = Path(hd) / "context.txt"
    if not p.exists():
        return None
    return int(p.read_text().strip())
