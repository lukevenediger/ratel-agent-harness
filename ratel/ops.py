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
        return self.bus.post(self.agent, text, parent=parent, attachments=attachments,
                             pin=pin, advance_sender=True)["id"]

    def read_channel(self, since: str | None = None, limit: int | None = None) -> list[dict]:
        """Messages after a public id, in append order; consume atomically."""
        return self._others(self.bus.consume(self.agent, since=since, limit=limit))

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
