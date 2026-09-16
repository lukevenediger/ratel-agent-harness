"""ChannelModel: dedupe, reply counts, pins, NEW marker, day separators, client-side filter."""
from datetime import date, timezone

from ratel.tui.model import ChannelModel

N = [0]


def msg(sender="dev", text="hi", parent=None, ts="2026-09-16T10:00:00.000Z", **extra):
    N[0] += 1
    return {"id": f"01M{N[0]:023d}", "ts": ts, "from": sender, "text": text, "parent": parent,
            "mentions": [w[1:] for w in text.split() if w.startswith("@")], "attachments": [], "pin": False, **extra}


def page(msgs, next_before=None, heads=None):
    return {"messages": msgs, "next_before": next_before, "tip": msgs[-1]["id"] if msgs else None,
            "heads": heads or {"proposed": None, "approved": None}}


def model():
    return ChannelModel("ch", today=lambda: date(2026, 9, 16), tz=timezone.utc)


def test_load_page_sets_tip_next_before_heads_and_dedupes():
    m = model()
    a, b = msg(), msg()
    m.load_page(page([a, b, b], next_before=a["id"], heads={"proposed": a["id"], "approved": None}))
    assert [x["id"] for x in m.messages] == [a["id"], b["id"]]
    assert m.tip == b["id"] and m.next_before == a["id"] and m.heads["proposed"] == a["id"]


def test_ingest_returns_only_new_messages_and_advances_tip():
    m = model()
    a = msg()
    m.load_page(page([a]))
    b = msg()
    assert [x["id"] for x in m.ingest([a, b])] == [b["id"]]
    assert m.ingest([b]) == [] and m.tip == b["id"]


def test_reply_counts_follow_replies_in_the_timeline():
    m = model()
    top = msg()
    m.load_page(page([top, msg(parent=top["id"])]))
    assert m.reply_count(top["id"]) == 1
    m.ingest([msg(parent=top["id"])])
    assert m.reply_count(top["id"]) == 2
    assert [x["id"] for x in m.visible()][-1] != top["id"]  # replies stay in the timeline


def test_prepend_older_page_keeps_cursor_on_the_same_message():
    m = model()
    b, c = msg(), msg()
    m.load_page(page([b, c], next_before=b["id"]))
    m.select_last()
    a = msg()
    assert m.prepend_page(page([a], next_before=None)) == 1
    assert [x["id"] for x in m.messages] == [a["id"], b["id"], c["id"]]
    assert m.selected()["id"] == c["id"] and m.next_before is None


def test_day_separators_use_today_yesterday_then_month_day():
    m = model()
    m.load_page(page([msg(ts="2026-09-01T09:00:00.000Z"), msg(ts="2026-09-15T09:00:00.000Z"),
                      msg(ts="2026-09-15T10:00:00.000Z"), msg(ts="2026-09-16T01:00:00.000Z")]))
    kinds = [(r.kind, r.label) for r in m.rows() if r.kind == "day"]
    assert kinds == [("day", "Sep 1"), ("day", "Yesterday"), ("day", "Today")]
    assert [r.kind for r in m.rows()] == ["day", "msg", "day", "msg", "msg", "day", "msg"]


def test_new_divider_only_when_cursor_is_not_at_bottom_and_clears_when_reached():
    m = model()
    a, b = msg(), msg()
    m.load_page(page([a, b]))
    m.select_last()
    c = msg()
    m.ingest([c])                      # at the bottom: follows, no divider
    assert m.new_id is None and m.selected()["id"] == c["id"]
    m.select_first()
    d, e = msg(), msg()
    m.ingest([d, e])
    assert m.new_id == d["id"] and m.selected()["id"] == a["id"]
    assert [r.kind for r in m.rows()][-3:] == ["new", "msg", "msg"]
    m.ingest([msg()])
    assert m.new_id == d["id"]         # the divider stays on the first unseen row
    while m.selected()["id"] != d["id"]:
        m.move(1)
    assert m.new_id is None


def test_jump_new_selects_the_divider_row_and_clears_it():
    m = model()
    m.load_page(page([msg(), msg()]))
    m.select_first()
    c = msg()
    m.ingest([c])
    assert m.new_id == c["id"]
    assert m.jump_new() is True and m.selected()["id"] == c["id"] and m.new_id is None
    assert m.jump_new() is False


def test_cursor_moves_are_clamped_and_at_bottom_tracks_the_last_visible_row():
    m = model()
    m.load_page(page([msg(), msg(), msg()]))
    m.select_first()
    assert not m.at_bottom
    m.move(-5)
    assert m.cursor == 0
    m.move(10)
    assert m.cursor == 2 and m.at_bottom
    assert m.select_id("nope") is False and m.select_id(m.messages[1]["id"]) is True and m.cursor == 1


def test_client_side_filter_uses_history_matches_and_hides_non_matching_live_rows():
    m = model()
    m.load_page(page([msg(text="alpha @stakeholder"), msg(text="beta")]))
    m.set_filter(query="ALPHA")
    assert [x["text"] for x in m.visible()] == ["alpha @stakeholder"]
    m.set_filter(mention="stakeholder")
    assert len(m.visible()) == 1
    m.set_filter(operator=True)
    m.ingest([msg(text="gamma"), msg(sender="stakeholder", text="delta")])
    assert [x["text"] for x in m.visible()] == ["alpha @stakeholder", "delta"]
    assert m.filter_label() == "operator"
    m.set_filter()
    assert m.filter_label() == "" and len(m.visible()) == 4


def test_pins_tasks_progress_and_agents_seen():
    m = model()
    plan = msg(sender="orch", text="plan", attachments=[{"type": "tasks", "ref": "plans/p.md", "items": [
        {"text": "a", "done": True}, {"text": "b", "done": False}, {"text": "c"}]}])
    m.load_page(page([plan, msg(sender="dev", text="@orch hi"), msg(sender="rev")]))
    assert m.tasks_progress() is None
    m.set_pins([msg(sender="orch", text="old pin"), plan])
    assert m.tasks_progress() == (1, 3)
    assert m.agents() == ["orch", "dev", "rev"]
    assert m.newest_pin()["id"] == plan["id"]
