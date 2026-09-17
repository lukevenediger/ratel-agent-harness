"""The filter modal (`/`): query ≤200 chars, mention, operator-only. Enter
applies (dismisses with the values), Esc leaves the current filter alone."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Static

QUERY_MAX = 200


class FilterScreen(ModalScreen[dict | None]):
    BINDINGS = [
        # priority: the Checkbox binds enter to toggle and would otherwise swallow it
        Binding("enter", "apply", "apply", show=False, priority=True),
        Binding("escape", "cancel", "close", show=False),
    ]

    def __init__(self, query: str = "", mention: str = "", operator: bool = False) -> None:
        super().__init__()
        self.initial = {"query": query, "mention": mention, "operator": operator}

    def compose(self) -> ComposeResult:
        yield Static(Text("filter · enter applies · esc cancels", "bold"), classes="modal-title")
        yield Input(self.initial["query"], placeholder="text query (≤200 chars)", max_length=QUERY_MAX, id="query")
        yield Input(self.initial["mention"], placeholder="mentions @agent", id="mention")
        yield Checkbox("operator only (stakeholder posts and mentions)", self.initial["operator"], id="operator")

    def on_mount(self) -> None:
        self.query_one("#query", Input).focus()

    def action_apply(self) -> None:
        self.dismiss({"query": self.query_one("#query", Input).value[:QUERY_MAX],
                      "mention": self.query_one("#mention", Input).value.strip().lstrip("@"),
                      "operator": self.query_one("#operator", Checkbox).value})

    def action_cancel(self) -> None:
        self.dismiss(None)
