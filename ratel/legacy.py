"""Read-only JSONL compatibility and explicit, offline SQLite import.

Stop all channel writers before migration. Original files are never rewritten;
old binaries must not be restarted against a migrated channel.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .paths import confined
from .schema import is_message, valid_timestamp, validate_name
from .storage import Store


def messages(path: Path):
    if not path.exists():
        return [], 0
    out, skipped = [], 0
    path = confined(path.parent, path.name)
    with path.open("rb") as f:
        for raw in f:
            try:
                doc = json.loads(raw) if raw.endswith(b"\n") else None
            except (ValueError, UnicodeDecodeError):
                doc = None
            if is_message(doc):
                out.append(doc)
            else:
                skipped += 1
    return out, skipped


def cursors(directory: Path):
    out = {}
    for path in sorted(directory.glob("*.json")):
        try:
            validate_name(path.stem, "agent")
            doc = json.loads(confined(directory, path.name).read_text())
        except (ValueError, UnicodeDecodeError):
            continue
        if (isinstance(doc, dict) and valid_timestamp(doc.get("ts"))
                and (doc.get("last_read") is None or isinstance(doc["last_read"], str))):
            out[path.stem] = doc
    return out


def invalid_cursors(directory: Path):
    return len(list(directory.glob("*.json"))) - len(cursors(directory))


def migrate(channel_dir: Path):
    target = confined(channel_dir, "channel.sqlite3")
    source = confined(channel_dir, "bus.jsonl")
    if target.exists():
        raise ValueError("channel already uses SQLite; import was not repeated")
    if not source.is_file():
        raise ValueError("no legacy bus.jsonl to migrate")
    tracked = [source, *sorted((channel_dir / "cursors").glob("*.json"))]

    def fingerprint():
        return [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in tracked]

    before = fingerprint()
    docs, skipped = messages(source)
    cursor_docs = cursors(channel_dir / "cursors")
    fd, name = tempfile.mkstemp(prefix=".migration-", suffix=".sqlite3", dir=channel_dir)
    os.close(fd)
    temp = Path(name)
    try:
        store = Store(temp)
        store.initialize()
        imported_cursors = 0
        with store.connection(write=True) as con:
            imported = []
            for msg in docs:
                imported.append((Store.insert(con, msg), msg))  # duplicate IDs abort the whole import
            # Legacy replay resolved pin targets against ALL messages, including
            # a target appearing later in file order. Preserve that exact state.
            con.execute("DELETE FROM pins")
            for seq, msg in imported:
                Store.apply_pin(con, msg, seq)
            for agent, doc in cursor_docs.items():
                try:
                    validate_name(agent, "agent")
                except ValueError:
                    continue
                if not valid_timestamp(doc.get("ts")):
                    continue
                last = doc.get("last_read")
                Store.advance(con, agent, Store.sequence(con, last if isinstance(last, str) else None), doc["ts"])
                imported_cursors += 1
        if before != fingerprint():
            raise ValueError("legacy files changed during import; stop all writers and retry")
        # All connections are closed: SQLite has checkpointed the WAL. Publish
        # without replacing a competing import's result. fsync before publication.
        with temp.open("rb") as f:
            os.fsync(f.fileno())
        os.link(temp, target)
        directory_fd = os.open(channel_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {"messages": len(docs), "cursors": imported_cursors, "skipped_lines": skipped,
                "database": str(target), "legacy_files_retained": True}
    finally:
        for path in (temp, Path(str(temp) + "-wal"), Path(str(temp) + "-shm")):
            path.unlink(missing_ok=True)
