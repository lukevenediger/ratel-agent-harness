"""Per-channel SQLite storage. Connections and transactions never outlive a call.

Public message IDs remain ULIDs; only the database sequence defines delivery
order. No connection is shared between HTTP threads or forked agent processes.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
SCHEMA = (
    "CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, "
    "parent TEXT, doc TEXT NOT NULL CHECK(json_valid(doc)) "
    "CHECK(json_type(doc) IS 'object' AND json_type(doc,'$.id') IS 'text' "
    "AND json_extract(doc,'$.id')=id AND json_type(doc,'$.from') IS 'text' "
    "AND json_type(doc,'$.text') IS 'text' AND json_type(doc,'$.mentions') IS 'array' "
    "AND (json_type(doc,'$.attachments') IS NULL OR json_type(doc,'$.attachments') IS 'array') "
    "AND json_extract(doc,'$.parent') IS parent))",
    "CREATE INDEX messages_parent ON messages(parent, seq)",
    "CREATE TABLE cursors (agent TEXT PRIMARY KEY, last_seq INTEGER NOT NULL DEFAULT 0, ts TEXT NOT NULL)",
    "CREATE TABLE pins (message_id TEXT PRIMARY KEY REFERENCES messages(id), ordinal INTEGER NOT NULL)",
)


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self, write=False, create=False):
        mode = "rwc" if create else "rw" if write else "ro"
        con = sqlite3.connect(self.path.resolve().as_uri() + f"?mode={mode}", uri=True,
                              timeout=5, isolation_level=None)
        try:
            con.execute("PRAGMA foreign_keys=ON")
            if not create and con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ValueError("unsupported channel database schema; upgrade ratel before opening it")
            if write:
                con.execute("BEGIN IMMEDIATE")
            else:
                con.execute("BEGIN")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def initialize(self):
        # Set WAL outside a transaction, once when a writer opens the channel.
        con = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    version = con.execute("PRAGMA user_version").fetchone()[0]
                    if version not in (0, SCHEMA_VERSION):
                        raise ValueError("unsupported channel database schema; upgrade ratel before opening it")
                    con.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as e:
                    # journal_mode can return BUSY immediately when multiple
                    # processes create the first channel at once. Unlike normal
                    # transactions it needs an explicit bounded retry.
                    if (getattr(e, "sqlite_errorcode", 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        finally:
            con.close()
        with self.connection(write=True, create=True) as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for sql in SCHEMA:
                    con.execute(sql)
                con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise ValueError("unsupported channel database schema; upgrade ratel before opening it")

    @staticmethod
    def sequence(con, message_id):
        if not message_id:
            return 0
        row = con.execute("SELECT seq FROM messages WHERE id=?", (message_id,)).fetchone()
        # An unknown/removed cursor replays history rather than silently losing it.
        return row[0] if row else 0

    @staticmethod
    def cursor(con, agent):
        row = con.execute("SELECT last_seq FROM cursors WHERE agent=?", (agent,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def advance(con, agent, seq, ts):
        con.execute("INSERT INTO cursors(agent,last_seq,ts) VALUES(?,?,?) "
                    "ON CONFLICT(agent) DO UPDATE SET last_seq=max(last_seq,excluded.last_seq), ts=excluded.ts",
                    (agent, seq, ts))

    @staticmethod
    def insert(con, msg):
        seq = con.execute("INSERT INTO messages(id,parent,doc) VALUES(?,?,?)",
                          (msg["id"], msg.get("parent"), json.dumps(msg, ensure_ascii=False))).lastrowid
        Store.apply_pin(con, msg, seq)
        return seq

    @staticmethod
    def apply_pin(con, msg, seq):
        pin = msg.get("pin")
        target = msg["id"] if pin is True else pin if isinstance(pin, str) else None
        if target:
            con.execute("INSERT OR IGNORE INTO pins(message_id,ordinal) "
                        "SELECT id,? FROM messages WHERE id=?", (seq, target))
        if isinstance(msg.get("unpin"), str):
            con.execute("DELETE FROM pins WHERE message_id=?", (msg["unpin"],))

    def post(self, msg, advance_sender=False):
        with self.connection(write=True) as con:
            tip = con.execute("SELECT coalesce(max(seq),0) FROM messages").fetchone()[0]
            caught_up = self.cursor(con, msg["from"]) == tip
            seq = self.insert(con, msg)
            if advance_sender:
                self.advance(con, msg["from"], seq if caught_up else 0, msg["ts"])
        return msg

    @staticmethod
    def rows(con, since=0, limit=None):
        if limit is not None and (isinstance(limit, bool) or limit <= 0):
            raise ValueError("limit must be positive")
        return con.execute("SELECT seq,doc FROM messages WHERE seq>? ORDER BY seq LIMIT ?",
                           (since, limit if limit is not None else -1)).fetchall()

    def read_since(self, since, limit=None):
        if not self.path.exists():
            return []
        with self.connection() as con:
            return [json.loads(row[1]) for row in self.rows(con, self.sequence(con, since), limit)]

    def consume(self, agent, ts, since=None, limit=None, mention_only=None):
        """Read and advance atomically. A failed mention predicate leaves the cursor untouched."""
        with self.connection(write=True) as con:
            seq = self.sequence(con, since) if since is not None else self.cursor(con, agent)
            rows = self.rows(con, seq, limit)
            msgs = [json.loads(row[1]) for row in rows]
            if mention_only is not None:
                hit = any(m["from"] != agent and (not mention_only or agent in m["mentions"]) for m in msgs)
                if not hit:
                    return []
            self.advance(con, agent, rows[-1][0] if rows else 0, ts)
            return msgs

    def get_cursor(self, agent):
        if not self.path.exists():
            return None
        with self.connection() as con:
            row = con.execute("SELECT id FROM messages WHERE seq=?", (self.cursor(con, agent),)).fetchone()
            return row[0] if row else None

    def set_cursor(self, agent, message_id, ts):
        with self.connection(write=True) as con:
            self.advance(con, agent, self.sequence(con, message_id), ts)

    def cursors(self):
        if not self.path.exists():
            return []
        with self.connection() as con:
            return con.execute("SELECT agent,ts FROM cursors ORDER BY agent").fetchall()

    def thread(self, message_id):
        with self.connection() as con:
            parent = con.execute("SELECT doc FROM messages WHERE id=?", (message_id,)).fetchone()
            if parent is None:
                return None
            replies = con.execute("SELECT doc FROM messages WHERE parent=? ORDER BY seq", (message_id,))
            return {"parent": json.loads(parent[0]), "replies": [json.loads(row[0]) for row in replies]}

    def pins(self):
        with self.connection() as con:
            return [json.loads(row[0]) for row in con.execute(
                "SELECT doc FROM pins JOIN messages ON messages.id=pins.message_id ORDER BY ordinal")]

    def tip(self):
        if not self.path.exists():
            return None
        with self.connection() as con:
            row = con.execute("SELECT id FROM messages ORDER BY seq DESC LIMIT 1").fetchone()
            return row[0] if row else None
