"""The message modal (`o`): header, full text as block Markdown (512 KiB cap,
links inert), then every attachment in full."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static

from .. import render

TEXT_CAP = 512 * 1024


class MessageScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "close", show=False)]

    def __init__(self, msg: dict, colour_for, replies: int = 0) -> None:
        super().__init__()
        self.msg, self.colour_for, self.replies = msg, colour_for, replies

    def compose(self) -> ComposeResult:
        yield Static(render.header(self.msg, self.colour_for, self.replies), classes="modal-title")
        with VerticalScroll(classes="modal-body"):
            yield Markdown(render.scrub(self.msg.get("text"))[:TEXT_CAP], open_links=False)
            atts = self.msg.get("attachments")
            for att in atts if isinstance(atts, list) else []:
                yield Static(render.attachment(att, self.colour_for), classes="attachment")
                if isinstance(att, dict) and att.get("type") == "code" and isinstance(att.get("body"), str):
                    yield Static(Text(render.scrub(att["body"])[:TEXT_CAP], render.CODE), classes="code-full")
        yield Static(Text(f"{render.scrub(self.msg.get('id'))} · esc closes", render.MUTED), classes="modal-note")

    def action_close(self) -> None:
        self.dismiss(None)
