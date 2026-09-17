"""Channel list (newest activity first, with count and repo) and the presence
dots for the open channel. Dot glyphs are keyed by the clan's node-state
vocabulary (ratel.clan.session.ACTIVITY_STATES), never a copy of it."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from ...clan.session import ACTIVITY_STATES
from ..render import MUTED, scrub

DOTS = {"online": ("●", ""), "offline": ("○", MUTED)}
assert set(DOTS) <= set(ACTIVITY_STATES)


def presence_state(row: dict) -> str:
    return "online" if row.get("online") is True else "offline"


class ChannelRow(Static):
    def __init__(self, row: dict, current: bool, ordinal: int) -> None:
        name = row["name"]
        text = Text()
        text.append(f"{ordinal} " if ordinal <= 9 else "  ", MUTED)
        text.append(name, "bold" if current else "")
        detail = " · ".join(s for s in (str(row.get("count", 0)), row.get("repo") or "") if s)
        text.append("\n  " + detail, MUTED)
        super().__init__(text, name=name, classes="channel" + (" -current" if current else ""))


class Sidebar(VerticalScroll):
    can_focus = True
    BINDINGS = [
        Binding("j,down", "cursor(1)", "down", show=False),
        Binding("k,up", "cursor(-1)", "up", show=False),
        Binding("g", "ends(0)", "first", show=False),
        Binding("G", "ends(-1)", "last", show=False),
        Binding("enter", "activate", "select", show=False),
    ]

    class Selected(Message):
        def __init__(self, channel: str) -> None:
            super().__init__()
            self.channel = channel

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.channels: list[dict] = []
        self.agents: list[dict] = []
        self.index = 0
        self._rows: list[ChannelRow] = []
        self._colour_for = lambda name: ""

    def compose(self) -> ComposeResult:
        yield Static(self._agents_text(), id="agents")

    def set_channels(self, channels: list[dict], current: str | None) -> None:
        self.channels = channels
        self._rows = [ChannelRow(r, r["name"] == current, i + 1) for i, r in enumerate(channels)]
        self.query(ChannelRow).remove()
        self.mount(*self._rows, before="#agents")
        names = [r["name"] for r in channels]
        self.select(names.index(current) if current in names else 0)

    def set_agents(self, presence: list[dict], colour_for) -> None:
        self._colour_for = colour_for
        self.agents = [{"agent": scrub(r.get("agent")), "state": presence_state(r)}
                       for r in presence if isinstance(r, dict) and isinstance(r.get("agent"), str)]
        self.query_one("#agents", Static).update(self._agents_text())

    def _agents_text(self) -> Text:
        out = Text("\nagents", MUTED)
        for a in self.agents:
            glyph, style = DOTS[a["state"]]
            out.append("\n" + glyph + " ", style or self._colour_for(a["agent"]))
            out.append(a["agent"], self._colour_for(a["agent"]))
        return out

    def select(self, index: int) -> None:
        rows = self._rows
        if not rows:
            return
        index = max(0, min(len(rows) - 1, index if index >= 0 else len(rows) + index))
        for i, r in enumerate(rows):
            r.set_class(i == index, "-selected")
        self.index = index

    def action_cursor(self, delta: int) -> None:
        self.select(self.index + delta)

    def action_ends(self, end: int) -> None:
        self.select(end)

    def action_activate(self) -> None:
        if 0 <= self.index < len(self.channels):
            self.post_message(self.Selected(self.channels[self.index]["name"]))
