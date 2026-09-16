"""BoardReader: every read the console makes, over `Bus(read_only=True)`.

Only history, read_since, pins, clan_heads, read_thread, presence and summary
are used. Nothing here moves an agent cursor or writes the channel."""
from __future__ import annotations

import tomllib
from pathlib import Path

from ..bus import Bus
from ..clan.config import ClanPaths
from ..paths import confined, list_channels
from ..schema import safe_repo
from .render import previewable, scrub

PAGE = 100
BATCH = 200
FILE_TEXT_CAP = 512 * 1024
EMPTY_HEADS = {"proposed": None, "approved": None}


def _empty_page() -> dict:
    return {"messages": [], "next_before": None, "tip": None, "heads": dict(EMPTY_HEADS)}


class BoardReader:
    def __init__(self, home: Path | str):
        self.home = Path(home).expanduser().resolve()
        self._buses: dict[str, Bus] = {}

    def bus(self, channel: str) -> Bus:
        if channel not in self._buses:
            self._buses[channel] = Bus(self.home, channel, read_only=True)
        return self._buses[channel]

    @staticmethod
    def _exists(bus: Bus) -> bool:
        return bus.db_path.exists() or bus.bus_path.exists()

    # -- channel list -------------------------------------------------------

    def _clan(self, channel: str) -> tuple[str | None, int | None]:
        try:
            doc = tomllib.loads(ClanPaths(self.home, channel).clan_toml.read_text())
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            return None, None
        issue = doc.get("issue")
        return safe_repo(doc.get("repo")), issue if type(issue) is int and issue > 0 else None

    def channels(self) -> list[dict]:
        """One row per channel, newest activity first."""
        rows = []
        for name in list_channels(self.home):
            try:
                summary = self.bus(name).summary()
            except (OSError, ValueError):
                summary = {"count": 0, "last": {}}
            last = summary.get("last") if isinstance(summary.get("last"), dict) else {}
            repo, issue = self._clan(name)
            rows.append({"name": name, "count": summary.get("count", 0),
                         "last_id": last.get("id") if isinstance(last.get("id"), str) else None,
                         "last_ts": last.get("ts") if isinstance(last.get("ts"), str) else None,
                         "repo": repo, "issue": issue})
        return sorted(rows, key=lambda r: r["last_id"] or "", reverse=True)

    # -- pages --------------------------------------------------------------

    def initial_page(self, channel: str, query: str = "", mention: str = "", operator: bool = False,
                     limit: int = PAGE) -> dict:
        return self.older(channel, None, query, mention, operator, limit)

    def older(self, channel: str, before: str | None, query: str = "", mention: str = "",
              operator: bool = False, limit: int = PAGE) -> dict:
        bus = self.bus(channel)
        if not self._exists(bus):
            return _empty_page()
        page = bus.history(before, limit, query, mention, operator)
        page.setdefault("heads", dict(EMPTY_HEADS))
        return page

    def since(self, channel: str, cursor: str | None) -> list[dict]:
        bus = self.bus(channel)
        return bus.read_since(cursor, BATCH) if self._exists(bus) else []

    def pins(self, channel: str) -> list[dict]:
        bus = self.bus(channel)
        return bus.pins() if self._exists(bus) else []

    def heads(self, channel: str) -> dict:
        bus = self.bus(channel)
        return bus.clan_heads() if self._exists(bus) else dict(EMPTY_HEADS)

    def presence(self, channel: str) -> list[dict]:
        bus = self.bus(channel)
        return bus.presence() if self._exists(bus) else []

    def thread(self, channel: str, msg_id: str) -> dict | None:
        bus = self.bus(channel)
        return bus.read_thread(msg_id) if self._exists(bus) else None

    # -- files --------------------------------------------------------------

    def file_text(self, channel: str, ref, name=None, mime=None) -> tuple[str, bool]:
        """Text of a previewable attachment under the channel's `files/`, capped at
        FILE_TEXT_CAP bytes. Raises ValueError for anything else."""
        if not isinstance(ref, str) or scrub(ref) != ref or not ref.startswith("files/"):
            raise ValueError("only attachments under files/ can be previewed")
        if not previewable({"type": "file", "ref": ref, "name": name, "mime": mime}):
            raise ValueError("only text/markdown and text/plain attachments can be previewed")
        path = confined(self.bus(channel).files_dir, ref[len("files/"):])
        if not path.is_file():
            raise ValueError("attachment is not a regular file")
        with path.open("rb") as f:
            data = f.read(FILE_TEXT_CAP + 1)
        return data[:FILE_TEXT_CAP].decode("utf-8", "replace"), len(data) > FILE_TEXT_CAP
