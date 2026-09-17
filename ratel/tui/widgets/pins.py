"""The pins strip: one collapsed line, or (`P`) the newest pin in full plus the older list."""
from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from .. import render
from .timeline import message_text


def first_line(text) -> str:
    return render.scrub(text).split("\n", 1)[0]


class PinsStrip(Static):
    def __init__(self, **kw) -> None:
        super().__init__(Text("no pins", render.MUTED), **kw)
        self.expanded = False

    def show(self, pins: list[dict], colour_for) -> None:
        if not pins:
            self.update(Text("no pins", render.MUTED))
            return
        newest = pins[-1]
        out = Text()
        if not self.expanded:
            out.append(f"📌 {len(pins)} ", render.MUTED)
            out.append(render.scrub(newest.get("from")), colour_for(render.scrub(newest.get("from"))))
            out.append(": " + first_line(newest.get("text")))
            out.append("  (P expands)", render.MUTED)
        else:
            out.append(f"📌 pinned ({len(pins)})\n", render.MUTED)
            out.append_text(message_text(newest, colour_for))
            for pin in reversed(pins[:-1]):
                out.append("\n· ", render.MUTED)
                out.append(render.scrub(pin.get("from")), colour_for(render.scrub(pin.get("from"))))
                out.append(": " + first_line(pin.get("text")))
        self.update(out)
