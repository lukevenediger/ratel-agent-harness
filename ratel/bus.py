"""Owner of the on-disk channel format. Everything else goes through this."""
from __future__ import annotations

import fcntl
import json
import mimetypes
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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


def is_message(doc) -> bool:
    """Whether a parsed bus line is a message every reader can dereference:
    a dict with `id`/`from`/`text` as `str` and `mentions` a `list`. The bus is
    agent-writable, so `read_all` and the SSE tail both drop a line this
    rejects. One definition, so the two readers cannot disagree."""
    return (isinstance(doc, dict)
            and isinstance(doc.get("id"), str)
            and isinstance(doc.get("from"), str)
            and isinstance(doc.get("text"), str)
            and isinstance(doc.get("mentions"), list))


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
    MAX_LINE = 4000  # bytes, incl. newline; O_APPEND is atomic under PIPE_BUF (4096)

    def __init__(self, home: Path | str, channel: str, read_only: bool = False):
        """`read_only` skips creating the channel dirs and touching `bus.jsonl`,
        for callers that only read (the board's handlers). Without it a GET
        rewrites the bus mtime, which then no longer means "last activity"."""
        self.home = Path(home).expanduser()
        self.channel = channel
        self.channel_dir = self.home / "channels" / channel
        self.bus_path = self.channel_dir / "bus.jsonl"
        self.cursors_dir = self.channel_dir / "cursors"
        self.files_dir = self.channel_dir / "files"
        self.plans_dir = self.channel_dir / "plans"
        if not read_only:
            for d in (self.cursors_dir, self.files_dir, self.plans_dir):
                d.mkdir(parents=True, exist_ok=True)
            self.bus_path.touch()

    # ---- write ---------------------------------------------------------
    def post(self, sender: str, text: str, parent: str | None = None,
             attachments: list[dict] | None = None, pin: bool | str = False,
             unpin: str | None = None) -> dict:
        if parent is not None and not is_ulid(parent):
            # `parent` becomes a thread id that the watcher and `clan checkpoint`
            # interpolate into typed keystrokes — only a ULID may pass
            raise ValueError("parent must be a ULID message id or None")
        msg = {
            "id": ulid(),
            "ts": now_iso(),
            "from": sender,
            "text": text or "",
            "parent": parent,
            "mentions": extract_mentions(text),
            "attachments": [normalize_attachment(a, self.files_dir) for a in (attachments or [])],
            "pin": pin if isinstance(pin, str) else bool(pin),
        }
        if unpin:
            msg["unpin"] = unpin
        msg = self._fit(msg)
        line = (_dumps(msg) + "\n").encode()
        fd = os.open(self.bus_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return msg

    def _line_len(self, msg: dict) -> int:
        return len(_dumps(msg).encode()) + 1

    def _fit(self, msg: dict) -> dict:
        """Guarantee one line <= MAX_LINE by spilling code bodies, then text, to files/."""
        if self._line_len(msg) <= self.MAX_LINE:
            return msg
        atts = []
        for a in msg["attachments"]:
            if a.get("type") == "code":
                name = f"{msg['id']}-{Path(a.get('file') or 'snippet').name}"
                (self.files_dir / name).write_text(a.get("body", ""))
                atts.append({"type": "file", "ref": f"files/{name}", "name": a.get("file") or name,
                             "mime": "text/plain", "lang": a.get("lang")})
            else:
                atts.append(a)
        msg["attachments"] = atts
        if self._line_len(msg) <= self.MAX_LINE:
            return msg
        name = f"{msg['id']}.md"
        (self.files_dir / name).write_text(msg["text"])
        msg["attachments"].append({"type": "file", "ref": f"files/{name}", "name": name, "mime": "text/markdown"})
        budget = self.MAX_LINE - self._line_len({**msg, "text": ""}) - 8
        msg["text"] = msg["text"].encode()[: max(budget, 0)].decode(errors="ignore").rstrip() + " …"
        return msg

    # ---- read ----------------------------------------------------------
    def read_all(self) -> list[dict]:
        """Every line that parses to a message a reader can dereference.

        bus.jsonl is append-only and agent-writable, so a line can be valid JSON
        of the wrong shape — a bare scalar, an array, or an object with the
        wrong field types. A message every reader relies on has `id`/`from`/
        `text` as `str` and `mentions` as a `list`; anything else is dropped
        here once instead of guarding every call site, because a line no reader
        can dereference is not a message and no longer counts. Invalid JSON was
        already dropped. This is read-side only: every write is O_APPEND and
        nothing rewrites the file from a read, so a dropped line is never
        deleted. A missing bus.jsonl is an empty channel, not an error — the
        read-only `Bus` no longer creates the file, so this must tolerate it.
        """
        out = []
        try:
            f = open(self.bus_path, "rb")
        except FileNotFoundError:
            return []
        with f:
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # torn write in progress
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if is_message(msg):
                    out.append(msg)
        return out

    def read_since(self, since: str | None, limit: int | None = None) -> list[dict]:
        msgs = [m for m in self.read_all() if since is None or m["id"] > since]
        return msgs[:limit] if limit else msgs

    # ---- cursors / presence -------------------------------------------
    def cursor_path(self, agent: str) -> Path:
        return self.cursors_dir / f"{agent}.json"

    def _read_cursor(self, agent: str) -> dict:
        p = self.cursor_path(agent)
        if not p.exists():
            return {}
        with open(p, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                raw = f.read()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        try:
            doc = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {}
        return doc if isinstance(doc, dict) else {}   # valid JSON, wrong shape

    def get_cursor(self, agent: str) -> str | None:
        return self._read_cursor(agent).get("last_read")

    def set_cursor(self, agent: str, last_read: str | None) -> None:
        with open(self.cursor_path(agent), "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                f.truncate()
                f.write(_dumps({"last_read": last_read, "ts": now_iso()}))
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def touch_cursor(self, agent: str) -> None:
        self.set_cursor(agent, self.get_cursor(agent))

    def presence(self, window_s: int = 300) -> list[dict]:
        out = []
        now = datetime.now(timezone.utc)
        for p in sorted(self.cursors_dir.glob("*.json")):
            agent = p.stem
            ts = self._read_cursor(agent).get("ts")
            if not isinstance(ts, str) or not ts:
                continue
            try:
                seen = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            out.append({"agent": agent, "last_seen": ts, "online": (now - seen).total_seconds() <= window_s})
        return out

    # ---- threads / pins -----------------------------------------------
    def read_thread(self, msg_id: str) -> dict | None:
        msgs = self.read_all()
        parent = next((m for m in msgs if m["id"] == msg_id), None)
        if parent is None:
            return None
        return {"parent": parent, "replies": [m for m in msgs if m.get("parent") == msg_id]}

    def pins(self) -> list[dict]:
        msgs = self.read_all()
        by_id = {m["id"]: m for m in msgs}
        pinned: list[str] = []
        for m in msgs:
            pin = m.get("pin")
            target = m["id"] if pin is True else pin if isinstance(pin, str) else None
            if target and target in by_id and target not in pinned:
                pinned.append(target)
            unpin = m.get("unpin")
            if unpin in pinned:
                pinned.remove(unpin)
        return [by_id[i] for i in pinned]

    # ---- attachments --------------------------------------------------
    def attach_file(self, path: Path | str, name: str | None = None) -> dict:
        src = Path(path).expanduser()
        # Flatten: a slash here would make a nested ref, and /files/{ch}/{name} is a
        # 3-segment route, so the board could never serve it.
        name = Path(name or src.name).name
        dest_name = f"{ulid()[:10]}-{name}"
        dest = self.files_dir / dest_name
        shutil.copyfile(src, dest)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        att = {"type": "file", "ref": f"files/{dest_name}", "name": name, "mime": mime}
        if mime == "application/pdf":
            att["pages"] = len(re.findall(rb"/Type\s*/Page(?!s)", dest.read_bytes()))
        return att

    # ---- waiting ------------------------------------------------------
    def wait_for_new(self, agent: str, timeout_s: float, mention_only: bool = True,
                     poll_s: float = 0.5) -> list[dict]:
        deadline = time.monotonic() + timeout_s
        while True:
            new = self.read_since(self.get_cursor(agent))
            if mention_only:
                hit = any(agent in m["mentions"] and m["from"] != agent for m in new)
            else:
                hit = bool(new)
            if hit:
                self.set_cursor(agent, new[-1]["id"])
                return new
            if time.monotonic() >= deadline:
                return []
            time.sleep(min(poll_s, max(deadline - time.monotonic(), 0.01)))
