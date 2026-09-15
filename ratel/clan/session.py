"""Clan lifecycle: create a session, bring roles up, inspect it, tear it down.

The CLI is a thin adapter over these; the Tier 1 integration test drives them
directly. Nothing here parses arguments or prints.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..bus import Bus, now_iso
from ..paths import channel_path, confined
from ..schema import validate_name
from ..storage import Store
from ..ulid import is_ulid
from . import approval, harness
from .budget import STOP_REASONS, stopped_reason
from .config import (
    CATALOG_ERRORS,
    HARNESSES,
    MODEL_ID_RE,
    ClanConfig,
    ClanPaths,
    expires_past,
    load_catalog,
    load_models,
    read_state,
    update_state,
)
from .config import catalog as clan_catalog
from .context import context_tokens
from .gitwt import (
    add_detached_worktree,
    add_writer_worktree,
    default_branch,
    is_dirty,
    remove_worktree,
    repo_slug,
    sync_detached,
)
from .prompts import _CTRL, TEXT_CAP
from .proposal import clan_config_from, validate_clan_attachment
from .terminal import OBSERVATION_TTL_S, Terminal, backend_name, preference, terminal_id
from .watch import MENTION, Watcher, safe_thread
from .zellij import ZELLIJ_ERRORS, Zellij, create_session, scrubbed_env


class ClanError(Exception):
    """The clan cannot be read as configured: a missing `clan.toml`, or a drift
    between `clan.toml` and `clan.state.json` only the operator can resolve.
    Raised, never `sys.exit` — a request handler must be able to answer 200
    instead of dying in its thread. The CLI wraps it back into an exit."""


_NO_STATE = object()


def _read_config(paths: ClanPaths) -> ClanConfig:
    db = paths.channel_dir / "channel.sqlite3"
    if db.exists():
        with Store(db, root=paths.home).connection() as con:
            if Store.pending(con) is not None:
                raise ClanError("approval application is pending — rerun `ratel clan approve` to recover it")
    if not paths.clan_toml.exists():
        raise ClanError(f"no clan at {paths.clan_toml} — run `ratel clan new` first")
    # the catalog is the user's/`roles.toml` plus the bundled file; a broken one
    # must not be blamed on clan.toml. Load it under its own message.
    try:
        catalog = load_catalog(paths.home)
    except CATALOG_ERRORS as e:
        raise ClanError(f"cannot read the role catalog ({paths.home / 'roles.toml'} "
                        f"or the bundled catalog): {e}") from e
    try:
        return ClanConfig.read(paths, catalog)
    except CATALOG_ERRORS as e:
        # clan.toml is agent-writable, so it can be malformed or the wrong
        # shape. Wrap it as ClanError — the board's `except ClanError` is the
        # only tolerance the /clan route has, and an escaping TOMLDecodeError
        # would kill the handler thread instead of answering 200.
        raise ClanError(f"cannot read {paths.clan_toml}: {e}") from e


def _read_models(paths: ClanPaths) -> dict:
    """The model catalog, wrapped as ClanError. `models.toml` is operator-writable
    and read on the activity path; a malformed one must reach the board's
    `except ClanError` (200) rather than kill the handler thread."""
    try:
        return load_models(paths.home)
    except CATALOG_ERRORS as e:
        raise ClanError(f"cannot read {paths.home / 'models.toml'} "
                        f"(or the bundled catalog): {e}") from e


def _check_drift(cfg: ClanConfig, state: dict) -> None:
    """clan.toml is agent-writable: the checkout and the writer bit must still
    match what the state recorded at kickoff."""
    if not state:
        return
    if state.get("checkout") and state["checkout"] != cfg.checkout:
        raise ClanError(f"checkout changed since kickoff "
                        f"(state: {state['checkout']}, clan.toml: {cfg.checkout}) — "
                        "resolve with the operator")
    for name, recorded in (state.get("writers") or {}).items():
        if name in cfg.roles and bool(recorded) != cfg.roles[name].writer:
            raise ClanError(f"role {name}'s writer bit changed since "
                            "kickoff — resolve with the operator")
    # the brief picks the arming decisions in harness.py (skills, plugin dir,
    # unattended tools, the clan/ grant): a role that re-briefs itself as the
    # orchestrator would be armed as one on its next round. Legacy state has
    # no `briefs`; a missing record is no check, not a refusal.
    for name, recorded in (state.get("briefs") or {}).items():
        if name in cfg.roles and recorded != cfg.roles[name].brief:
            raise ClanError(f"role {name}'s brief changed since "
                            "kickoff — resolve with the operator")


def _read_clan(paths: ClanPaths, state: object = _NO_STATE) -> tuple[ClanConfig, dict]:
    """The one cfg+state reader. `state` lets a caller (the board, under a
    non-blocking shared lock) pass a snapshot instead of re-reading the file;
    `_NO_STATE` reads it here. The cfg load, shape check and drift check live
    once so `activity()` and `status()` cannot diverge."""
    cfg = _read_config(paths)
    if state is _NO_STATE:
        state = read_state(paths)
    if not isinstance(state, dict):     # agent-writable file: valid JSON, wrong shape
        state = {}
    _check_drift(cfg, state)
    return cfg, state


def _load(paths: ClanPaths) -> tuple[ClanConfig, dict]:
    try:
        return _read_clan(paths)
    except ClanError as e:
        sys.exit(f"ratel clan: {e}")


ISOLATION_KEYS = ("TMPDIR", "HOME", "ZELLIJ_CONFIG_FILE")


def _zellij(state: dict, cfg: ClanConfig) -> Zellij:
    """The clan's server. state["env"] carries only the isolation keys — never a whole
    environment, which would put whatever secrets the operator exported into a JSON file."""
    # state["env"] is tool-written, but the file is agent-REACHABLE (inside the
    # channel dir) — filter on read as well as write, so a hostile PATH or any
    # smuggled var never reaches the zellij subprocess.
    env = {k: v for k, v in (state.get("env") or {}).items() if k in ISOLATION_KEYS}
    return Zellij(state.get("session", cfg.channel), env={**scrubbed_env(), **env})


def _terminal(paths: ClanPaths, state: dict, cfg: ClanConfig) -> Terminal:
    if backend_name(state) == "herdr":
        from .herdr import Herdr
        return Herdr(paths, state)
    return _zellij(state, cfg)


def _launch_argv(paths: ClanPaths, channel: str, role: str) -> list[str]:
    """The tab command: this interpreter, absolute, so the zellij server's PATH is irrelevant."""
    return [sys.executable, "-m", "ratel.cli", "clan", "launch",
            "--home", str(paths.home), "--channel", channel, role]


def _worktree_for(cfg: ClanConfig, role: str) -> Path:
    validate_name(role, "role")
    checkout = Path(cfg.checkout)
    return confined(checkout.parent, f"{checkout.name}-wt", f"{cfg.issue}-{role}")


def _record_tab(paths: ClanPaths, role: str, info: dict) -> None:
    # merge, never replace: the tab process may have pinned session_id/launched
    # (harness.record_session) before new/up records the pane — both survive
    update_state(paths, lambda s: s.setdefault("tabs", {}).setdefault(role, {}).update(info))


def headless_kind(harness: str) -> str:
    """The headless twin of an interactive kind; explicit headless kinds pass through."""
    return {"claude": "claude-p", "opencode": "opencode-run"}.get(harness, harness)

STRAY_TIMEOUT_S = 2.0


def _until(pred: Callable[[], bool], timeout_s: float, poll_s: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout_s
    while not pred():
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)
    return True


def _close_stray_tabs(z: Zellij, keep: set[str]) -> None:
    """Close tabs the clan did not open — zellij's default `Tab #1`.

    `list-panes -j` lags `query-tab-names` by ~200-300 ms on a fresh session
    (measured, issue #7), so each stray's tab id is resolved from `list-panes`
    with a bounded poll and the close is confirmed against `query-tab-names`.
    Closing goes only by id: a bare `close-tab` acts on the focused tab, which
    is destructive with a human attached and inert on the detached session
    `clan new` leaves (measured, round 5). A stray that never resolves to an
    id, or survives the close, is left open with a warning on stderr — the
    sweep itself never raises, whatever the server does mid-flight.
    """
    try:
        for name in [n for n in z.tab_names() if n not in keep]:
            resolved = _until(lambda: any(not p.get("is_plugin") and p.get("tab_name") == name
                                          for p in z.list_panes()), STRAY_TIMEOUT_S)
            if not resolved:
                print(f"ratel clan: tab {name!r} never resolved to a tab id — "
                      "leaving it open", file=sys.stderr)
                continue
            for tab_id in {int(p["tab_id"]) for p in z.list_panes()
                           if not p.get("is_plugin") and p.get("tab_name") == name}:
                z.close_tab(tab_id=tab_id)
            if not _until(lambda: name not in z.tab_names(), STRAY_TIMEOUT_S):
                print(f"ratel clan: tab {name!r} could not be closed — leaving it open",
                      file=sys.stderr)
    except ZELLIJ_ERRORS as e:              # the server died or hung mid-sweep
        print(f"ratel clan: stray-tab sweep aborted: {e!r}", file=sys.stderr)
    finally:
        try:
            z.go_to_tab("orchestrator")     # any close may have moved focus
        except ZELLIJ_ERRORS:
            pass


def _tab(state: dict, role: str) -> dict:
    """The role's tab record, or `{}`. `clan.state.json` is agent-written, so
    `tabs` or `tabs[role]` can be any JSON shape — never hand a non-dict on to
    a caller that derefs it."""
    tabs = state.get("tabs")
    tab = tabs.get(role) if isinstance(tabs, dict) else None
    return tab if isinstance(tab, dict) else {}


def roles(home: Path) -> dict:
    return clan_catalog(home)


def _refuse_if_no_nested(what: str) -> None:
    """CLAN_NO_NESTED=1 is a mistake barrier for a misled agent, not a
    containment boundary — see docs/DECISIONS.md, Decision 29. Covers every
    entry point that creates worktrees or zellij state: `new`, `up`, `launch`."""
    if os.environ.get("CLAN_NO_NESTED") == "1":
        sys.exit(f"ratel clan {what}: refused — CLAN_NO_NESTED=1 is set (a test or probe "
                 "environment must not create a clan on this zellij server)")



def new(home: Path, checkout: str | Path, issue: int, session: str | None = None,
        oharness: str | None = None, omodel: str | None = None,
        unattended: bool = False, zellij_env: dict[str, str] | None = None,
        terminal: str | None = None) -> dict:
    """Create the channel, seed clan.toml with the orchestrator, open its session."""
    _refuse_if_no_nested("new")
    backend = preference(home, terminal)
    if zellij_env is not None and backend != "zellij":
        raise ValueError("zellij isolation requires --terminal zellij")
    from .tools import require_binary
    if backend == "herdr":
        require_binary(backend, "new clans; select --terminal herdr or --terminal zellij")
    checkout = Path(checkout).expanduser().resolve()
    slug = repo_slug(checkout)
    channel = session or f"{slug.split('/')[-1]}-{issue}"
    paths = ClanPaths(home, channel).ensure()
    if backend == "herdr" and read_state(paths).get("tabs"):
        raise ValueError("clan already has launches; run clan down before clan new")
    Bus(home, channel)                      # creates the channel dirs and SQLite database

    over: dict[str, Any] = {}
    if oharness:
        over["harness"] = oharness
    if omodel:
        over["model"] = omodel
    cfg = ClanConfig.from_dict({"channel": channel, "issue": issue, "repo": slug,
                                "checkout": str(checkout), "roles": {"orchestrator": over}},
                               load_catalog(home))
    cfg.write(paths)

    env = {k: v for k, v in (zellij_env or {}).items() if k in ISOLATION_KEYS}
    zenv = {**scrubbed_env(), **env}
    if backend == "herdr":
        from .herdr import Herdr
        z = Herdr(paths, {})
        z.create_background(checkout)
        zsession = z.session
    else:
        z = create_session(channel, zenv, factory=Zellij)
        zsession = z.session
    update_state(paths, lambda s: s.update(
        terminal_backend=backend, workspace_id=getattr(z, "workspace", None),
        session=zsession, python=sys.executable, unattended=bool(unattended), env=env, maintenance_ready=False, runs={},
        checkout=str(checkout), writers={"orchestrator": cfg.roles["orchestrator"].writer},
        briefs={"orchestrator": cfg.roles["orchestrator"].brief},
        created=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        tabs={}, watch={}, terminal_lifecycle={}, terminal_controls={}))
    harness.write_configs(paths, cfg, "orchestrator", worktree=None, unattended=unattended)
    if unattended:                          # the headless kickoff cannot answer claude's
        harness.mark_trusted(checkout)      # first-run trust dialog — pre-mark the path

    clan_tabs = (
        ("orchestrator", checkout, _launch_argv(paths, channel, "orchestrator")),
        ("watch", paths.channel_dir, [sys.executable, "-m", "ratel.cli", "clan", "watch",
                                      "--home", str(home), "--channel", channel]),
        ("bus", paths.channel_dir, [sys.executable, "-m", "ratel.cli", "tail",
                                    "--home", str(home), "--channel", channel]))
    for name, cwd, argv in clan_tabs:
        tab_id = z.new_tab(name, cwd, argv)
        pane_id = z.pane_or_none(name)
        if pane_id is None:
            print(f"ratel clan: no pane for tab {name!r} yet — recorded without a pane id",
                  file=sys.stderr)
        info = {"tab_id": tab_id, "pane_id": pane_id, "cwd": str(cwd)}
        if name == "orchestrator":
            kind = cfg.roles["orchestrator"].harness
            info["harness"] = headless_kind(kind) if unattended else kind
        _record_tab(paths, name, info)
    if backend == "zellij":
        _close_stray_tabs(z, keep={name for name, _, _ in clan_tabs})
    return {"session": zsession, "channel": channel, "terminal_backend": backend,
            "workspace_id": getattr(z, "workspace", None),
            "attach": z.attach_command() if backend == "herdr" else f"zellij attach {zsession}"}


def up(paths: ClanPaths) -> dict:
    """Worktree, config and tab for every role that is not up yet. Idempotent."""
    _refuse_if_no_nested("up")
    cfg, state = _load(paths)
    models = load_models(paths.home)
    cfg.validate(models)
    missing = harness.missing_env(cfg, models)
    if missing:
        sys.exit("ratel clan up: missing environment keys — " +
                 "; ".join(f"{role}: {', '.join(vars_)}" for role, vars_ in missing.items()))
    z = _terminal(paths, state, cfg)
    if z.session not in z.live_sessions():
        sys.exit(f"ratel clan up: {backend_name(state)} session {z.session!r} is gone — "
                 "`clan down` killed it (a reboot also drops an exited one). "
                 "Run `ratel clan new <checkout> <issue>` to start a fresh session, "
                 "then `clan up`.")
    if backend_name(state) == "herdr":
        for role, tab in state.get("tabs", {}).items():
            if role in cfg.roles and tab.get("launch_pid"):
                try:
                    z.resolve(tab["pane_id"])
                except (ValueError, OSError):
                    sys.exit(f"ratel clan up: {role}'s launch is lost; run clan down, then clan new to recover")
    update_state(paths, lambda s: s.update(maintenance_ready=False))
    branch = f"issue-{cfg.issue}"
    todo = [r for r in cfg.roles if r != "orchestrator" and r not in (state.get("tabs") or {})]
    todo.sort(key=lambda r: not cfg.roles[r].writer)     # the writer creates the branch
    started = []
    skills_failed = []
    for role in todo:
        spec = cfg.roles[role]
        wt = _worktree_for(cfg, role)
        if spec.writer:
            add_writer_worktree(cfg.checkout, wt, branch, base=default_branch(cfg.checkout))
        else:
            add_detached_worktree(cfg.checkout, wt, branch)
        skills_failed += [{"role": role, "skill": s}
                          for s in harness.install_skills(wt, spec.skills)]
        harness.write_configs(paths, cfg, role, worktree=wt, unattended=state.get("unattended", False))
        if state.get("unattended", False):
            harness.mark_trusted(wt)
        tab_id = z.new_tab(role, wt, _launch_argv(paths, cfg.channel, role))
        effective = headless_kind(spec.harness) if state.get("unattended", False) else spec.harness
        _record_tab(paths, role, {"tab_id": tab_id, "pane_id": z.pane_or_none(role),
                                  "cwd": str(wt),
                                  "harness": effective, "worktree": str(wt), "branch": branch,
                                  "writer": spec.writer})
        started.append(role)
    if started:      # pin the writer bits and briefs the operator approved at kickoff
        def pin(s: dict) -> None:
            s.setdefault("writers", {}).update({r: cfg.roles[r].writer for r in started})
            s.setdefault("briefs", {}).update({r: cfg.roles[r].brief for r in started})
        update_state(paths, pin)
    result: dict[str, Any] = {"started": started}
    if skills_failed:      # not fatal: the role runs, but the operator is told
        result["skills_failed"] = skills_failed
    return result


def launch(paths: ClanPaths, role: str) -> None:
    _refuse_if_no_nested("launch")
    cfg, state = _load(paths)
    tab = _tab(state, role)
    # Not the tab record's job: a role's tab starts before its record is
    # written, so `launch` derives the kind itself. Only an unattended clan
    # maps interactive kinds headless — an attended clan's human sits in the
    # tab and must get the interactive harness the catalog names.
    kind = tab.get("harness") or cfg.roles[role].harness
    cfg.roles[role].harness = headless_kind(kind) if state.get("unattended", False) else kind
    harness.launch(paths, cfg, role, worktree=tab.get("worktree"),
                   unattended=state.get("unattended", False))


def watch(paths: ClanPaths) -> None:
    cfg, state = _load(paths)

    def cp(role: str, mode: str, reason: str) -> None:
        checkpoint(paths, role, mode=mode, force=False, reason=reason)

    Watcher(Bus(paths.home, cfg.channel), paths, _terminal(paths, state, cfg),
            thresholds={r: s.checkpoint_at for r, s in cfg.roles.items()},
            checkpoint=cp).run()


# Node state vocabulary — exclusive, first match wins. `board.html` carries the
# same list in mapState and a test asserts they are equal.
ACTIVITY_STATES = ("gone", "stopped", "awaiting-operator", "stuck", "context-full", "busy", "queued",
                   "online", "idle", "offline")
CONFIDENCES = ("measured", "inferred", "weak")
ONLINE_WINDOW_S = 300
QUIET_WINDOW_S = 90
HEADLESS_HARNESSES = frozenset({"claude-p", "opencode-run"})
# Read-path guards: everything on the map comes from agent-writable files, so a
# value is dropped rather than escaped-and-shown.
ROLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")

# reasons[] is a closed vocabulary of format strings; only numbers are ever
# interpolated, never a value read from disk.
R_NO_TAB = "no tab recorded for this role"
R_NUDGED = "nudged {minutes} min ago with no reply yet"
R_ESCALATED = "silent for {minutes} min after a nudge"
# Legacy state predates the wall-clock `at`; a missing timestamp is unknown
# age, never "just nudged" — the reason carries no number to misread.
R_NUDGED_UNKNOWN = "nudged, age unknown — state predates this version"
R_ESCALATED_UNKNOWN = "silent after a nudge, age unknown"
R_AWAITING = "a permission prompt has been waiting in the pane for {minutes} min"
R_AWAITING_UNKNOWN = "a permission prompt is waiting in the pane, age unknown"
R_PENDING = "{count} unanswered mention(s) queued"
R_CONTEXT = "context at {tokens} of {limit} tokens"
R_ROUND_RUNNING = "headless round {round} running for {seconds}s"
R_CURSOR = "cursor {seconds}s behind the channel tip"
R_QUIET = ("no activity measured in the last {seconds}s — the harness may be "
           "waiting at its prompt")
R_EMITTING = "the transcript grew since the last sample — the model is still emitting"
ROUND_OUTCOMES = {
    "ok": "the last headless round completed",
    "failed": "the last headless round failed",
    "timeout": "the last headless round timed out",
    "auth": "the last headless round could not authenticate",
    "prompt": "the last headless round hit an unanswerable prompt",
    "error": "the last headless round errored",
}
REASONS = (R_NO_TAB, R_NUDGED, R_NUDGED_UNKNOWN, R_ESCALATED, R_ESCALATED_UNKNOWN,
           R_AWAITING, R_AWAITING_UNKNOWN, R_PENDING, R_CONTEXT, R_ROUND_RUNNING,
           R_CURSOR, R_QUIET, R_EMITTING, *ROUND_OUTCOMES.values())


def _as_int(v) -> int | None:
    """A number from an agent-writable file, or None. `"200k"` must not reach
    the page and become a NaN SVG attribute."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None


def _as_str(v) -> str | None:
    return v if isinstance(v, str) else None


def _seconds_since(ts, now: datetime) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return max(0.0, (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds())
    except ValueError:
        return None


def _minutes_since(ts, now: datetime) -> int | None:
    """Minutes since an ISO timestamp, or None when there is no parseable one.
    None is unknown age, not zero — legacy state without a wall-clock `at` must
    never read as "just nudged"."""
    seconds = _seconds_since(ts, now)
    return None if seconds is None else int(seconds // 60)


def _pid_alive(pid) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _round_state(paths: ClanPaths, role: str) -> dict:
    """The last headless round's number and a *classified* outcome, plus whether
    a round is running. `round.output` and `round.prompt` never leave here."""
    hd = paths.harness_dir(role)
    pid_file = hd / "round.pid"
    running, started_s = False, None
    if pid_file.exists():
        try:
            rec = json.loads(pid_file.read_text())
        except (json.JSONDecodeError, OSError):
            rec = {}
        running = isinstance(rec, dict) and _pid_alive(rec.get("pid"))
        try:
            started_s = max(0.0, time.time() - pid_file.stat().st_mtime)
        except OSError:
            started_s = None
    n = outcome = last_ts = None
    rounds = hd / "rounds.jsonl"
    if rounds.exists():
        try:
            raw = rounds.read_text()
        except OSError:
            raw = ""
        for line in reversed(raw.splitlines()):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if isinstance(rec.get("round"), int) and not isinstance(rec.get("round"), bool):
                n = rec["round"]
            rc = rec.get("returncode")
            if isinstance(rc, bool):
                pass
            elif isinstance(rc, int):
                outcome = "ok" if rc == 0 else "failed"
            elif rc in ("timeout", "auth", "prompt", "error"):
                outcome = rc
            if isinstance(rec.get("ts"), str):
                last_ts = rec["ts"]
            break
    return {"n": n, "running": running, "outcome": outcome, "last_ts": last_ts,
            "started_s": started_s}


def terminal_observation(state: dict, role: str, now: datetime) -> dict | None:
    if backend_name(state) != "herdr":
        return None
    watch = state.get("watch") or {}
    observations = watch.get("terminal")
    observations = observations if isinstance(observations, dict) else {}
    obs = observations.get(role)
    obs = obs if isinstance(obs, dict) else {}
    age = _seconds_since(obs.get("at"), now)
    fresh = age is not None and 0 <= age <= OBSERVATION_TTL_S
    status = obs.get("state")
    if status not in ("idle", "done", "working", "blocked", "unknown", "unavailable"):
        status = "unknown"
    return {"state": status, "at": _as_str(obs.get("at")), "stale": not fresh, "source": "herdr"}


def _activity_from(paths: ClanPaths, cfg: ClanConfig, state: dict,
                   samples: dict | None = None) -> dict:
    """The read-only clan snapshot shared by `status()`, the board's clan route
    and `checkpoint`'s busy guard: pure reads of `clan.toml`, `clan.state.json`
    and the harness dirs, no writes and no subprocess. Subprocess work
    (`is_dirty`, `dump_screen`) stays in `status()`.

    `samples` is the board's process-local context-token sampler keyed by role;
    this updates it and uses a rise since the previous sample as the only honest
    liveness signal an interactive role has. It is never persisted into
    `clan.state.json` — a second writer to that flock'd file is how it truncates.
    """
    models = _read_models(paths)
    checkpoints = [c for c in (state.get("checkpoints") or []) if isinstance(c, dict)]
    bus = Bus(paths.home, paths.channel, read_only=True)   # status/activity never write
    msgs = bus.read_all()
    last_post: dict[str, str] = {}
    for m in msgs:
        last_post[m["from"]] = m["ts"]
    presence = bus.presence()
    by_agent = {p["agent"]: p for p in presence}
    watch = state.get("watch") or {}
    nudged = watch.get("nudged") or {}
    pending = watch.get("pending") or {}
    awaiting = watch.get("awaiting") or {}
    if not isinstance(awaiting, dict):
        awaiting = {}
    now = datetime.now(timezone.utc)
    rows = []
    warnings = []
    for role, spec in cfg.roles.items():
        # -- read-path guards: drop a bad role, never escape-and-show it --
        if not ROLE_RE.fullmatch(role):
            warnings.append("dropped a role with a non-mentionable name")
            continue
        if spec.harness not in HARNESSES:
            warnings.append("dropped a role with an unknown harness")
            continue
        if not isinstance(spec.model, str) or not MODEL_ID_RE.fullmatch(spec.model):
            warnings.append("dropped a role with a malformed model id")
            continue
        checkpoint_at = _as_int(spec.checkpoint_at)
        tab = _tab(state, role)
        harness_kind = tab.get("harness") or spec.harness
        if harness_kind not in HARNESSES:               # agent-writable tab record
            harness_kind = spec.harness
        headless = harness_kind in HEADLESS_HARNESSES
        expired = expires_past(models[spec.model]) if spec.model in models else False
        if expired:
            # never interpolate a raw value from models.toml (operator-writable)
            when = _as_str(models[spec.model].get("expires")) or "?"
            warnings.append(f"{role}: model {spec.model} expired {when}")
        tokens = context_tokens({**tab, "harness": harness_kind,
                                 "harness_dir": str(paths.harness_dir(role))})
        emitting = False
        if samples is not None and not headless:
            prev = samples.get(role)
            emitting = tokens is not None and prev is not None and tokens > prev
            if tokens is not None:
                samples[role] = tokens
        rnd = (_round_state(paths, role) if headless else
               {"n": None, "running": False, "outcome": None, "last_ts": None, "started_s": None})
        nd = nudged.get(role)
        nd = nd if isinstance(nd, dict) else None
        # the watcher's record of a harness permission dialog in the pane; the
        # state file is agent-writable, so the shape is checked and the one
        # free-text field is re-sanitised on the way out (prompts.TEXT_CAP)
        aw = awaiting.get(role)
        aw = aw if isinstance(aw, dict) and aw.get("kind") == "permission" else None
        aw_text = (_as_str(aw.get("text")) or "").translate(_CTRL)[:TEXT_CAP] if aw else ""
        pend = pending.get(role)
        pend = pend if isinstance(pend, dict) else None
        pres = by_agent.get(role) or {}
        last_seen = pres.get("last_seen") if isinstance(pres.get("last_seen"), str) else None
        post_ts = last_post.get(role)
        post_age = _seconds_since(post_ts, now)
        cursor_age = _seconds_since(last_seen, now)
        branch = _as_str(tab.get("branch"))
        if branch is not None and not BRANCH_RE.fullmatch(branch):
            branch = None
        worktree = _as_str(tab.get("worktree"))
        cp_ts = [_as_str(c.get("ts")) for c in checkpoints if c.get("role") == role]
        reasons: list[str] = []
        since = None
        runs = state.get('runs')
        run = runs.get(role, {}) if isinstance(runs, dict) else {}
        stop_reason = stopped_reason(state, role)
        # -- node state: exclusive, first match wins --
        if not tab:
            state_name, confidence = "gone", "weak"
            reasons.append(R_NO_TAB)
        elif stop_reason in STOP_REASONS:
            state_name, confidence = 'stopped', 'measured'
            reasons.append(STOP_REASONS[stop_reason])
            since = run.get('at')
        elif aw is not None:
            # a dialog explains the silence: not stuck, not busy — the
            # operator's to answer, and the watcher has already said so
            state_name, confidence = "awaiting-operator", "measured"
            age = _minutes_since(aw.get("at"), now)
            reasons.append(R_AWAITING.format(minutes=age) if age is not None
                           else R_AWAITING_UNKNOWN)
            since = aw.get("at")
        elif nd is not None and nd.get("escalated"):
            state_name, confidence = "stuck", "inferred"
            age = _minutes_since(nd.get("at"), now)
            reasons.append(R_ESCALATED.format(minutes=age) if age is not None
                           else R_ESCALATED_UNKNOWN)
            since = nd.get("at")
        elif rnd["outcome"] in ("timeout", "error", "auth", "prompt"):
            state_name, confidence = "stuck", "measured"
            reasons.append(ROUND_OUTCOMES[rnd["outcome"]])
            since = rnd["last_ts"]
        elif tokens is not None and checkpoint_at and tokens > checkpoint_at:
            state_name, confidence = "context-full", "measured"
            reasons.append(R_CONTEXT.format(tokens=tokens, limit=checkpoint_at))
        elif rnd["running"] or nd is not None or emitting:
            state_name = "busy"
            confidence = "measured" if (rnd["running"] or emitting) else "inferred"
            if rnd["running"]:
                reasons.append(R_ROUND_RUNNING.format(round=rnd["n"] or 0,
                                                      seconds=int(rnd.get("started_s") or 0)))
            if nd is not None:
                age = _minutes_since(nd.get("at"), now)
                reasons.append(R_NUDGED.format(minutes=age) if age is not None
                               else R_NUDGED_UNKNOWN)
            if emitting:
                reasons.append(R_EMITTING)
            since = (nd.get("at") if nd else None) or rnd.get("last_ts")
        elif pend is not None:
            state_name, confidence = "queued", "inferred"
            reasons.append(R_PENDING.format(count=_as_int(pend.get("n")) or 0))
            since = pend.get("since")
        elif pres.get("online") or (post_age is not None and post_age <= ONLINE_WINDOW_S):
            state_name, confidence = "online", ("measured" if pres.get("online") else "inferred")
            if pres.get("online") and cursor_age is not None:
                reasons.append(R_CURSOR.format(seconds=int(cursor_age)))
            since = last_seen or post_ts
        else:
            state_name, confidence = "idle", "weak"
            reasons.append(R_QUIET.format(seconds=QUIET_WINDOW_S))
            since = post_ts
        rows.append({
            "role": role, "state": state_name, "confidence": confidence,
            "since": _as_str(since), "reasons": reasons, "up": bool(tab),
            "headless": headless, "harness": harness_kind, "model": spec.model,
            "writer": bool(spec.writer), "effort": _as_str(spec.effort),
            "model_expired": expired, "branch": branch, "worktree": worktree,
            "tab_id": (terminal_id(tab.get("tab_id")) if backend_name(state) == "herdr"
                       else _as_int(tab.get("tab_id"))),
            "pane_id": (terminal_id(tab.get("pane_id")) if backend_name(state) == "herdr"
                        else _as_int(tab.get("pane_id"))),
            "terminal": terminal_observation(state, role, now),
            "nudged_at": _as_str(nd.get("at")) if nd else None,
            "nudged_thread": _as_str(nd.get("id")) if nd else None,
            "nudged_by": _as_str(nd.get("by")) if nd else None,
            "escalated": bool(nd.get("escalated")) if nd else False,
            "pending": pend is not None,
            "context_tokens": _as_int(tokens), "checkpoint_at": checkpoint_at,
            "last_checkpoint": max((t for t in cp_ts if t), default=None),
            "last_nudge": ({"at": _as_str(nd.get("at")), "thread": _as_str(nd.get("id")),
                            "by": _as_str(nd.get("by"))} if nd else None),
            "busy": _is_busy(state, role),
            "round": rnd if headless else None,
            "stop_reason": stop_reason if stop_reason in STOP_REASONS else None,
            "awaiting": {"at": _as_str(aw.get("at")), "text": aw_text} if aw else None,
        })
    return {"session": state.get("session", cfg.channel), "channel": cfg.channel,
            "terminal_backend": backend_name(state), "workspace_id": state.get("workspace_id"),
            "issue": cfg.issue, "repo": cfg.repo, "roles": rows,
            "warnings": warnings, "generated": now_iso(), "stale": False,
            "checkpoints": state.get("checkpoints", []), "presence": presence}


def activity(paths: ClanPaths, *, samples: dict | None = None,
             state: object = _NO_STATE, stale: bool = False) -> dict:
    """The clan snapshot for callers that must not exit — the board's clan
    route. Raises `ClanError` (never `sys.exit`) so a request handler can answer
    200 while the CLI keeps its hard exit.

    `state` lets the board pass a snapshot it read under a non-blocking shared
    lock (the live file may be mid-write); `stale` marks a snapshot served
    because the lock was busy. `_NO_STATE` reads the file itself."""
    cfg, state = _read_clan(paths, state=state)
    out = _activity_from(paths, cfg, state, samples=samples)
    out["stale"] = bool(stale)
    return out


def status(paths: ClanPaths, screen: bool = False) -> dict:
    cfg, state = _load(paths)
    try:
        out = _activity_from(paths, cfg, state)
    except ClanError as e:
        sys.exit(f"ratel clan: {e}")     # models.toml, like clan.toml, exits cleanly
    z = _terminal(paths, state, cfg)
    out["attach"] = z.attach_command() if backend_name(state) == "herdr" else f"zellij attach {z.session}"
    for row in out["roles"]:
        wt = row.get("worktree")
        row["dirty"] = is_dirty(wt) if wt and Path(wt).exists() else False
        if screen and row.get("pane_id") is not None:
            row["screen"] = "\n".join(z.dump_screen(row["pane_id"]).splitlines()[-20:])
    return out


PROPOSE_TEXT = ("Clan proposal for #{issue} — confirm on the board or amend in the "
                "orchestrator tab")


def propose(paths: ClanPaths, file: str, auto_approve: bool = False) -> dict:
    """Read a JSON proposal `{"roles": [...]}`, validate it, write it to
    `clan/proposal.json`, and post it pinned on the bus as `orchestrator`."""
    cfg, _state = _load(paths)
    if file == "-":
        proposal = json.loads(sys.stdin.read())
    else:
        proposal = json.loads(Path(file).read_text())
    att = {"type": "clan", "status": "proposed", "issue": cfg.issue,
           "roles": proposal.get("roles")}
    validate_clan_attachment(att, clan_catalog(paths.home))
    paths.clan_toml.parent.mkdir(parents=True, exist_ok=True)
    channel_path(paths.home, paths.channel, "clan", "proposal.json").write_text(json.dumps(att, indent=2))
    bus = Bus(paths.home, cfg.channel)
    msg = bus.post("orchestrator", PROPOSE_TEXT.format(issue=cfg.issue),
                   attachments=[att], pin=True)
    out = {"id": msg["id"], "roles": [r["name"] for r in att["roles"]]}
    if auto_approve:
        approved = {**att, "status": "approved", "supersedes": msg["id"]}
        confirmation = bus.post("stakeholder", "Clan approved", attachments=[approved],
                                expected_proposal=msg["id"])
        out["result"] = approve(paths, confirmation["id"])
    return out


def _clan_attachments(bus_msgs: list[dict]) -> list[tuple[dict, dict]]:
    """(message, attachment) for every clan attachment on the bus, oldest first."""
    found = []
    for m in bus_msgs:
        for a in m.get("attachments") or []:
            if isinstance(a, dict) and a.get("type") == "clan":
                found.append((m, a))
    return found


def approve(paths: ClanPaths, msg_id: str | None = None) -> dict:
    """Recover an interrupted application, then atomically prepare the newest decision."""
    bus = Bus(paths.home, paths.channel)
    approval.recover(paths, bus.store)
    with bus.store.connection(write=True) as con:
        # Another applier may have prepared an intent after our recovery. Do
        # not overwrite it or start lifecycle commands against half-applied files.
        if bus.store.pending(con) is not None:
            raise ValueError("approval application is pending — retry clan approve to recover it")
        clan_atts = _clan_attachments([json.loads(row[1]) for row in bus.store.rows(con)])
        if msg_id is not None:
            matches = [(m, a) for m, a in clan_atts if m["id"] == msg_id]
            if not matches:
                sys.exit(f"ratel clan approve: no clan attachment on message {msg_id}")
            msg, att = matches[-1]
            if msg["from"] != "stakeholder":
                sys.exit(f"ratel clan approve: message {msg_id} is from {msg['from']!r}, "
                         "not stakeholder — approvals are only trusted from the board")
        else:
            approved = [(m, a) for m, a in clan_atts
                        if a.get("status") == "approved" and m["from"] == "stakeholder"]
            if not approved:
                sys.exit("ratel clan approve: no approved clan proposal on the bus")
            msg, att = approved[-1]
        if att.get("status") != "approved":
            raise ValueError("attachment status must be approved")
        try:
            bus.store.check_proposal(con, att.get("supersedes"))
        except ValueError as e:
            sys.exit(f"ratel clan approve: {e}")
        done = approval.completed(con, msg["id"])
        if done is not None:
            return done
        cfg, state = _load(paths)
        validate_clan_attachment(att, clan_catalog(paths.home))
        if att["issue"] != cfg.issue:
            raise ValueError("attachment issue does not match this clan")
        chosen = clan_config_from(att, cfg, paths.home)
        approval.prepare(con, chosen, state, msg["id"])
    # This separate transaction leaves a durable intent if either file write
    # fails. Recovery uses the resolved snapshot rather than today's catalog.
    recovered = approval.recover(paths, bus.store)
    if recovered is not None:
        return recovered[1]
    # A concurrent retry may already have completed this exact intent.
    with bus.store.connection() as con:
        done = approval.completed(con, msg["id"])
        if done is None:
            raise ValueError("approval application is pending — retry clan approve")
        return done


def nudge(paths: ClanPaths, role: str, text: str | None = None) -> dict:
    cfg, state = _load(paths)
    tab = _tab(state, role)
    pane = tab.get("pane_id")
    if pane is None:                         # a slow start: re-resolve once
        pane = _terminal(paths, state, cfg).pane_or_none(role) if tab else None
        if pane is None:                     # one message for both no-record and no-pane
            sys.exit(f"ratel clan nudge: no pane for {role} — is it up?")
        update_state(paths, lambda s, pane=pane: s["tabs"][role].__setitem__("pane_id", pane))
    msgs = Bus(paths.home, cfg.channel).read_all()
    text = text or MENTION.format(role=role, n=0, channel=cfg.channel, senders="human",
                                  thread=msgs[-1]["id"] if msgs else "the newest message")
    if backend_name(state) == "herdr":
        from .watch import queue_control
        queue_control(paths, role, text)
        return {"role": role, "pane": pane, "text": text, "queued": True}
    _terminal(paths, state, cfg).nudge(pane, text)
    return {"role": role, "pane": pane, "text": text}


REORIENT = ("ratel: fresh context after checkpoint — call catch_up, read the pins and the "
            "clan table, then {tail}")
CLEAR_KEYS = {"claude": "/clear", "opencode": "/new", "fake": "/clear"}
ORCHESTRATOR = "orchestrator"   # the role whose context is the run's state


def _is_busy(state: dict, role: str) -> bool:
    """A role is busy mid-round when the watcher has work queued for it, or
    when it has not posted since its last nudge — `Watcher.once()` pops
    `watch["nudged"][role]` on any bus post from that role, so membership in
    `nudged` IS "has not answered yet". No timestamps compared: the watcher
    stores `time.monotonic()` floats there, not clock time.

    The one definition: `checkpoint`'s refusal and the map's busy dot both read
    it, so they cannot drift."""
    watch = state.get("watch") or {}
    controls = state.get("terminal_controls") or {}
    return (role in (watch.get("pending") or {}) or role in (watch.get("nudged") or {})
            or any(c.get("role") == role for c in controls.values() if isinstance(c, dict)))


def checkpoint(paths: ClanPaths, role: str, mode: str = "clear", force: bool = False,
               reason: str = "manual") -> dict:
    """Reset one role's context: type the harness's clear/compact command into
    an interactive pane, or leave a reset file the headless loop consumes."""
    if mode not in ("clear", "compact"):
        sys.exit(f"ratel clan checkpoint: unknown mode {mode!r} (clear or compact)")
    cfg, state = _load(paths)
    # The orchestrator's context IS the run's state: every other role is
    # disposable between tasks, this one is not. Compact keeps the session;
    # clear does not, and no flag overrides that.
    if role == ORCHESTRATOR and mode == "clear":
        sys.exit(json.dumps({"role": role, "reason": "orchestrator is never cleared",
                             "hint": "use --mode compact"}))
    # A checkpoint is delivered by typing into the role's pane, then typing a
    # re-orient line two seconds later. A role running this against itself is
    # typing into a pane that is mid-turn — the lossy case of Decision 25 —
    # and if /clear lands but the re-orient does not, nobody ever nudges it.
    if not force and os.environ.get("AGENT_NAME") == role:
        sys.exit(json.dumps({"role": role, "reason": "self-checkpoint",
                             "hint": "a role cannot checkpoint itself; ask the operator, "
                                     "or pass --force"}))
    spec = cfg.roles.get(role)
    tab = _tab(state, role)
    if spec is None or not tab:
        sys.exit(json.dumps({"role": role, "reason": "not up"}))
    if not force and _is_busy(state, role):
        sys.exit(json.dumps({"role": role, "reason": "busy",
                             "hint": "pass --force to checkpoint anyway"}))
    kind = tab.get("harness") or spec.harness
    hd = paths.harness_dir(role)
    tokens = context_tokens({**tab, "harness": kind, "harness_dir": str(hd)})
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if kind in harness.HEADLESS_KINDS:
        hd.mkdir(parents=True, exist_ok=True)
        (hd / "reset").touch()               # the round loop rewinds itself
    elif kind in CLEAR_KEYS:
        pane = tab.get("pane_id")
        if pane is None:
            sys.exit(json.dumps({"role": role, "reason": "not up"}))
        z = _terminal(paths, state, cfg)
        if backend_name(state) == "herdr":
            z.nudge(pane, "/compact" if mode == "compact" else CLEAR_KEYS[kind])
        else:
            z.write_chars(pane, "/compact" if mode == "compact" else CLEAR_KEYS[kind])
            z.press_enter(pane)                  # the command runs before the nudge lands
        time.sleep(2)
        if mode == "clear":
            # /clear (and /new) rotate the session file: the pinned session_id
            # would point at the closed transcript forever. Re-pin by launch
            # time — discovery finds exactly the post-clear session (compact
            # continues the same session, so its pin stays).
            update_state(paths, lambda s: (s["tabs"][role].pop("session_id", None),
                                           s["tabs"][role].update({"launched": ts})))
        msgs = Bus(paths.home, cfg.channel).read_all()
        # the re-orient names the role's OWN thread: its last nudge record if
        # present, else the newest dispatch mentioning it — never a stranger's
        nudged_id = ((state.get("watch") or {}).get("nudged") or {}).get(role, {}).get("id") or ""
        own = [m for m in msgs if role in (m.get("mentions") or [])]
        thread = nudged_id if is_ulid(nudged_id) else (safe_thread(own[-1]) if own else "")
        tail = f"continue thread {thread} or wait for the next dispatch." if thread \
            else "wait for the next dispatch."
        if backend_name(state) == "herdr":
            from .watch import queue_control
            queue_control(paths, role, REORIENT.format(tail=tail))
        else:
            z.nudge(pane, REORIENT.format(tail=tail))
    else:
        sys.exit(json.dumps({"role": role, "reason": f"unknown harness {kind!r}"}))
    record = {"role": role, "mode": mode, "ts": ts,
              "context_tokens": tokens, "reason": reason}
    update_state(paths, lambda s: s.setdefault("checkpoints", []).append(record))
    return record


def sync(paths: ClanPaths, role: str) -> dict:
    cfg, state = _load(paths)
    wt = _tab(state, role).get("worktree")
    if not wt:
        sys.exit(f"ratel clan sync: {role} has no worktree")
    return {"role": role, "worktree": wt, "head": sync_detached(cfg.checkout, wt, f"issue-{cfg.issue}")}


def down(paths: ClanPaths, prune_worktrees: bool = False, force: bool = False) -> dict:
    cfg, state = _load(paths)
    if force and not prune_worktrees:
        raise ValueError("--force requires --prune-worktrees")
    if prune_worktrees:
        for role, tab in (state.get("tabs") or {}).items():
            if not tab.get("worktree"):
                continue
            path = Path(tab["worktree"])
            if role not in cfg.roles or path.resolve() != _worktree_for(cfg, role).resolve():
                raise ValueError("recorded worktree is outside this clan's expected layout; refusing to prune")
            if path.exists() and not force and is_dirty(path):
                raise ValueError(f"{path} has uncommitted changes — use --force with --prune-worktrees to discard them")
    _terminal(paths, state, cfg).kill()
    killed_rounds = 0
    for role in cfg.roles:                       # a round outlives its pane: kill its group
        if harness.terminate_round(paths.harness_dir(role) / "round.pid"):
            killed_rounds += 1
    removed = []
    if prune_worktrees:
        for tab in (state.get("tabs") or {}).values():
            if tab.get("worktree"):
                remove_worktree(cfg.checkout, tab["worktree"], force=force)
                removed.append(tab["worktree"])
    update_state(paths, lambda s: s.update(tabs={}, maintenance_ready=True))
    return {"session": state.get("session", cfg.channel), "terminal_backend": backend_name(state),
            "workspace_id": state.get("workspace_id"), "worktrees_removed": removed,
            "rounds_killed": killed_rounds}
