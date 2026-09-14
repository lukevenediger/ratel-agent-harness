import asyncio
import threading
import time

import pytest

from ratel.bus import Bus
from ratel.mcp_server import build_server


def call(server, name, **args):
    # mcp 2.1: scalar/list returns are wrapped as {"result": ...}; dict[str, Any] returns come back as the dict itself.
    # Tools that raise propagate ToolError from call_tool (is_error is never set on this path).
    res = asyncio.run(server.call_tool(name, args))
    sc = res.structured_content
    return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc


@pytest.fixture
def pair(home):
    bus = Bus(home, "test")
    return bus, build_server(bus, "orchestrator"), build_server(bus, "worker-a")


def test_tool_names(pair):
    _, o, _ = pair
    names = {t.name for t in asyncio.run(o.list_tools())}
    assert names == {"post", "read_channel", "read_thread", "wait_for_mention", "pin", "unpin", "attach_file", "catch_up"}


def test_post_and_read_excludes_self_and_advances(pair):
    bus, o, w = pair
    mid = call(o, "post", text="@worker-a take auth")
    assert isinstance(mid, str) and len(mid) == 26
    assert call(o, "read_channel") == []                    # own post not echoed
    got = call(w, "read_channel")
    assert [m["id"] for m in got] == [mid] and got[0]["mentions"] == ["worker-a"]
    assert call(w, "read_channel") == []                    # cursor advanced
    assert bus.get_cursor("worker-a") == mid


def test_read_channel_since_and_limit(pair):
    bus, o, w = pair
    ids = [call(o, "post", text=f"m{i}") for i in range(4)]
    assert [m["id"] for m in call(w, "read_channel", limit=2)] == ids[:2]
    assert [m["id"] for m in call(w, "read_channel", since=ids[0])] == ids[1:]
    assert bus.get_cursor("worker-a") == ids[-1]


def test_thread_pin_unpin_catch_up(pair):
    bus, o, w = pair
    top = call(o, "post", text="plan v1", pin=True)
    call(w, "post", text="ack", parent=top)
    t = call(o, "read_thread", id=top)
    assert t["parent"]["id"] == top and len(t["replies"]) == 1
    other = call(o, "post", text="second")
    call(o, "pin", id=other)
    cu = call(w, "catch_up")
    assert [p["id"] for p in cu["pins"]] == [top, other]
    # worker-a never read anything and its own "ack" did not move the cursor (it was not at the tip),
    # so catch_up returns everything from others: plan v1, second, and the empty-text pin message.
    assert [m["text"] for m in cu["messages"]] == ["plan v1", "second", ""]
    call(o, "unpin", id=top)
    assert [p["id"] for p in bus.pins()] == [other]


def test_wait_for_mention_blocks_then_returns(pair):
    bus, o, w = pair

    def later():
        time.sleep(0.3)
        call(o, "post", text="@worker-a go")

    threading.Thread(target=later).start()
    got = call(w, "wait_for_mention", timeout_s=5)
    assert [m["text"] for m in got] == ["@worker-a go"]
    assert call(w, "wait_for_mention", timeout_s=0.2) == []


def test_read_thread_unknown_raises(pair):
    from mcp.server.mcpserver.exceptions import ToolError
    _, o, _ = pair
    with pytest.raises(ToolError):
        asyncio.run(o.call_tool("read_thread", {"id": "nope"}))


def test_attach_file(pair, tmp_path):
    _, o, _ = pair
    f = tmp_path / "a.txt"
    f.write_text("hi")
    att = call(o, "attach_file", path=str(f))
    assert att["type"] == "file" and att["name"] == "a.txt"
