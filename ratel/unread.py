"""UserPromptSubmit hook: print this agent's unread channel messages, advance cursor."""
from __future__ import annotations

import os
import sys

from .bus import Bus, default_home


def _att_summary(a: dict) -> str:
    t = a.get("type")
    if t == "code":
        return f"[code {a.get('file', 'snippet')}]"
    if t == "file":
        return f"[file {a.get('name', a.get('ref'))}]"
    if t == "link":
        return f"[link {a.get('url')}]"
    if t == "tasks":
        items = a.get("items", [])
        return f"[tasks {sum(1 for i in items if i.get('done'))}/{len(items)}]"
    return f"[{t}]"


def render(channel: str, agent: str, msgs: list[dict]) -> str:
    lines = [f"[ratel #{channel}] {len(msgs)} unread for {agent}"]
    for m in msgs:
        text = " ".join(m["text"].split())
        if len(text) > 400:
            text = text[:399] + "…"
        who = m["from"] + (f" (reply to {m['parent'][:5]}…)" if m.get("parent") else "")
        atts = "".join(" " + _att_summary(a) for a in m.get("attachments", []))
        lines.append(f"- {m['id'][:5]}… {who}: {text}{atts}".rstrip())
    return "\n".join(lines)


def main() -> int:
    agent = os.environ.get("AGENT_NAME")
    channel = os.environ.get("CHANNEL")
    if not agent or not channel:
        return 0
    bus = Bus(default_home(), channel)
    new = bus.read_since(bus.get_cursor(agent))
    if not new:
        bus.touch_cursor(agent)
        return 0
    bus.set_cursor(agent, new[-1]["id"])
    others = [m for m in new if m["from"] != agent]
    if others:
        sys.stdout.write(render(channel, agent, others) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
