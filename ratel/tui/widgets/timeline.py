"""Row widgets shared by the timeline and the thread panel. Every row is a
`Static` over a `rich.text.Text` built by ratel.tui.render — never a markup string."""
from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from .. import render
from ..model import Row as ModelRow

FLASH_S = 1.2


def message_text(msg: dict, colour_for, replies: int = 0, wrapped: bool = False, tz=None) -> Text:
    """Header, body and one line per attachment for a timeline or thread row."""
    head = render.header(msg, colour_for, replies, tz)
    if wrapped:
        head = Text("┆") + head[1:]
    if isinstance(msg.get("parent"), str):
        head.append("  ↩ reply", render.MUTED)
    out = head + Text("\n") + render.body(msg.get("text"), colour_for)
    atts = msg.get("attachments")
    for att in atts if isinstance(atts, list) else []:
        out.append("\n")
        out.append_text(render.attachment(att, colour_for))
    return out


class Row(Static):
    """One timeline row: kind `msg`, `day` or `new` (as a CSS class)."""
    def __init__(self, kind: str, text: Text, msg: dict | None = None) -> None:
        super().__init__(text, classes=f"row {kind}")
        self.kind = kind
        self.msg = msg

    @property
    def msg_id(self) -> str | None:
        return self.msg["id"] if self.msg else None


def divider(row: ModelRow) -> Row:
    style = "bold #4CC2FF" if row.kind == "new" else render.MUTED
    return Row(row.kind, Text(f"── {row.label} ", style))


class Rows(VerticalScroll):
    """A scrollable list of rows with a keyboard cursor over the message rows."""
    can_focus = True
    BINDINGS = [
        Binding("j,down", "cursor(1)", "down", show=False),
        Binding("k,up", "cursor(-1)", "up", show=False),
        Binding("g", "ends(0)", "first", show=False),
        Binding("G", "ends(-1)", "last", show=False),
        Binding("enter", "activate", "open", show=False),
    ]

    class Activated(Message):
        def __init__(self, rows: Rows, msg: dict) -> None:
            super().__init__()
            self.rows, self.msg = rows, msg

    class Moved(Message):
        def __init__(self, rows: Rows, delta: int | None, end: int | None) -> None:
            super().__init__()
            self.rows, self.delta, self.end = rows, delta, end

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.index = -1
        self._rows: list[Row] = []

    def message_rows(self) -> list[Row]:
        return [r for r in self._rows if r.kind == "msg"]

    def set_rows(self, rows: list[Row], selected: int = -1, flash: set[str] = frozenset()) -> None:
        self._rows = rows
        self.remove_children()
        for r in rows:
            if r.msg_id in flash:
                r.add_class("-flash")
                self.set_timer(FLASH_S, lambda r=r: r.remove_class("-flash"))
        self.mount(*rows)
        self.index = -1
        self.select(selected)

    def select(self, index: int) -> None:
        msgs = self.message_rows()
        if not msgs:
            self.index = -1
            return
        index = max(0, min(len(msgs) - 1, index if index >= 0 else len(msgs) + index))
        for i, r in enumerate(msgs):
            r.set_class(i == index, "-selected")
        self.index = index
        self.call_after_refresh(self.scroll_to_widget, msgs[index], animate=False)

    def selected_msg(self) -> dict | None:
        msgs = self.message_rows()
        return msgs[self.index].msg if 0 <= self.index < len(msgs) else None

    def action_cursor(self, delta: int) -> None:
        self.post_message(self.Moved(self, delta, None))

    def action_ends(self, end: int) -> None:
        self.post_message(self.Moved(self, None, end))

    def action_activate(self) -> None:
        msg = self.selected_msg()
        if msg is not None:
            self.post_message(self.Activated(self, msg))


class Timeline(Rows):
    """The channel timeline; its cursor is owned by ChannelModel, so movement is
    reported to the app rather than applied here."""
