"""Agent identity colours: the board's eight-slot palette (docs/design/DESIGN.md),
assigned in first-seen order per channel and persisted to `$RATEL_HOME/tui.toml`
so a later agent never re-colours an earlier one. A malformed file starts fresh."""
from __future__ import annotations

import tomllib
from pathlib import Path

from ..paths import atomic_write, confined
from ..schema import validate_name

PALETTE = ("#82A7FF", "#FF7A76", "#3FD9C4", "#FFD43B", "#C0A6FF", "#6FDD8B", "#FFA94D", "#FF7ABF")
FILE = "tui.toml"


def _quote(key: str) -> str:
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dump(table: dict[str, dict[str, int]]) -> str:
    lines = ["# ratel-tui colour slots: first-seen agent order per channel. Safe to delete."]
    for channel in sorted(table):
        lines.append(f"\n[slots.{_quote(channel)}]")
        for agent, slot in sorted(table[channel].items(), key=lambda kv: kv[1]):
            lines.append(f"{_quote(agent)} = {slot}")
    return "\n".join(lines) + "\n"


def load(text: str) -> dict[str, dict[str, int]]:
    """Parse a slot file; a malformed document is an empty map, and any
    entry that is not `valid channel -> valid agent -> positive int` is dropped."""
    try:
        doc = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, TypeError):
        return {}
    slots = doc.get("slots")
    if not isinstance(slots, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for channel, agents in slots.items():
        if not isinstance(agents, dict):
            continue
        try:
            validate_name(channel)
        except ValueError:
            continue
        table: dict[str, int] = {}
        for agent, slot in agents.items():
            if type(slot) is not int or slot < 1:
                continue
            try:
                validate_name(agent, "agent")
            except ValueError:
                continue
            table[agent] = slot
        out[channel] = table
    return out


class SlotMap:
    def __init__(self, home: Path | str | None, persist: bool = True):
        self.home = Path(home).expanduser() if home is not None else None
        self.persist = persist and self.home is not None
        self.table: dict[str, dict[str, int]] = {}
        if self.persist:
            try:
                self.table = load(confined(self.home, FILE).read_text())
            except (OSError, ValueError):
                self.table = {}

    def channels(self) -> list[str]:
        return sorted(self.table)

    def slot(self, channel: str, agent: str) -> int:
        """The 1-based first-seen ordinal of `agent` on `channel`, assigning one on first use."""
        agents = self.table.setdefault(channel, {})
        if agent not in agents:
            agents[agent] = max(agents.values(), default=0) + 1
            self._save()
        return agents[agent]

    def colour(self, channel: str, agent: str) -> str:
        return PALETTE[(self.slot(channel, agent) - 1) % len(PALETTE)]

    def wrapped(self, channel: str, agent: str) -> bool:
        """Agents past the eighth reuse a colour; the left rule draws dotted for them."""
        return self.slot(channel, agent) > len(PALETTE)

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            atomic_write(confined(self.home, FILE), dump(self.table))
        except (OSError, ValueError):
            pass  # colours are a convenience; a read-only home must not break the console
