"""ChannelModel: the timeline's view-model. Pure Python — no textual."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
from typing import Callable

from ..history import matches
from .render import day, day_label


@dataclass(frozen=True)
class Row:
    kind: str                      # "day" | "new" | "msg"
    label: str = ""
    msg: dict | None = None


def _today() -> date:
    return datetime.now(timezone.utc).astimezone().date()


class ChannelModel:
    def __init__(self, channel: str, today: Callable[[], date] | None = None, tz: tzinfo | None = None):
        self.channel = channel
        self.today = today or _today
        self.tz = tz
        self.messages: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.pins: list[dict] = []
        self.heads: dict = {"proposed": None, "approved": None}
        self.tip: str | None = None
        self.next_before: str | None = None
        self.query, self.mention, self.operator = "", "", False
        self.cursor = -1                 # index into visible(); -1 = nothing selected
        self.new_id: str | None = None   # first live arrival the reader has not reached
        self._visible: list[dict] | None = None

    # -- ingest -------------------------------------------------------------

    def _add(self, msgs: list[dict]) -> list[dict]:
        added = []
        for m in msgs:
            mid = m.get("id")
            if not isinstance(mid, str) or mid in self.by_id:
                continue
            self.by_id[mid] = m
            added.append(m)
        self._visible = None
        return added

    def load_page(self, page: dict) -> None:
        """The initial page: replaces everything loaded so far."""
        self.messages, self.by_id, self._visible = [], {}, None
        self.new_id = None
        self.messages = self._add(page.get("messages", []))
        self.tip = page.get("tip")
        self.next_before = page.get("next_before")
        heads = page.get("heads")
        if isinstance(heads, dict):
            self.heads = heads
        self.cursor = len(self.visible()) - 1

    def prepend_page(self, page: dict) -> int:
        """An older page in front of the loaded rows; the selection stays on its message."""
        selected = self.selected()
        older = self._add(page.get("messages", []))
        self.messages = older + self.messages
        self.next_before = page.get("next_before")
        if selected is not None:
            self.select_id(selected["id"])
        return len(older)

    def ingest(self, msgs: list[dict], live: bool = True) -> list[dict]:
        """Append new messages (deduped by id). Live arrivals raise the NEW divider
        when the reader is not at the bottom, and follow the bottom when they are."""
        was_bottom = self.at_bottom
        added = self._add(msgs)
        if not added:
            return added
        self.messages.extend(added)
        if isinstance(added[-1].get("id"), str):
            self.tip = added[-1]["id"]
        visible_new = [m for m in added if self._match(m)]
        if live and visible_new:
            if was_bottom:
                self.select_last()
            elif self.new_id is None:
                self.new_id = visible_new[0]["id"]
        return added

    def set_pins(self, pins: list[dict], heads: dict | None = None) -> None:
        self.pins = [p for p in pins if isinstance(p, dict) and isinstance(p.get("id"), str)]
        if isinstance(heads, dict):
            self.heads = heads

    # -- filter -------------------------------------------------------------

    def _match(self, m: dict) -> bool:
        return matches(m, self.query, self.mention, self.operator)

    def set_filter(self, query: str = "", mention: str = "", operator: bool = False) -> None:
        selected = self.selected()
        self.query, self.mention, self.operator = query, mention, operator
        self._visible = None
        if selected is None or not self.select_id(selected["id"]):
            self.cursor = len(self.visible()) - 1

    def filter_label(self) -> str:
        parts = []
        if self.query:
            parts.append(f"q:{self.query}")
        if self.mention:
            parts.append(f"@{self.mention}")
        if self.operator:
            parts.append("operator")
        return " ".join(parts)

    def visible(self) -> list[dict]:
        if self._visible is None:
            self._visible = [m for m in self.messages if self._match(m)]
        return self._visible

    # -- rows ---------------------------------------------------------------

    def rows(self) -> list[Row]:
        out: list[Row] = []
        today = self.today()
        last_day = None
        for m in self.visible():
            d = day(m.get("ts"), self.tz)
            if d is not None and d != last_day:
                out.append(Row("day", day_label(d, today)))
                last_day = d
            if m["id"] == self.new_id:
                out.append(Row("new", "NEW"))
            out.append(Row("msg", msg=m))
        return out

    def reply_count(self, msg_id: str) -> int:
        return sum(1 for m in self.messages if m.get("parent") == msg_id)

    def agents(self) -> list[str]:
        seen: list[str] = []
        for m in self.messages:
            sender = m.get("from")
            if isinstance(sender, str) and sender not in seen:
                seen.append(sender)
        return seen

    def newest_pin(self) -> dict | None:
        return self.pins[-1] if self.pins else None

    def tasks_progress(self) -> tuple[int, int] | None:
        for pin in reversed(self.pins):
            for att in pin.get("attachments", []) if isinstance(pin.get("attachments"), list) else []:
                if isinstance(att, dict) and att.get("type") == "tasks" and isinstance(att.get("items"), list):
                    items = [i for i in att["items"] if isinstance(i, dict)]
                    return sum(1 for i in items if i.get("done") is True), len(items)
        return None

    # -- cursor -------------------------------------------------------------

    @property
    def at_bottom(self) -> bool:
        return self.cursor >= len(self.visible()) - 1

    def selected(self) -> dict | None:
        rows = self.visible()
        return rows[self.cursor] if 0 <= self.cursor < len(rows) else None

    def _land(self) -> None:
        sel = self.selected()
        if sel is not None and sel["id"] == self.new_id:
            self.new_id = None

    def move(self, delta: int) -> None:
        n = len(self.visible())
        if n:
            self.cursor = max(0, min(n - 1, (self.cursor if self.cursor >= 0 else n - 1) + delta))
        self._land()

    def select_first(self) -> None:
        self.cursor = 0 if self.visible() else -1
        self._land()

    def select_last(self) -> None:
        self.cursor = len(self.visible()) - 1
        self._land()

    def select_id(self, msg_id: str) -> bool:
        for i, m in enumerate(self.visible()):
            if m["id"] == msg_id:
                self.cursor = i
                self._land()
                return True
        return False

    def clear_new(self) -> None:
        self.new_id = None

    def jump_new(self) -> bool:
        return self.new_id is not None and self.select_id(self.new_id)
