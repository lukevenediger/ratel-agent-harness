"""Attachment picker and preview. Text/Markdown files render as block Markdown
(512 KiB cap, links inert); anything else shows name · mime · pages · ref only —
the bytes are never read."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Markdown, OptionList, Static
from textual.widgets.option_list import Option

from .. import render
from ..data import FILE_TEXT_CAP


class AttachmentPicker(ModalScreen[int | None]):
    BINDINGS = [Binding("escape", "cancel", "close", show=False)]

    def __init__(self, attachments: list, colour_for) -> None:
        super().__init__()
        self.attachments = attachments
        self.colour_for = colour_for

    def compose(self) -> ComposeResult:
        yield Static(Text("attachments · enter previews · esc closes", render.MUTED), classes="modal-title")
        yield OptionList(*(Option(render.attachment(a, self.colour_for).split("\n")[0], id=str(i))
                           for i, a in enumerate(self.attachments)), id="picker")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(int(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class PreviewScreen(ModalScreen[None]):
    """`markdown` renders as a block Markdown widget; otherwise `plain` shows as text."""
    BINDINGS = [Binding("escape", "close", "close", show=False)]

    def __init__(self, title: str, markdown: str | None = None, plain: Text | None = None, note: str = "") -> None:
        super().__init__()
        self.title_text, self.markdown, self.plain, self.note = title, markdown, plain, note

    def compose(self) -> ComposeResult:
        yield Static(Text(render.scrub(self.title_text), "bold"), classes="modal-title")
        with VerticalScroll(classes="modal-body"):
            if self.markdown is not None:
                yield Markdown(self.markdown[:FILE_TEXT_CAP], open_links=False)
            else:
                yield Static(self.plain if self.plain is not None else Text(""))
        yield Static(Text(render.scrub(self.note) or "esc closes", render.MUTED), classes="modal-note")

    def action_close(self) -> None:
        self.dismiss(None)
