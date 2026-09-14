"""Tail the channel, type a one-line nudge into the pane of whoever was mentioned.

An idle interactive agent is blocked on its own prompt; it does not poll the
bus. The watcher is what makes `@developer` reach a developer who is sitting
there waiting. It is not an agent: it keeps its own offset in
`clan.state.json` and never updates agent cursors, because those belong
to the agents and the board's unread and presence are computed from them.

It has one bus write: when a role's pane shows a harness permission dialog
(`prompts.detect_prompt`), it posts one `@stakeholder` line as `ratel` — the
orchestrator cannot answer another role's dialog, and a "re-dispatch" nudge
would only pile up behind the modal. Nothing trusts the `ratel` sender
(approvals trust `stakeholder`, proposals `orchestrator`), so the write adds
no trust surface; `stakeholder` has no tab, so the mention is never routed
back into a pane.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..bus import Bus, now_iso
from ..ulid import is_ulid
from .budget import stopped_reason
from .config import ClanPaths, read_state, update_state
from .context import context_tokens
from .prompts import _CTRL, detect_prompt
from .zellij import ZELLIJ_ERRORS

MENTION = ("ratel: @{role} {n} new on #{channel} from {senders} — call catch_up now, "
           "act on the latest mention, reply in thread {thread}. Do not wait_for_mention.")
VERDICT = 'ratel: @{role} {sender} posted "{verdict}" in thread {thread} — gate on it.'
STALE = ("ratel: @{role} {who} has not posted {minutes} minutes after a nudge — "
         "check its tab or re-dispatch.")
AWAITING = "@stakeholder {who} is waiting on a permission prompt in tab {tab}: {text}"
WATCH_SENDER = "ratel"
# Headless kinds are never probed: harness.PROMPT_MARKERS already kills a
# headless round on a dialog and its `prompt` outcome reads as stuck. Named
# here rather than imported — the watcher must not import session.
HEADLESS_KINDS = ("claude-p", "opencode-run")


def safe_thread(m: dict) -> str:
    """A thread id goes into typed keystrokes; anything that is not a ULID
    (an id from a post whose parent failed validation, or old traffic) types
    as the empty string."""
    t = m.get("parent") or m["id"]
    return t if is_ulid(t) else ""


def _one_line(text: str, cap: int = 120) -> str:
    """The verdict line is typed too: one line, no control chars, capped."""
    return text.splitlines()[0].translate(_CTRL)[:cap]


@dataclass
class Nudge:
    role: str
    pane: int
    text: str
    kind: str = "mention"
    n: int = 1
    senders: list[str] = field(default_factory=list)
    thread: str | None = None


class Watcher:
    def __init__(self, bus: Bus, paths: ClanPaths, zellij: Any, debounce_s: float = 20,
                 stale_s: float = 600, orchestrator: str = "orchestrator",
                 clock: Callable[[], float] = time.monotonic,
                 measure: Callable[[dict], int | None] | None = None,
                 measure_every_s: float = 30, thresholds: dict[str, int] | None = None,
                 checkpoint: Callable[[str, str, str], Any] | None = None,
                 probe_every_s: float = 30):
        self.bus = bus
        self.paths = paths
        self.zellij = zellij
        self.debounce_s = debounce_s
        self.stale_s = stale_s
        self.orchestrator = orchestrator
        self.clock = clock
        self.measure = measure or context_tokens   # default: the real measurement
        self.measure_every_s = measure_every_s
        self.thresholds = thresholds or {}
        self.checkpoint = checkpoint
        self.probe_every_s = probe_every_s
        self._measured: dict[str, float] = {}   # throttle only — never persisted,
        # because a persisted time.monotonic() is boot-relative and dies at reboot
        self._probed: dict[str, float] = {}     # same rule, for screen probes

    # ---- pure -----------------------------------------------------------
    def _mention_text(self, role: str, n: int, senders: list[str], thread: str | None) -> str:
        return MENTION.format(role=role, n=n, channel=self.bus.channel,
                              senders=", ".join(senders), thread=thread)

    def route(self, msgs: list[dict], tabs: dict[str, int]) -> list[Nudge]:
        """What this batch deserves, delivered by nobody. Mentions fold per role."""
        folded: dict[str, Nudge] = {}
        verdicts: list[Nudge] = []
        for m in msgs:
            thread = safe_thread(m)
            if m["text"].startswith("VERDICT:") and self.orchestrator in tabs \
                    and m["from"] != self.orchestrator:
                verdicts.append(Nudge(
                    role=self.orchestrator, pane=tabs[self.orchestrator], kind="verdict",
                    thread=thread, senders=[m["from"]],
                    text=VERDICT.format(role=self.orchestrator, sender=m["from"],
                                        verdict=_one_line(m["text"]), thread=thread)))
            for role in m.get("mentions", []):
                if role not in tabs or role == m["from"]:
                    continue
                cur = folded.get(role)
                bump = [m["from"]] if not cur or m["from"] not in cur.senders else []
                senders = (cur.senders if cur else []) + bump
                folded[role] = Nudge(role=role, pane=tabs[role], n=(cur.n + 1) if cur else 1,
                                     senders=senders, thread=thread, text="")
        for n in folded.values():
            n.text = self._mention_text(n.role, n.n, n.senders, n.thread)
        return list(folded.values()) + verdicts

    # ---- stateful -------------------------------------------------------
    def _tabs(self, state: dict) -> dict[str, int]:
        """Pane ids per role. A `None` recorded on a slow server (`clan new`
        tolerated the pane lag) is re-resolved once and written back."""
        tabs = {}
        for r, v in (state.get("tabs") or {}).items():
            if stopped_reason(state, r):
                continue
            pane = v.get("pane_id")
            if pane is None:
                # The router must outlive a dead server; nudge is the caller
                # that surfaces it. 0.5 s, retried on the next 1 s tick.
                try:
                    pane = self.zellij.pane_or_none(r, timeout_s=0.5)
                except FileNotFoundError:      # a missing binary is never skipped silently
                    raise
                except ZELLIJ_ERRORS:
                    continue
            if pane is None:
                continue
            if pane != v.get("pane_id"):
                update_state(self.paths, lambda s, r=r, pane=pane:
                             s.setdefault("tabs", {}).setdefault(r, {}).__setitem__("pane_id", pane))
            tabs[r] = pane
        return tabs

    def _stale(self, watch: dict, tabs: dict[str, int]) -> list[Nudge]:
        """A role nudged long ago that has said nothing since: tell the orchestrator, once."""
        out = []
        if self.orchestrator not in tabs:
            return out
        awaiting = watch.get("awaiting") or {}
        for role, info in (watch.get("nudged") or {}).items():
            elapsed = self.clock() - info.get("ts", 0)
            if role == self.orchestrator or info.get("escalated") or elapsed < self.stale_s:
                continue
            if role not in tabs or role in awaiting:            # blocked on a dialog: the stakeholder's, not the
                continue                    # orchestrator's — and not silence
            info["escalated"] = True
            out.append(Nudge(role=self.orchestrator, pane=tabs[self.orchestrator], kind="stale",
                             text=STALE.format(role=self.orchestrator, who=role,
                                               minutes=int(elapsed // 60))))
        return out

    def seed(self) -> str | None:
        """Adopt the channel tip on a first start, nudging nobody.

        A watcher pointed at a channel that already has history — a restart with
        wiped state, or `clan watch` on a running clan — would otherwise type every
        past mention into the panes at once. A restart with saved state keeps its
        offset and still catches what it missed.
        """
        watch = dict(read_state(self.paths).get("watch") or {})
        if "last_id" in watch:
            return watch["last_id"]
        msgs = self.bus.read_all()
        watch["last_id"] = msgs[-1]["id"] if msgs else None
        update_state(self.paths, lambda s: s.__setitem__("watch", watch))
        return watch["last_id"]

    def once(self) -> list[Nudge]:
        state = read_state(self.paths)
        watch = dict(state.get("watch") or {})
        watch.setdefault("nudged", {})
        watch.setdefault("pending", {})
        watch.setdefault("awaiting", {})
        tabs = self._tabs(state)

        msgs = self.bus.read_since(watch.get("last_id"))
        for m in msgs:                      # a role that spoke is neither silent nor owed a nudge
            watch["nudged"].pop(m["from"], None)
            watch["awaiting"].pop(m["from"], None)   # nor blocked on a dialog

        routed = self.route(msgs, tabs)
        for n in (r for r in routed if r.kind == "mention"):
            p = watch["pending"].get(n.role)
            if p is None:
                p = watch["pending"][n.role] = {"n": 0, "senders": [], "thread": None}
            p.setdefault("since", now_iso())    # an entry predating `since` gets one
            p["n"] += n.n
            p["senders"] += [s for s in n.senders if s not in p["senders"]]
            p["thread"] = n.thread
        sending = [n for n in routed if n.kind != "mention"]

        now = self.clock()
        for role, p in list(watch["pending"].items()):
            last = (watch["nudged"].get(role) or {}).get("ts")
            # `last` can be from a previous boot and larger than a fresh clock:
            # `now - last` is then negative, which is unknown age, not "just
            # nudged". Only a real gap inside the window folds the mention in.
            if last is not None and 0 <= now - last < self.debounce_s:
                continue                    # fold into the next one rather than typing twice
            if role not in tabs:
                continue
            sending.append(Nudge(role=role, pane=tabs[role], n=p["n"], senders=p["senders"],
                                 thread=p["thread"],
                                 text=self._mention_text(role, p["n"], p["senders"], p["thread"])))

        self._probe_prompts(watch, tabs, state, now)
        sending += self._stale(watch, tabs)
        for n in sending:
            # Pane resolution is already defensive (`_tabs`); delivery is not.
            # A killed pane, a stale id or a dead server must cost that one
            # nudge, not the whole watch tab. A missing binary stays loud.
            try:
                self.zellij.nudge(n.pane, n.text)
            except FileNotFoundError:
                raise
            except ZELLIJ_ERRORS as e:
                print(f"ratel clan watch: nudge to {n.role} failed: {e!r} "
                      "(pane gone or server dead) — will retry next tick", file=sys.stderr)
                continue
            if n.kind != "stale":
                watch["nudged"][n.role] = {"id": n.thread or "", "ts": now,
                                           "at": now_iso(), "escalated": False}
                if n.kind == "mention":
                    # only a delivered nudge consumes the fold; a failed one
                    # stays pending and the next tick retries it
                    watch["pending"].pop(n.role, None)
        if msgs:
            watch["last_id"] = msgs[-1]["id"]
        self._compact_tick(watch, tabs, state, now)
        update_state(self.paths, lambda s: s.__setitem__("watch", watch))
        return sending

    def _probe_prompts(self, watch: dict, tabs: dict[str, int], state: dict, now: float) -> None:
        """Dump each interactive role's pane on a slow cadence and record a
        harness permission dialog under `watch["awaiting"]`. Every up role is
        probed, not only the nudged ones: the orchestrator is never in
        `nudged` (Decision 38) yet can hit a prompt, and a role leaves `nudged`
        on its first post but may hit a dialog later in the same task. One
        `dump-screen` per role per `probe_every_s`.

        The record is made once per occurrence and the stakeholder is posted
        once; a dialog that persists never re-posts. When the screen moves on
        the record is dropped, and a nudge clock on that role restarts from
        the unblock — otherwise `_stale` would fire the instant the operator
        cleared a fifteen-minute dialog."""
        for role, pane in tabs.items():
            if role in ("watch", "bus"):
                continue
            tab = (state.get("tabs") or {}).get(role) or {}
            if tab.get("harness") in HEADLESS_KINDS:
                continue
            last = self._probed.get(role)
            if last is not None and now - last < self.probe_every_s:
                continue
            self._probed[role] = now
            try:
                screen = self.zellij.dump_screen(pane)
            except FileNotFoundError:        # a missing binary is never skipped silently
                raise
            except ZELLIJ_ERRORS:
                continue                     # pane gone or server dead: this probe, not the tick
            found = detect_prompt(screen)
            if found is None:
                if watch["awaiting"].pop(role, None) is not None:
                    nudged = watch["nudged"].get(role)
                    if nudged is not None:
                        nudged["ts"] = now
                        nudged["at"] = now_iso()
                continue
            rec = watch["awaiting"].get(role)
            if not isinstance(rec, dict):
                tab_id = tab.get("tab_id")
                rec = watch["awaiting"][role] = {
                    "at": now_iso(), "ts": now, "kind": found.kind, "family": found.family,
                    "tab_id": tab_id if isinstance(tab_id, int) and not isinstance(tab_id, bool)
                    else None,
                    "text": found.text, "escalated": False}
            if not rec.get("escalated"):
                self.bus.post(WATCH_SENDER, AWAITING.format(
                    who=role, tab=rec["tab_id"] if rec["tab_id"] is not None else "?",
                    text=rec["text"]))
                rec["escalated"] = True

    def _compact_tick(self, watch: dict, tabs: dict[str, int], state: dict, now: float) -> None:
        """Measure idle roles; compact through the checkpoint callback when a
        role is over its threshold. `session.watch` wires the callback to
        `session.checkpoint` (the watcher must not import session) — a busy
        refusal raises SystemExit, which is swallowed so the next tick retries.
        Automatic checkpoints are always compact, never clear."""
        if not self.checkpoint:
            return
        watch.setdefault("compacted", {})
        for role in tabs:
            if role in ("watch", "bus") or role in watch["pending"] or role in watch["nudged"]:
                continue
            if role in watch.get("awaiting", {}):   # never type /compact into a dialog
                continue
            if role == self.orchestrator:   # the nudger is never in nudged, so it always
                continue                    # looks idle here; the operator compacts it
            last = self._measured.get(role)
            if last is not None and now - last < self.measure_every_s:
                continue
            tab = (state.get("tabs") or {}).get(role) or {}
            tokens = self.measure({**tab, "harness_dir": str(self.paths.harness_dir(role))})
            self._measured[role] = now
            if tokens is None:
                continue
            threshold = self.thresholds.get(role)
            if threshold is None:
                continue
            if tokens > threshold:
                if role in watch["compacted"]:
                    continue
                try:
                    self.checkpoint(role, "compact", "threshold")
                    watch["compacted"][role] = {
                        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "tokens": tokens}
                except SystemExit:
                    pass                      # busy: measured stays, retry next tick
            else:
                watch["compacted"].pop(role, None)   # back under the line: re-arm

    def run(self, poll_s: float = 1.0, until: Callable[[], bool] | None = None) -> None:
        self.seed()
        while until is None or not until():
            self.once()
            time.sleep(poll_s)
