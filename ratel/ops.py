"""Channel operations for one agent: cursor and self-exclusion semantics.

The MCP tools and the `ratel` CLI are thin wrappers over this class, so both
surfaces move cursors identically and the board's presence/unread stays truthful.
"""
from __future__ import annotations

from typing import Any

from .bus import Bus


class AgentOps:
    def __init__(self, bus: Bus, agent: str):
        self.bus = bus
        self.agent = agent

    # ---- internals -----------------------------------------------------
    def _tip(self) -> str | None:
        msgs = self.bus.read_all()
        return msgs[-1]["id"] if msgs else None

    def _advance(self, to_id: str | None) -> None:
        """Move the cursor forward only; otherwise just refresh presence."""
        cur = self.bus.get_cursor(self.agent)
        if to_id and (cur is None or to_id > cur):
            self.bus.set_cursor(self.agent, to_id)
        else:
            self.bus.touch_cursor(self.agent)

    def _others(self, msgs: list[dict]) -> list[dict]:
        return [m for m in msgs if m["from"] != self.agent]

    # ---- operations ----------------------------------------------------
    def post(self, text: str, parent: str | None = None,
             attachments: list[dict] | None = None, pin: bool = False) -> str:
        """Post a message; returns its id. Posting while caught up keeps you caught up.

        `attachments` takes `attach_file`'s return value **unchanged** — do not
        rebuild or trim it. A missing `type` is inferred from the shape (`ref` →
        file, `url` → link, `body` → code, `items` → tasks).
        """
        at_tip = self.bus.get_cursor(self.agent) == self._tip()
        msg = self.bus.post(self.agent, text, parent=parent, attachments=attachments, pin=pin)
        if at_tip:
            self.bus.set_cursor(self.agent, msg["id"])
        else:
            self.bus.touch_cursor(self.agent)
        return msg["id"]

    def read_channel(self, since: str | None = None, limit: int | None = None) -> list[dict]:
        """Messages after `since` (default: your cursor), oldest first, excluding your own."""
        cur = since if since is not None else self.bus.get_cursor(self.agent)
        msgs = self.bus.read_since(cur, limit)
        self._advance(msgs[-1]["id"] if msgs else None)
        return self._others(msgs)

    def read_thread(self, id: str) -> dict[str, Any]:
        t = self.bus.read_thread(id)
        if t is None:
            raise ValueError(f"no message {id}")
        return t

    def wait_for_mention(self, timeout_s: float = 60, any_message: bool = False) -> list[dict]:
        """Block until mentioned (or any new message), then return everything since your cursor."""
        got = self.bus.wait_for_new(self.agent, timeout_s, not any_message)
        if not got:
            self.bus.touch_cursor(self.agent)
        return self._others(got)

    def pin(self, id: str) -> str:
        return self.bus.post(self.agent, "", pin=id)["id"]

    def unpin(self, id: str) -> str:
        return self.bus.post(self.agent, "", unpin=id)["id"]

    def pins(self) -> list[dict]:
        return self.bus.pins()

    def attach_file(self, path: str, name: str | None = None) -> dict[str, Any]:
        return self.bus.attach_file(path, name)

    def catch_up(self) -> dict[str, Any]:
        return {"pins": self.bus.pins(), "messages": self.read_channel()}
