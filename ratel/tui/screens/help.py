"""The key map (`?`)."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

KEYS = (
    ("j/k, arrows", "move"), ("g / G", "first / last"), ("Enter", "open thread (timeline) · select channel (sidebar)"),
    ("Esc", "close"), ("Tab", "cycle focus"), ("n / p", "next / previous channel"), ("1–9", "channel jump"),
    ("s", "sidebar"), ("t", "thread panel"), ("P", "pins"), ("o", "message modal (full Markdown)"),
    ("a", "attachment picker → preview"), ("/", "filter modal"), ("m", "cycle mention filter"),
    ("O", "toggle operator-only"), ("[", "load older page"), ("N", "jump to NEW"), ("r", "reload"),
    ("?", "help"), ("q", "quit"),
)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,question_mark", "close", "close", show=False)]

    def compose(self) -> ComposeResult:
        yield Static(Text("ratel-tui keys", "bold"), classes="modal-title")
        body = Text()
        for key, what in KEYS:
            body.append(f"{key:14}", "bold")
            body.append(what + "\n")
        with VerticalScroll(classes="modal-body"):
            yield Static(body)
        yield Static(Text("read-only: this console never posts or moves a cursor · esc closes", "dim"),
                     classes="modal-note")

    def action_close(self) -> None:
        self.dismiss(None)
