"""Owner of the on-disk channel format. Everything else goes through this."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import legacy
from .storage import Store
from .ulid import is_ulid, ulid

MENTION_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9][A-Za-z0-9_-]*)")


def extract_mentions(text: str) -> list[str]:
    out: list[str] = []
    for name in MENTION_RE.findall(text or ""):
        if name not in out:
            out.append(name)
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate_name(value: str, kind: str = "channel") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in (".", ".."):
        raise ValueError(f"invalid {kind} name")
    return value


def valid_timestamp(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def is_message(doc) -> bool:
    """Minimum legacy record contract shared by import and read-only browsing."""
    return (isinstance(doc, dict)
            and is_ulid(doc.get("id"))
            and isinstance(doc.get("from"), str)
            and isinstance(doc.get("text"), str)
            and isinstance(doc.get("mentions"), list)
            and all(isinstance(m, str) for m in doc["mentions"])
            and (doc.get("parent") is None or isinstance(doc["parent"], str))
            and isinstance(doc.get("attachments", []), list)
            and all(isinstance(a, dict) for a in doc.get("attachments", [])))


def default_home() -> Path:
    """`RATEL_HOME`, then the deprecated `AGENTCHAT_HOME`, then `~/.ratel`.

    The old home is not moved; the fallback only keeps existing channels
    reachable until the operator `mv`s `~/.agentchat` themselves.
    """
    home = os.environ.get("RATEL_HOME")
    if home:
        return Path(home).expanduser()
    legacy = os.environ.get("AGENTCHAT_HOME")
    if legacy:
        print("ratel: AGENTCHAT_HOME is deprecated; use RATEL_HOME", file=sys.stderr)
        return Path(legacy).expanduser()
    return (Path.home() / ".ratel").expanduser()


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def normalize_attachment(att: dict, files_dir: Path | None = None) -> dict:
    """Repair an attachment a caller assembled by hand.

    Weak models round-trip `attach_file`'s object back into `post` with keys
    dropped — most often `type`, which the board needs to pick a renderer.
    The shape says what it is, so infer rather than reject: rejecting would
    lose the message, and a raw JSON dump on the board helps nobody.
    """
    if not isinstance(att, dict):
        raise ValueError(f"attachment must be an object, got {type(att).__name__}")
    att = dict(att)
    if not att.get("type"):
        for key, kind in (("ref", "file"), ("url", "link"), ("body", "code"),
                          ("items", "tasks"), ("roles", "clan")):
            if key in att:
                att["type"] = kind
                break
        else:
            raise ValueError(f"attachment needs type: {_dumps(att)}")
    if att["type"] == "file":
        ref = att.get("ref")
        # A bare "01ABC-DESIGN.md" is attach_file's dest_name with the prefix lost.
        if ref and not str(ref).startswith("files/") and files_dir is not None:
            base = Path(str(ref)).name
            if (files_dir / base).exists():
                att["ref"] = f"files/{base}"
        if not att.get("mime"):
            att["mime"] = mimetypes.guess_type(att.get("name") or att.get("ref") or "")[0] or "application/octet-stream"
    return att


class Bus:
    def __init__(self, home: Path | str, channel: str, read_only: bool = False):
        self.home = Path(home).expanduser()
        self.channel = validate_name(channel)
        self.channel_dir = self.home / "channels" / channel
        if not self.channel_dir.resolve().is_relative_to((self.home / "channels").resolve()):
            raise ValueError("channel path escapes channels directory")
        self.bus_path = self.channel_dir / "bus.jsonl"  # legacy input only
        self.db_path = self.channel_dir / "channel.sqlite3"
        self.cursors_dir = self.channel_dir / "cursors"  # legacy input only
        self.files_dir = self.channel_dir / "files"
        self.plans_dir = self.channel_dir / "plans"
        self.read_only = read_only
        self.store = Store(self.db_path)
        if not read_only:
            if self.bus_path.exists() and not self.db_path.exists():
                raise ValueError("legacy channel is read-only; stop its writers and run "
                                 "ratel migrate --channel " + channel)
            for d in (self.files_dir, self.plans_dir):
                d.mkdir(parents=True, exist_ok=True)
            self.store.initialize()

    def _writable(self):
        if self.read_only:
            raise ValueError("this channel was opened read-only")

    @property
    def is_legacy(self):
        return not self.db_path.exists() and self.bus_path.exists()

    def post(self, sender: str, text: str, parent: str | None = None,
             attachments: list[dict] | None = None, pin: bool | str = False,
             unpin: str | None = None, *, advance_sender: bool = False) -> dict:
        self._writable()
        validate_name(sender, "agent")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        if parent is not None and not is_ulid(parent):
            raise ValueError("parent must be a ULID message id or None")
        if attachments is not None and not isinstance(attachments, list):
            raise ValueError("attachments must be a list")
        msg = {"id": ulid(), "ts": now_iso(), "from": sender, "text": text,
               "parent": parent, "mentions": extract_mentions(text),
               "attachments": [normalize_attachment(a, self.files_dir) for a in (attachments or [])],
               "pin": pin if isinstance(pin, str) else bool(pin)}
        if unpin:
            msg["unpin"] = unpin
        return self.store.post(msg, advance_sender=advance_sender)

    def read_all(self) -> list[dict]:
        return self.read_since(None)

    def read_since(self, since: str | None, limit: int | None = None) -> list[dict]:
        if self.is_legacy:
            msgs, _ = legacy.messages(self.bus_path)
            start = next((i + 1 for i, m in enumerate(msgs) if m["id"] == since), 0)
            return msgs[start:start + limit] if limit is not None else msgs[start:]
        return self.store.read_since(since, limit)

    def tip(self) -> str | None:
        if self.is_legacy:
            msgs = self.read_all()
            return msgs[-1]["id"] if msgs else None
        return self.store.tip()

    def consume(self, agent, since=None, limit=None, mention_only=None):
        self._writable()
        validate_name(agent, "agent")
        return self.store.consume(agent, now_iso(), since, limit, mention_only)

    def get_cursor(self, agent: str) -> str | None:
        validate_name(agent, "agent")
        if self.is_legacy:
            value = legacy.cursors(self.cursors_dir).get(agent, {}).get("last_read")
            return value if isinstance(value, str) else None
        return self.store.get_cursor(agent)

    def set_cursor(self, agent: str, last_read: str | None) -> None:
        self._writable()
        validate_name(agent, "agent")
        self.store.set_cursor(agent, last_read, now_iso())

    def touch_cursor(self, agent: str) -> None:
        # A zero sequence only refreshes presence; SQL max() preserves progress.
        self.set_cursor(agent, None)

    def presence(self, window_s: int = 300) -> list[dict]:
        rows = ([(a, d.get("ts")) for a, d in legacy.cursors(self.cursors_dir).items()]
                if self.is_legacy else self.store.cursors())
        now = datetime.now(timezone.utc)
        return [{"agent": agent, "last_seen": ts,
                 "online": (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() <= window_s}
                for agent, ts in rows if valid_timestamp(ts)]

    def read_thread(self, msg_id: str) -> dict | None:
        if self.db_path.exists():
            return self.store.thread(msg_id)
        msgs = self.read_all()
        parent = next((m for m in msgs if m["id"] == msg_id), None)
        return {"parent": parent, "replies": [m for m in msgs if m.get("parent") == msg_id]} if parent else None

    def pins(self) -> list[dict]:
        if self.db_path.exists():
            return self.store.pins()
        msgs = self.read_all()
        by_id = {m["id"]: m for m in msgs}
        pinned = []
        for m in msgs:
            pin = m.get("pin")
            target = m["id"] if pin is True else pin if isinstance(pin, str) else None
            if target and target in by_id and target not in pinned:
                pinned.append(target)
            if m.get("unpin") in pinned:
                pinned.remove(m["unpin"])
        return [by_id[i] for i in pinned]

    def attach_file(self, path: Path | str, name: str | None = None) -> dict:
        self._writable()
        src = Path(path).expanduser()
        name = Path(name or src.name).name
        dest_name = f"{ulid()}-{name}"
        dest = self.files_dir / dest_name
        with src.open("rb") as source, dest.open("xb") as target:
            shutil.copyfileobj(source, target)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        att = {"type": "file", "ref": f"files/{dest_name}", "name": name, "mime": mime}
        if mime == "application/pdf":
            att["pages"] = len(re.findall(rb"/Type\s*/Page(?!s)", dest.read_bytes()))
        return att

    def wait_for_new(self, agent: str, timeout_s: float, mention_only: bool = True,
                     poll_s: float = 0.5) -> list[dict]:
        deadline = time.monotonic() + timeout_s
        while True:
            new = self.consume(agent, mention_only=mention_only)
            if new:
                return new
            if time.monotonic() >= deadline:
                return []
            time.sleep(min(poll_s, max(deadline - time.monotonic(), 0.01)))
