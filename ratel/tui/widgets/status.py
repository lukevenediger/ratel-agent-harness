"""TopBar: channel · tasks N/M · live | storage unavailable — retrying in Ns.
StatusBar: cursor id · active filter · last action. Fixed text only."""
from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from ..render import MUTED, scrub

LIVE = "live"
UNAVAILABLE = "storage unavailable — retrying in {n}s"


class TopBar(Static):
    def show(self, channel: str, tasks: tuple[int, int] | None, retry_in: float | None) -> None:
        out = Text(scrub(channel), "bold")
        if tasks is not None:
            out.append(f" · tasks {tasks[0]}/{tasks[1]}")
        out.append(" · ")
        if retry_in is None:
            out.append(LIVE, "#6FDD8B")
        else:
            out.append(UNAVAILABLE.format(n=int(retry_in)), "bold #FF7A76")
        self.update(out)


class StatusBar(Static):
    def show(self, cursor: str | None, filter_label: str, action: str) -> None:
        out = Text(cursor or "—", MUTED)
        out.append(" · " + (filter_label or "no filter"), MUTED if not filter_label else "#FFD43B")
        if action:
            out.append(" · " + scrub(action))
        self.update(out)
