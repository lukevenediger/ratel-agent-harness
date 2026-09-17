"""RatelTui: the Textual app. It drives the pure layer (ChannelModel,
BoardReader, render, SlotMap, poll loop bodies) and owns two generation
counters: `generation` for the open channel and `thread_generation` for the
open thread. Every poll event carries the generation it was started under;
handlers drop anything older."""
from __future__ import annotations

import time
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.worker import get_current_worker

from . import events, poll, render
from .data import BoardReader
from .model import ChannelModel
from .screens.filter import FilterScreen
from .screens.help import HelpScreen
from .screens.message import MessageScreen
from .screens.preview import AttachmentPicker, PreviewScreen
from .slots import SlotMap
from .widgets.pins import PinsStrip
from .widgets.sidebar import Sidebar
from .widgets.status import StatusBar, TopBar
from .widgets.thread import ThreadPanel, ThreadScreen
from .widgets.timeline import Row, Rows, Timeline, divider, message_text

WIDE_COLS = 110
SLEEP_SLICE = 0.1


class RatelTui(App):
    TITLE = "ratel"
    CSS_PATH = "tui.tcss"
    BINDINGS = [
        Binding("q", "quit", "quit", show=False),
        Binding("question_mark", "help", "help", show=False),
        Binding("escape", "close", "close", show=False),
        Binding("n", "channel_step(1)", "next channel", show=False),
        Binding("p", "channel_step(-1)", "previous channel", show=False),
        *[Binding(str(i), f"channel_jump({i})", "channel", show=False) for i in range(1, 10)],
        Binding("s", "toggle_sidebar", "sidebar", show=False),
        Binding("t", "toggle_thread", "thread", show=False),
        Binding("P", "toggle_pins", "pins", show=False),
        Binding("o", "open_message", "message", show=False),
        Binding("a", "attachments", "attachments", show=False),
        Binding("slash", "filter", "filter", show=False),
        Binding("m", "cycle_mention", "mention", show=False),
        Binding("O", "toggle_operator", "operator", show=False),
        Binding("left_square_bracket", "older", "older", show=False),
        Binding("N", "jump_new", "NEW", show=False),
        Binding("r", "reload", "reload", show=False),
    ]

    def __init__(self, home: Path | str, channel: str | None = None, persist_colours: bool = True) -> None:
        super().__init__()
        self.home = Path(home).expanduser().resolve()
        self.reader = BoardReader(self.home)
        self.slots = SlotMap(self.home, persist_colours)
        self.channel: str | None = channel
        self.model = ChannelModel(channel or "")
        self.generation = 0
        self.thread_generation = 0
        self.stale_dropped = 0
        self.retry_in: float | None = None
        self.last_action = ""
        self.narrow = False
        self.channels: list[dict] = []
        self.agents_seen: list[str] = []   # every sender seen on the channel, first-seen order, filter or not

    # -- compose ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Sidebar(id="sidebar")
            with Vertical(id="main"):
                yield TopBar(id="topbar")
                yield PinsStrip(id="pins")
                yield Timeline(id="timeline")
                yield StatusBar(id="status")
            yield ThreadPanel(id="thread")

    def on_mount(self) -> None:
        self._apply_width(self.size.width)
        self.channels = self._channels()
        if self.channel is None and self.channels:
            self.channel = self.channels[0]["name"]
        self.query_one("#thread", ThreadPanel).clear()
        self.query_one(Timeline).focus()
        if self.channel:
            self.open_channel(self.channel)

    def on_resize(self, event) -> None:
        self._apply_width(event.size.width)

    def _apply_width(self, width: int) -> None:
        self.narrow = width < WIDE_COLS
        self.screen.set_class(self.narrow, "-narrow")
        if not self.narrow:
            self.query_one("#sidebar").remove_class("-overlay")

    # -- helpers ------------------------------------------------------------

    def colour_for(self, agent: str) -> str:
        return self.slots.colour(self.channel or "", agent)

    def _channels(self) -> list[dict]:
        try:
            return self.reader.channels()
        except (OSError, ValueError):
            return []

    def _row(self, msg: dict) -> Row:
        sender = render.scrub(msg.get("from"))
        return Row("msg", message_text(msg, self.colour_for, self.model.reply_count(msg["id"]),
                                       self.slots.wrapped(self.channel or "", sender)), msg)

    def _timeline_index(self) -> int:
        return self.model.cursor

    def _render_timeline(self, flash: set[str] = frozenset()) -> None:
        rows = [self._row(r.msg) if r.kind == "msg" else divider(r) for r in self.model.rows()]
        self.query_one(Timeline).set_rows(rows, self._timeline_index(), flash)
        self._render_status()

    def _sync_cursor(self) -> None:
        """After a model cursor move: the NEW divider may have been cleared."""
        if self.model.new_id is None and self.query("#timeline .new"):
            self._render_timeline()
        else:
            self.query_one(Timeline).select(self._timeline_index())
            self._render_status()

    def _render_pins(self) -> None:
        self.query_one("#pins", PinsStrip).show(self.model.pins, self.colour_for)
        self._render_topbar()

    def _render_topbar(self) -> None:
        self.query_one("#topbar", TopBar).show(self.channel or "—", self.model.tasks_progress(), self.retry_in)

    def _render_status(self) -> None:
        sel = self.model.selected()
        self.query_one("#status", StatusBar).show(sel["id"] if sel else None, self.model.filter_label(),
                                                  self.last_action)

    def _render_sidebar(self) -> None:
        self.query_one("#sidebar", Sidebar).set_channels(self.channels, self.channel)

    def _see(self, msgs: list[dict]) -> None:
        for m in msgs:
            sender = m.get("from")
            if isinstance(sender, str) and sender not in self.agents_seen:
                self.agents_seen.append(sender)

    def _act(self, text: str) -> None:
        self.last_action = text
        self._render_status()

    # -- channel ------------------------------------------------------------

    def open_channel(self, name: str) -> None:
        """Switch channels: bump the generation, cancel every worker group, load
        the first page and start polling under the new generation."""
        self.generation += 1
        for group in ("messages", "presence", "thread"):
            self.workers.cancel_group(self, group)
        self.channel = name
        self.model = ChannelModel(name)
        self.agents_seen = []
        self._close_thread()
        self._render_sidebar()
        self._load_page()

    def _load_page(self, query: str = "", mention: str = "", operator: bool = False) -> None:
        generation = self.generation
        model = self.model
        try:
            page = self.reader.initial_page(self.channel, query, mention, operator)
            pins = self.reader.pins(self.channel)
            presence = self.reader.presence(self.channel)
        except Exception:
            self.retry_in = poll.BACKOFF[0]
            self._render_topbar()
            self.set_timer(poll.BACKOFF[0], lambda: generation == self.generation and self._load_page(
                query, mention, operator))
            return
        self.retry_in = None
        model.query, model.mention, model.operator = query, mention, operator
        model.load_page(page)
        self._see(model.messages)
        model.set_pins(pins, page.get("heads"))
        self.query_one("#sidebar", Sidebar).set_agents(presence, self.colour_for)
        self._render_timeline()
        self._render_pins()
        self._poll_messages(self.channel, model.tip, generation)
        self._poll_presence(self.channel, generation)

    # -- workers ------------------------------------------------------------

    def _post(self, payload) -> None:
        self.post_message(events.wrap(payload))

    @staticmethod
    def _sleep(seconds: float) -> None:
        worker = get_current_worker()
        end = time.monotonic() + seconds
        while not worker.is_cancelled:
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, SLEEP_SLICE))

    @work(thread=True, exclusive=True, group="messages")
    def _poll_messages(self, channel: str, cursor: str | None, generation: int) -> None:
        worker = get_current_worker()
        poll.poll_messages(self.reader, channel, cursor, generation, self._post,
                           lambda: worker.is_cancelled, self._sleep)

    @work(thread=True, exclusive=True, group="presence")
    def _poll_presence(self, channel: str, generation: int) -> None:
        worker = get_current_worker()
        poll.poll_presence(self.reader, channel, generation, self._post, lambda: worker.is_cancelled, self._sleep)

    @work(thread=True, exclusive=True, group="thread")
    def _fetch_thread(self, channel: str, msg_id: str, generation: int, thread_generation: int) -> None:
        try:
            thread = self.reader.thread(channel, msg_id)
        except Exception:
            thread = None
        self.post_message(events.ThreadLoaded(generation, thread_generation, thread))

    def _stale(self, event) -> bool:
        if event.generation != self.generation:
            self.stale_dropped += 1
            return True
        return False

    # -- poll events --------------------------------------------------------

    def on_messages_arrived(self, event: events.MessagesArrived) -> None:
        if self._stale(event):
            return
        added = self.model.ingest(event.payload.messages)
        self._see(added)
        if not added:
            return
        self._render_timeline({m["id"] for m in added})
        panel = self._thread_panel()
        if panel is not None and panel.thread_id:
            replies = [m for m in added if m.get("parent") == panel.thread_id]
            if replies:
                self._fetch_thread(self.channel, panel.thread_id, self.generation, self.thread_generation)

    def on_pins_changed(self, event: events.PinsChanged) -> None:
        if self._stale(event):
            return
        self.model.set_pins(event.payload.pins, event.payload.heads)
        self._render_pins()

    def on_presence_changed(self, event: events.PresenceChanged) -> None:
        if self._stale(event):
            return
        self.channels = event.payload.channels
        self._render_sidebar()
        self.query_one("#sidebar", Sidebar).set_agents(event.payload.presence, self.colour_for)

    def on_storage_changed(self, event: events.StorageChanged) -> None:
        if self._stale(event):
            return
        self.retry_in = None if event.payload.ok else event.payload.retry_in
        self._render_topbar()

    def on_thread_loaded(self, event: events.ThreadLoaded) -> None:
        if self._stale(event) or event.thread_generation != self.thread_generation:
            return
        panel = self._thread_panel()
        if panel is None:
            return
        thread = event.thread
        if not thread or not isinstance(thread.get("parent"), dict):
            panel.set_rows([Row("day", Text("thread not found", render.MUTED))])
            return
        msgs = [thread["parent"], *[r for r in thread.get("replies", []) if isinstance(r, dict)]]
        panel.set_rows([self._row(m) for m in msgs], 0)

    # -- rows events --------------------------------------------------------

    def on_rows_moved(self, event: Rows.Moved) -> None:
        event.stop()
        if isinstance(event.rows, Timeline):
            if event.delta is not None:
                self.model.move(event.delta)
            elif event.end == 0:
                self.model.select_first()
            else:
                self.model.select_last()
            self._sync_cursor()
        else:
            event.rows.select((event.rows.index + event.delta) if event.delta is not None else event.end)

    def on_rows_activated(self, event: Rows.Activated) -> None:
        event.stop()
        if isinstance(event.rows, Timeline):
            self.open_thread(event.msg["id"] if not event.msg.get("parent") else event.msg["parent"])

    def on_sidebar_selected(self, event: Sidebar.Selected) -> None:
        event.stop()
        self.query_one("#sidebar").remove_class("-overlay")
        if event.channel != self.channel:
            self.open_channel(event.channel)
        self.query_one(Timeline).focus()

    # -- thread -------------------------------------------------------------

    def _thread_panel(self) -> ThreadPanel | None:
        if isinstance(self.screen, ThreadScreen):
            return self.screen.query_one(ThreadPanel)
        panel = self.query_one("#thread", ThreadPanel)
        return panel if panel.has_class("-open") else None

    def open_thread(self, msg_id: str) -> None:
        self.thread_generation += 1
        if self.narrow:
            if isinstance(self.screen, ThreadScreen):
                self.screen.query_one(ThreadPanel).thread_id = msg_id
            else:
                self.push_screen(ThreadScreen(msg_id))
        else:
            panel = self.query_one("#thread", ThreadPanel)
            panel.add_class("-open")
            panel.thread_id = msg_id
            panel.set_rows([Row("day", Text("loading…", render.MUTED))])
        self._fetch_thread(self.channel, msg_id, self.generation, self.thread_generation)
        self._act(f"thread {msg_id}")

    def _close_thread(self) -> None:
        self.thread_generation += 1
        if isinstance(self.screen, ThreadScreen):
            self.pop_screen()
        panel = self.query_one("#thread", ThreadPanel)
        panel.remove_class("-open")
        panel.clear()

    # -- actions ------------------------------------------------------------

    def _selected_message(self) -> dict | None:
        focused = self.focused
        if isinstance(focused, ThreadPanel):
            return focused.selected_msg()
        return self.model.selected()

    def action_close(self) -> None:
        sidebar = self.query_one("#sidebar")
        if sidebar.has_class("-overlay"):
            sidebar.remove_class("-overlay")
            self.query_one(Timeline).focus()
        elif self._thread_panel() is not None:
            self._close_thread()
            self.query_one(Timeline).focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_channel_step(self, delta: int) -> None:
        names = [c["name"] for c in self.channels]
        if self.channel in names and len(names) > 1:
            self.open_channel(names[(names.index(self.channel) + delta) % len(names)])

    def action_channel_jump(self, n: int) -> None:
        if n <= len(self.channels) and self.channels[n - 1]["name"] != self.channel:
            self.open_channel(self.channels[n - 1]["name"])

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar", Sidebar)
        if self.narrow:
            sidebar.toggle_class("-overlay")
        (sidebar if sidebar.display else self.query_one(Timeline)).focus()

    def action_toggle_thread(self) -> None:
        if self._thread_panel() is not None:
            self._close_thread()
        else:
            sel = self.model.selected()
            if sel is not None:
                self.open_thread(sel["parent"] if sel.get("parent") else sel["id"])

    def action_toggle_pins(self) -> None:
        pins = self.query_one("#pins", PinsStrip)
        pins.expanded = not pins.expanded
        pins.set_class(pins.expanded, "-expanded")
        self._render_pins()

    def action_open_message(self) -> None:
        msg = self._selected_message()
        if msg is not None:
            self.push_screen(MessageScreen(msg, self.colour_for, self.model.reply_count(msg["id"])))

    def action_attachments(self) -> None:
        msg = self._selected_message()
        atts = msg.get("attachments") if msg else None
        if not isinstance(atts, list) or not atts:
            self._act("no attachments")
            return
        self.push_screen(AttachmentPicker(atts, self.colour_for), lambda i: self._preview(atts, i))

    def _preview(self, atts: list, index: int | None) -> None:
        if index is None:
            return
        att = atts[index]
        title = render.attachment(att, self.colour_for).plain.split("\n", 1)[0]
        if render.previewable(att):
            try:
                text, truncated = self.reader.file_text(self.channel, att.get("ref"), att.get("name"), att.get("mime"))
            except (OSError, ValueError) as e:
                self.push_screen(PreviewScreen(title, plain=Text(str(e)), note="preview unavailable"))
                return
            note = "truncated at 512 KiB" if truncated else ""
            if render.scrub(att.get("mime")).lower() == "text/plain":
                self.push_screen(PreviewScreen(title, plain=Text(text), note=note))
            else:
                self.push_screen(PreviewScreen(title, markdown=text, note=note))
        else:
            self.push_screen(PreviewScreen(title, plain=render.attachment(att, self.colour_for),
                                           note="not previewable: metadata only, bytes never read"))

    def action_filter(self) -> None:
        self.push_screen(FilterScreen(self.model.query, self.model.mention, self.model.operator), self._apply_filter)

    def _apply_filter(self, result: dict | None) -> None:
        if result is None:
            return
        self._load_page(result["query"], result["mention"], result["operator"])
        self._act("filter applied")

    def action_cycle_mention(self) -> None:
        agents = self.agents_seen
        current = self.model.mention
        nxt = "" if not agents else agents[0] if current not in agents else (
            agents[agents.index(current) + 1] if agents.index(current) + 1 < len(agents) else "")
        self._load_page(self.model.query, nxt, self.model.operator)
        self._act(f"mention @{nxt}" if nxt else "mention filter off")

    def action_toggle_operator(self) -> None:
        self._load_page(self.model.query, self.model.mention, not self.model.operator)
        self._act("operator only" if self.model.operator else "operator filter off")

    def action_older(self) -> None:
        if not self.model.next_before:
            self._act("no older messages")
            return
        try:
            page = self.reader.older(self.channel, self.model.next_before, self.model.query, self.model.mention,
                                     self.model.operator)
        except Exception:
            self._act("older page unavailable")
            return
        n = self.model.prepend_page(page)
        self._render_timeline()
        self._act(f"loaded {n} older")

    def action_jump_new(self) -> None:
        if self.model.jump_new():
            self._render_timeline()
            self._act("jumped to NEW")
        else:
            self._act("nothing new")

    def action_reload(self) -> None:
        self.generation += 1
        for group in ("messages", "presence"):
            self.workers.cancel_group(self, group)
        self.channels = self._channels()
        self._render_sidebar()
        self._load_page(self.model.query, self.model.mention, self.model.operator)
        self._act("reloaded")
