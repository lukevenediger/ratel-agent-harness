"""AgentOps: the cursor/self-exclusion semantics shared by the MCP server and the CLI."""
import threading
import time

import pytest

from ratel.bus import Bus
from ratel.ops import AgentOps


@pytest.fixture
def pair(home):
    bus = Bus(home, "test")
    return bus, AgentOps(bus, "orchestrator"), AgentOps(bus, "worker-a")


def test_post_returns_id_and_read_excludes_self(pair):
    bus, o, w = pair
    mid = o.post("@worker-a take auth")
    assert isinstance(mid, str) and len(mid) == 26
    assert o.read_channel() == []
    assert [m["id"] for m in w.read_channel()] == [mid]


def test_read_channel_advances_cursor(pair):
    bus, o, w = pair
    mid = o.post("hello")
    w.read_channel()
    assert bus.get_cursor("worker-a") == mid
    assert w.read_channel() == []


def test_post_advances_own_cursor_only_when_at_tip(pair):
    bus, o, w = pair
    first = o.post("one")          # o was at tip (cursor None, bus empty) -> cursor moves
    assert bus.get_cursor("orchestrator") == first
    w.post("unread by o")          # o is no longer at tip
    second = o.post("two")
    assert bus.get_cursor("orchestrator") == first
    assert [m["text"] for m in o.read_channel()] == ["unread by o"]
    assert bus.get_cursor("orchestrator") == second


def test_read_channel_since_and_limit(pair):
    bus, o, w = pair
    ids = [o.post(f"m{i}") for i in range(4)]
    assert [m["id"] for m in w.read_channel(limit=2)] == ids[:2]
    assert [m["id"] for m in w.read_channel(since=ids[0])] == ids[1:]


def test_cursor_advances_forward_only(pair):
    bus, o, w = pair
    ids = [o.post(f"m{i}") for i in range(3)]
    w.read_channel()
    w.read_channel(since=ids[0])   # re-reading older messages must not rewind
    assert bus.get_cursor("worker-a") == ids[-1]


def test_read_thread_and_unknown_id(pair):
    _, o, w = pair
    top = o.post("plan v1")
    w.post("ack", parent=top)
    t = o.read_thread(top)
    assert t["parent"]["id"] == top and [r["text"] for r in t["replies"]] == ["ack"]
    with pytest.raises(ValueError):
        o.read_thread("nope")


def test_pin_unpin_and_catch_up(pair):
    bus, o, w = pair
    top = o.post("plan v1", pin=True)
    other = o.post("second")
    o.pin(other)
    cu = w.catch_up()
    assert [p["id"] for p in cu["pins"]] == [top, other]
    assert [m["text"] for m in cu["messages"]] == ["plan v1", "second", ""]
    o.unpin(top)
    assert [p["id"] for p in o.pins()] == [other]


def test_attach_file(pair, tmp_path):
    _, o, _ = pair
    f = tmp_path / "a.txt"
    f.write_text("hi")
    att = o.attach_file(str(f))
    assert att["type"] == "file" and att["name"] == "a.txt"


def test_wait_for_mention_blocks_then_times_out(pair):
    _, o, w = pair
    threading.Thread(target=lambda: (time.sleep(0.3), o.post("@worker-a go"))).start()
    assert [m["text"] for m in w.wait_for_mention(5)] == ["@worker-a go"]
    assert w.wait_for_mention(0.2) == []


def test_wait_any_returns_on_any_new_message(pair):
    _, o, w = pair
    o.post("no mention here")
    assert [m["text"] for m in w.wait_for_mention(2, any_message=True)] == ["no mention here"]
