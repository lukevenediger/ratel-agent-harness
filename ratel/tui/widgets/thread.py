"""The thread panel (wide layout) and the pushed thread screen (narrow layout)."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static

from .timeline import Row, Rows


class ThreadPanel(Rows):
    """Parent plus replies; the panel keeps its own cursor (the app applies Moved locally)."""
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.thread_id: str | None = None

    def clear(self) -> None:
        self.thread_id = None
        self.set_rows([Row("day", Text("no thread open", "dim"))])


class ThreadScreen(Screen):
    """Narrow layout: the thread as its own screen; Esc returns to the timeline."""
    BINDINGS = [Binding("escape", "close", "close", show=False)]

    def __init__(self, thread_id: str) -> None:
        super().__init__()
        self.thread_id = thread_id

    def compose(self) -> ComposeResult:
        yield Static(Text("thread · esc closes", "dim"), id="thread-title")
        panel = ThreadPanel(id="thread-screen-panel")
        panel.thread_id = self.thread_id
        yield panel

    def on_mount(self) -> None:
        self.query_one(ThreadPanel).focus()

    def action_close(self) -> None:
        self.app.pop_screen()
