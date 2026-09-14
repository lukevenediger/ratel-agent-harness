import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _params(home, agent):
    return StdioServerParameters(
        command=sys.executable, args=["-m", "ratel.mcp_server"],
        env={**os.environ, "RATEL_HOME": str(home), "AGENT_NAME": agent, "CHANNEL": "it"},
    )


async def _run(home):
    async with stdio_client(_params(home, "orchestrator")) as (ro, wo), ClientSession(ro, wo) as o, \
               stdio_client(_params(home, "worker-a")) as (rw, ww), ClientSession(rw, ww) as w:
        await o.initialize()
        await w.initialize()
        r = await o.call_tool("post", {"text": "@worker-a ping"})
        mid = r.structured_content["result"]
        got = await w.call_tool("wait_for_mention", {"timeout_s": 5})
        msgs = got.structured_content["result"]
        assert [m["id"] for m in msgs] == [mid]
        await w.call_tool("post", {"text": "@orchestrator pong", "parent": mid})
        back = await o.call_tool("wait_for_mention", {"timeout_s": 5})
        assert back.structured_content["result"][0]["text"] == "@orchestrator pong"
        cu = await w.call_tool("catch_up", {})
        assert cu.structured_content["messages"] == []  # dict[str, Any] tools are not wrapped in "result"


def test_two_agents_over_stdio(home):
    asyncio.run(_run(home))
    lines = (home / "channels" / "it" / "bus.jsonl").read_text().splitlines()
    assert len(lines) == 2
