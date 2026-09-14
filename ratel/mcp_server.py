"""MCP stdio server: one process per agent. Env: AGENT_NAME, CHANNEL, RATEL_HOME."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from .bus import Bus, default_home
from .ops import AgentOps

INSTRUCTIONS = """You are `{agent}` on ratel channel `{channel}`.
Post coordination messages (dispatch, results, questions, findings, decisions), not narration.
Start each session with catch_up. When you finish or get stuck: post, then wait_for_mention.
Mention agents with @name. Reply in a thread by passing parent=<id>."""


def build_server(bus: Bus, agent: str) -> MCPServer:
    server = MCPServer("ratel", instructions=INSTRUCTIONS.format(agent=agent, channel=bus.channel))
    ops = AgentOps(bus, agent)

    @server.tool()
    def post(text: str, parent: str | None = None, attachments: list[dict] | None = None,
             pin: bool = False) -> str:
        """Post a message. `parent` = id of a top-level message to reply in its thread.
        `pin=True` pins this message (the plan lives in pins). Returns the message id.

        `attachments` is a list of attachment objects. Pass attach_file's return value
        UNCHANGED — keep every key, especially `type`. If `type` is missing it is inferred
        from the shape (`ref` -> file, `url` -> link, `body` -> code, `items` -> tasks)."""
        return ops.post(text, parent=parent, attachments=attachments, pin=pin)

    @server.tool()
    def read_channel(since: str | None = None, limit: int | None = None) -> list[dict]:
        """Messages after `since` (default: your cursor), oldest first, excluding your own. Advances cursor."""
        return ops.read_channel(since, limit)

    @server.tool()
    def read_thread(id: str) -> dict[str, Any]:
        """A top-level message plus all its replies."""
        return ops.read_thread(id)

    @server.tool()
    async def wait_for_mention(timeout_s: float = 60, any: bool = False) -> list[dict]:
        """Block until someone @mentions you (or, with any=True, until any new message), or timeout.
        Returns every new message since your cursor (context included) and advances it. [] on timeout — call again."""
        return await asyncio.to_thread(ops.wait_for_mention, timeout_s, any)

    @server.tool()
    def pin(id: str) -> str:
        """Pin an existing message by id. Latest pin renders expanded on the board."""
        return ops.pin(id)

    @server.tool()
    def unpin(id: str) -> str:
        """Unpin a message by id."""
        return ops.unpin(id)

    @server.tool()
    def attach_file(path: str, name: str | None = None) -> dict[str, Any]:
        """Copy a local file into the channel's files/ dir. Returns an attachment object for `post`."""
        return ops.attach_file(path, name)

    @server.tool()
    def catch_up() -> dict[str, Any]:
        """Everything since your cursor plus the current pins. Call at session start."""
        return ops.catch_up()

    return server


def main() -> None:
    agent = os.environ.get("AGENT_NAME")
    channel = os.environ.get("CHANNEL")
    if not agent or not channel:
        sys.exit("ratel-mcp: set AGENT_NAME and CHANNEL")
    build_server(Bus(default_home(), channel), agent).run("stdio")


if __name__ == "__main__":
    main()
