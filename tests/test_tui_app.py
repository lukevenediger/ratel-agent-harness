"""RatelTui driven headlessly through Pilot: layout, keys, live arrival, filter,
stale generations, modals. Every run uses a temporary seeded home."""
from conftest import run_app, screen_text

from ratel.bus import Bus
from ratel.history import matches
from ratel.tui import events, poll
from ratel.tui.app import RatelTui
from ratel.tui.screens.filter import FilterScreen
from ratel.tui.screens.help import HelpScreen
from ratel.tui.screens.message import MessageScreen
from ratel.tui.screens.preview import PreviewScreen
from ratel.tui.widgets.thread import ThreadScreen

WIDE = (140, 40)
NARROW = (80, 24)


def app_for(home, channel="harbor-demo", **kw):
    return RatelTui(home, channel, persist_colours=False, **kw)


def rows(app, panel="#timeline"):
    return list(app.query(f"{panel} .msg"))


def selected_id(app):
    return app.model.selected()["id"]


def demo_messages(home):
    return Bus(home, "harbor-demo", read_only=True).read_all()


# -- compose ----------------------------------------------------------------

def test_wide_layout_composes_every_panel_and_all_demo_rows(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        assert len(rows(app)) == 6
        assert app.query_one("#sidebar").display and app.query_one("#thread").display
        assert app.query_one("#pins").display
        text = screen_text(app)
        assert "harbor-demo" in text and "tasks 1/2" in text and "live" in text
        assert "orchestrator" in text and "developer" in text and "reviewer" in text
        assert "Welcome to the demo" in text
        assert "3 replies" in text
    run_app(app_for(demo_home), script, WIDE)


def test_narrow_layout_hides_sidebar_and_thread_and_s_overlays_the_sidebar(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        assert len(rows(app)) == 6
        assert not app.query_one("#sidebar").display
        assert not app.query_one("#thread").display
        assert "includes a diagram" in screen_text(app)   # the selected (last) row is scrolled into view
        await pilot.press("s")
        assert app.query_one("#sidebar").display
        await pilot.press("escape")
        assert not app.query_one("#sidebar").display
    run_app(app_for(demo_home), script, NARROW)


def test_sidebar_shows_channels_newest_first_with_counts_and_presence_dots(demo_home):
    Bus(demo_home, "other").post("zed", "later channel")
    for agent in ("orchestrator", "developer", "reviewer"):   # presence comes from agent cursors
        Bus(demo_home, "harbor-demo").touch_cursor(agent)

    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        names = [r.name for r in app.query("#sidebar .channel")]
        assert names == ["other", "harbor-demo"]
        text = screen_text(app)
        assert "other" in text and "harbor-demo" in text and "6" in text
        agents = app.query_one("#sidebar").agents
        assert {a["agent"] for a in agents} == {"orchestrator", "developer", "reviewer"}
        assert all(a["state"] == "online" for a in agents)
        assert "● orchestrator" in text
    run_app(app_for(demo_home), script, WIDE)


# -- navigation -------------------------------------------------------------

def test_j_k_g_G_move_the_timeline_cursor_and_the_status_bar_shows_it(demo_home):
    ids = [m["id"] for m in demo_messages(demo_home)]

    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        assert selected_id(app) == ids[-1]
        await pilot.press("k")
        assert selected_id(app) == ids[-2]
        assert ids[-2] in screen_text(app)
        await pilot.press("j", "j")
        assert selected_id(app) == ids[-1]
        await pilot.press("g")
        assert selected_id(app) == ids[0]
        await pilot.press("G")
        assert selected_id(app) == ids[-1]
        await pilot.press("up")
        assert selected_id(app) == ids[-2]
        await pilot.press("down")
        assert selected_id(app) == ids[-1]
        assert len([r for r in rows(app) if r.has_class("-selected")]) == 1
    run_app(app_for(demo_home), script, WIDE)


def test_enter_opens_the_thread_in_the_side_panel_when_wide(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        await pilot.press("g", "enter")
        await pilot.pause(0.5)
        assert len(rows(app, "#thread")) == 4
        assert app.thread_generation >= 1
        text = screen_text(app)
        assert "explicit response-status test" in text
        await pilot.press("escape")
        assert not app.query_one("#thread").has_class("-open")
    run_app(app_for(demo_home), script, WIDE)


def test_enter_pushes_a_thread_screen_when_narrow(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        await pilot.press("g", "enter")
        await pilot.pause(0.5)
        assert isinstance(app.screen, ThreadScreen)
        assert len(app.screen.query(".msg")) == 4
        await pilot.press("escape")
        assert not isinstance(app.screen, ThreadScreen)
    run_app(app_for(demo_home), script, NARROW)


def test_tab_cycles_focus_between_panels(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        seen = [app.focused.id]
        for _ in range(3):
            await pilot.press("tab")
            seen.append(app.focused.id)
        assert set(seen) >= {"timeline", "sidebar"}
    run_app(app_for(demo_home), script, WIDE)


def test_n_p_and_digits_switch_channels_and_bump_the_generation(demo_home):
    Bus(demo_home, "other").post("zed", "later channel")

    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        g0 = app.generation
        assert app.channel == "harbor-demo"
        await pilot.press("p")
        await pilot.pause()
        assert app.channel == "other" and app.generation == g0 + 1
        assert len(rows(app)) == 1
        await pilot.press("n")
        await pilot.pause()
        assert app.channel == "harbor-demo" and app.generation == g0 + 2
        await pilot.press("1")
        await pilot.pause()
        assert app.channel == "other"
        await pilot.press("2")
        await pilot.pause()
        assert app.channel == "harbor-demo"
        await pilot.press("9")
        await pilot.pause()
        assert app.channel == "harbor-demo"
    run_app(app_for(demo_home), script, WIDE)


# -- live -------------------------------------------------------------------

def test_a_message_from_another_process_arrives_with_a_new_divider_and_N_clears_it(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        await pilot.press("k")
        posted = Bus(demo_home, "harbor-demo").post("tester", "posted from elsewhere")
        await pilot.pause(1.5)
        assert len(rows(app)) == 7
        assert len(app.query("#timeline .new")) == 1
        assert "NEW" in screen_text(app)
        assert selected_id(app) != posted["id"]
        await pilot.press("N")
        assert selected_id(app) == posted["id"]
        assert not app.query("#timeline .new")
    run_app(app_for(demo_home), script, WIDE)


def test_live_arrival_follows_the_cursor_when_already_at_the_bottom(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        posted = Bus(demo_home, "harbor-demo").post("tester", "posted from elsewhere")
        await pilot.pause(1.5)
        assert selected_id(app) == posted["id"]
        assert not app.query("#timeline .new")
    run_app(app_for(demo_home), script, WIDE)


def test_a_batch_from_a_stale_generation_is_ignored(demo_home):
    msg = {"id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ", "ts": "2026-09-16T10:00:00.000Z", "from": "ghost", "text": "stale",
           "parent": None, "mentions": [], "attachments": [], "pin": False}

    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        app.post_message(events.MessagesArrived(poll.Batch(app.generation - 1, [msg], msg["id"])))
        await pilot.pause()
        assert len(rows(app)) == 6 and app.stale_dropped == 1
        app.post_message(events.MessagesArrived(poll.Batch(app.generation, [msg], msg["id"])))
        await pilot.pause()
        assert len(rows(app)) == 7 and app.stale_dropped == 1
    run_app(app_for(demo_home), script, WIDE)


def test_a_live_pin_refreshes_the_pins_strip_and_task_count(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        assert "tasks 1/2" in screen_text(app)
        Bus(demo_home, "harbor-demo").post("orchestrator", "New plan", pin=True, attachments=[
            {"type": "tasks", "items": [{"text": "a", "done": True}, {"text": "b", "done": True}, {"text": "c", "done": False}]}])
        await pilot.pause(1.5)
        text = screen_text(app)
        assert "tasks 2/3" in text and "New plan" in text
    run_app(app_for(demo_home), script, WIDE)


def test_storage_events_drive_the_top_bar_with_fixed_text(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        app.post_message(events.StorageChanged(poll.Storage(app.generation, False, 4)))
        await pilot.pause()
        assert "storage unavailable — retrying in 4s" in screen_text(app)
        app.post_message(events.StorageChanged(poll.Storage(app.generation, True)))
        await pilot.pause()
        assert "storage unavailable" not in screen_text(app) and "live" in screen_text(app)
    run_app(app_for(demo_home), script, WIDE)


def test_a_live_reply_lands_in_the_open_thread(demo_home):
    top = demo_messages(demo_home)[0]

    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        await pilot.press("g", "enter")
        await pilot.pause(0.5)
        assert len(rows(app, "#thread")) == 4
        Bus(demo_home, "harbor-demo").post("tester", "late reply", parent=top["id"])
        await pilot.pause(1.5)
        assert len(rows(app, "#thread")) == 5
    run_app(app_for(demo_home), script, WIDE)


# -- filter -----------------------------------------------------------------

def test_slash_filter_modal_applies_a_query(demo_home):
    expected = sum(1 for m in demo_messages(demo_home) if matches(m, "health"))

    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        await pilot.press("slash")
        assert isinstance(app.screen, FilterScreen)
        await pilot.press(*"health", "enter")
        await pilot.pause()
        assert not isinstance(app.screen, FilterScreen)
        assert len(rows(app)) == expected
        assert "q:health" in screen_text(app)
        await pilot.press("slash", "escape")
        await pilot.pause()
        assert len(rows(app)) == expected
    run_app(app_for(demo_home), script, WIDE)


def test_m_cycles_the_mention_filter_and_O_toggles_operator_only(demo_home):
    msgs = demo_messages(demo_home)

    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        await pilot.press("m")
        first = app.agents_seen[0]
        assert app.model.mention == first
        assert len(rows(app)) == sum(1 for m in msgs if matches(m, mention=first))
        assert f"@{first}" in screen_text(app)
        for _ in app.agents_seen:
            await pilot.press("m")
        assert app.model.mention == ""
        await pilot.press("O")
        assert app.model.operator is True
        assert len(rows(app)) == sum(1 for m in msgs if matches(m, operator=True))
        assert "operator" in screen_text(app)
        await pilot.press("O")
        assert len(rows(app)) == 6
    run_app(app_for(demo_home), script, WIDE)


# -- pages ------------------------------------------------------------------

def test_bracket_loads_the_older_page_in_front(demo_home):
    bus = Bus(demo_home, "big")
    for i in range(105):
        bus.post("a", f"msg {i}")

    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        assert len(rows(app)) == 100
        await pilot.press("[")
        await pilot.pause()
        assert len(rows(app)) == 105
        await pilot.press("[")
        await pilot.pause()
        assert len(rows(app)) == 105 and "no older" in screen_text(app)
    run_app(app_for(demo_home, "big"), script, WIDE)


def test_r_reloads_the_page(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert len(rows(app)) == 6 and "reloaded" in screen_text(app)
    run_app(app_for(demo_home), script, WIDE)


# -- pins, modals -----------------------------------------------------------

def test_P_expands_the_newest_pin_with_its_tasks(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        assert "📌 1 orchestrator: Welcome to the demo" in screen_text(app)
        assert "pinned (1)" not in screen_text(app)
        await pilot.press("P")
        text = screen_text(app)
        assert "pinned (1)" in text and app.query_one("#pins").has_class("-expanded")
        assert text.count("Implement health endpoint") == 2   # the timeline row and the expanded pin
        await pilot.press("P")
        assert "pinned (1)" not in screen_text(app)
    run_app(app_for(demo_home), script, WIDE)


def test_o_opens_the_message_modal_with_block_markdown(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        await pilot.press("g", "o")
        await pilot.pause()
        assert isinstance(app.screen, MessageScreen)
        md = app.screen.query_one("Markdown")
        assert md._open_links is False
        assert "Welcome to the demo" in screen_text(app)
        await pilot.press("escape")
        assert not isinstance(app.screen, MessageScreen)
    run_app(app_for(demo_home), script, WIDE)


def test_a_previews_a_markdown_attachment(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        await pilot.press("G", "k", "a")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PreviewScreen)
        assert app.screen.query_one("Markdown")._open_links is False
        text = screen_text(app)
        assert "Review notes" in text and "200 OK" in text
        await pilot.press("escape")
        assert not isinstance(app.screen, PreviewScreen)
    run_app(app_for(demo_home), script, WIDE)


ESCAPES = "\x1b]52;c;Y3VybA==\x1b\\ \x1b[2J \x1b[6n \x1b]0;title\x07"


def test_preview_scrubs_escapes_for_plain_and_markdown(demo_home):
    bus = Bus(demo_home, "harbor-demo")
    for name in ("evil.txt", "evil.md"):
        src = demo_home.parent / name
        src.write_text(f"safe start {ESCAPES} safe end\n")
        bus.post("attacker", f"see {name}", attachments=[bus.attach_file(src)])

    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        for keys, is_markdown in ((("G", "k", "a"), False), (("G", "a"), True)):
            await pilot.press(*keys)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, PreviewScreen)
            assert (app.screen.markdown is not None) is is_markdown
            text = screen_text(app)
            assert "\x1b" not in text and "\x07" not in text, keys
            assert "safe start" in text and "safe end" in text
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, PreviewScreen)
    run_app(app_for(demo_home), script, WIDE)


def test_preview_error_shows_fixed_text_never_the_exception(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()

        def boom(*args, **kwargs):
            raise OSError("/secret/absolute/path.md")
        app.reader.file_text = boom
        await pilot.press("G", "k", "a")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PreviewScreen)
        text = screen_text(app)
        assert "preview unavailable" in text and "/secret" not in text and "path.md" not in text
    run_app(app_for(demo_home), script, WIDE)


def test_a_shows_only_metadata_for_images_and_pdfs(demo_home):
    bus = Bus(demo_home, "harbor-demo")
    (bus.files_dir / "shot.png").write_bytes(b"PNGSECRETBYTES")
    bus.post("tester", "binary", attachments=[
        {"type": "file", "ref": "files/shot.png", "name": "shot.png", "mime": "image/png"},
        {"type": "file", "ref": "files/paper.pdf", "name": "paper.pdf", "mime": "application/pdf", "pages": 3}])

    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        await pilot.press("G", "a")
        await pilot.pause()
        assert "shot.png" in screen_text(app) and "paper.pdf" in screen_text(app)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PreviewScreen)
        text = screen_text(app)
        assert "shot.png" in text and "image/png" in text and "files/shot.png" in text
        assert "SECRETBYTES" not in text
        await pilot.press("escape")
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        text = screen_text(app)
        assert "paper.pdf" in text and "application/pdf" in text and "3 pages" in text
    run_app(app_for(demo_home), script, WIDE)


def test_question_mark_opens_help_and_q_quits(demo_home):
    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
        text = screen_text(app)
        assert "j/k" in text and "quit" in text
        await pilot.press("escape")
        assert not isinstance(app.screen, HelpScreen)
        await pilot.press("q")
        await pilot.pause()
        assert app._exit
    run_app(app_for(demo_home), script, WIDE)


def test_hostile_text_never_reaches_a_markup_parser(demo_home):
    Bus(demo_home, "harbor-demo").post("evil", "[bold red]not markup[/] \x1b[31mansi\x1b[0m \x07bell")

    async def script(pilot):
        app = pilot.app
        await pilot.pause()
        text = screen_text(app)
        assert "[bold red]not markup[/]" in text and "ansi" in text
        assert "\x1b" not in text and "\x07" not in text
    run_app(app_for(demo_home), script, WIDE)
