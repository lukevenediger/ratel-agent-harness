"""Storage invariants exercised with real connections and independent processes."""
import json
import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from unittest.mock import patch

import pytest

from ratel.bus import Bus
from ratel.cli import main
from ratel.legacy import migrate
from ratel.ops import AgentOps
from ratel.storage import Store

HIGH = "01K00000000000000000000009"
LOW = "01K00000000000000000000001"


def _writer(home, name):
    bus = Bus(home, "concurrent")
    ops = AgentOps(bus, name)
    seen = []
    for i in range(20):
        ops.post(f"{name}-{i}")
        seen.extend(m["text"] for m in ops.read_channel())
    return name, seen


def test_independent_writers_never_skip_other_agents_messages(home):
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_writer, [home] * 4, ["a", "b", "c", "d"]))
    bus = Bus(home, "concurrent")
    messages = bus.read_all()
    assert len(messages) == 80 and len({m["id"] for m in messages}) == 80
    for name, seen in results:
        seen.extend(m["text"] for m in AgentOps(bus, name).read_channel())
        assert len(seen) == 60
        assert set(seen) == {m["text"] for m in messages if m["from"] != name}


def test_delivery_and_cursors_follow_append_order_not_ulids(bus):
    ops = AgentOps(bus, "reader")
    with patch("ratel.bus.ulid", side_effect=[HIGH, LOW]):
        first = bus.post("writer", "first")
        assert ops.read_channel() == [first]
        second = bus.post("writer", "second")
    assert bus.read_since(first["id"]) == [second]
    assert ops.read_channel() == [second]
    assert bus.get_cursor("reader") == LOW
    bus.set_cursor("reader", HIGH)
    bus.touch_cursor("reader")
    assert bus.get_cursor("reader") == LOW


def test_post_and_cursor_rollback_together(bus):
    ops = AgentOps(bus, "writer")
    first = ops.post("first")
    original = Store.insert

    def failing_insert(con, msg):
        original(con, msg)
        raise RuntimeError("interrupted before cursor update")

    with patch.object(Store, "insert", side_effect=failing_insert), pytest.raises(RuntimeError):
        ops.post("must not persist")
    assert [m["text"] for m in bus.read_all()] == ["first"]
    assert bus.get_cursor("writer") == first


def test_connections_release_transaction_before_return(bus):
    bus.post("writer", "one")
    assert bus.read_all()
    with sqlite3.connect(bus.db_path, timeout=0) as con:
        con.execute("BEGIN EXCLUSIVE")
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("doc", [None, [], {"id": HIGH}, {"id": 123}])
def test_schema_rejects_malformed_message_rows(bus, doc):
    with sqlite3.connect(bus.db_path) as con, pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO messages(id,doc) VALUES(?,?)", (HIGH, json.dumps(doc)))
    assert bus.read_all() == []


def test_identical_timestamp_attachment_names_do_not_overwrite(bus, tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("first")
    with patch("ratel.bus.ulid", side_effect=[HIGH, LOW]):
        first = bus.attach_file(src, "same.txt")
        src.write_text("second")
        second = bus.attach_file(src, "same.txt")
    assert first["ref"] != second["ref"]
    assert (bus.channel_dir / first["ref"]).read_text() == "first"
    assert (bus.channel_dir / second["ref"]).read_text() == "second"


def test_exact_attachment_collision_fails_without_overwrite(bus, tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("first")
    with patch("ratel.bus.ulid", return_value=HIGH):
        first = bus.attach_file(src)
        src.write_text("second")
        with pytest.raises(FileExistsError):
            bus.attach_file(src)
    assert (bus.channel_dir / first["ref"]).read_text() == "first"


def _legacy_channel(home, docs):
    channel = home / "channels" / "old"
    (channel / "cursors").mkdir(parents=True)
    (channel / "bus.jsonl").write_text("".join(json.dumps(m) + "\n" for m in docs))
    return channel


def _message(mid, text, **extra):
    return {"id": mid, "from": "writer", "text": text, "mentions": [],
            "ts": "2026-09-14T00:00:00Z", "attachments": [], "parent": None, "pin": False, **extra}


def test_migration_preserves_file_order_cursors_pins_and_originals(home):
    first = _message(HIGH, "first", pin=True)
    second = _message(LOW, "second", parent=HIGH)
    channel = _legacy_channel(home, [first, second])
    cursor = channel / "cursors" / "reader.json"
    cursor.write_text(json.dumps({"last_read": HIGH, "ts": first["ts"]}))
    originals = {p: p.read_bytes() for p in (channel / "bus.jsonl", cursor)}
    assert Bus(home, "old", read_only=True).read_all() == [first, second]
    with pytest.raises(ValueError, match="ratel migrate"):
        Bus(home, "old")
    result = migrate(channel)
    assert result["messages"] == 2 and result["cursors"] == 1
    bus = Bus(home, "old")
    assert AgentOps(bus, "reader").read_channel() == [second]
    assert bus.pins() == [first]
    assert bus.read_thread(HIGH) == {"parent": first, "replies": [second]}
    bus.post("writer", "third")
    assert len(bus.read_all()) == 3
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    with pytest.raises(ValueError, match="already uses SQLite"):
        migrate(channel)
    assert len(bus.read_all()) == 3


def test_migration_reports_invalid_lines_and_keeps_them_in_original(home):
    channel = _legacy_channel(home, [_message(HIGH, "valid")])
    with (channel / "bus.jsonl").open("ab") as f:
        f.write(b'null\n\xff\n{"id": "unfinished')
    cursor = channel / "cursors" / "reader.json"
    cursor.write_text('{"ts":"2026-09-14T00:00:00"}')
    original = (channel / "bus.jsonl").read_bytes()
    result = migrate(channel)
    assert result["messages"] == 1 and result["skipped_lines"] == 3 and result["cursors"] == 0
    assert (channel / "bus.jsonl").read_bytes() == original


def test_failed_migration_does_not_publish_partial_database(home):
    channel = _legacy_channel(home, [_message(HIGH, "one"), _message(HIGH, "duplicate")])
    with pytest.raises(sqlite3.IntegrityError):
        migrate(channel)
    assert not (channel / "channel.sqlite3").exists()
    assert not list(channel.glob(".migration-*"))
    assert len(Bus(home, "old", read_only=True).read_all()) == 2


def test_migration_preserves_legacy_forward_pin_references(home):
    target = _message(LOW, "target")
    channel = _legacy_channel(home, [_message(HIGH, "pin future target", pin=LOW), target])
    expected = Bus(home, "old", read_only=True).pins()
    migrate(channel)
    assert Bus(home, "old").pins() == expected == [target]


def test_migration_aborts_when_source_changes(home):
    channel = _legacy_channel(home, [_message(HIGH, "first")])
    original = Store.insert

    def changing_insert(con, msg):
        seq = original(con, msg)
        with (channel / "bus.jsonl").open("a") as f:
            f.write(json.dumps(_message(LOW, "concurrent write")) + "\n")
        return seq

    with patch.object(Store, "insert", side_effect=changing_insert), pytest.raises(ValueError, match="changed"):
        migrate(channel)
    assert not (channel / "channel.sqlite3").exists()
    assert len(Bus(home, "old", read_only=True).read_all()) == 2


def test_cli_migration_export_and_tail_do_not_require_agent(home, capsys, monkeypatch):
    monkeypatch.delenv("AGENT_NAME", raising=False)
    docs = [_message(HIGH, "first"), _message(LOW, "second")]
    _legacy_channel(home, docs)
    main(["migrate", "--channel", "old"])
    assert json.loads(capsys.readouterr().out)["messages"] == 2
    main(["export", "--channel", "old"])
    assert [json.loads(s) for s in capsys.readouterr().out.splitlines()] == docs
    with patch("ratel.cli.time.sleep", side_effect=KeyboardInterrupt):
        main(["tail", "--channel", "old", "--since", HIGH])
    assert json.loads(capsys.readouterr().out) == docs[1]
    assert Bus(home, "old").presence() == []


@pytest.mark.parametrize("name", ["..", ".", "../../outside", "/absolute", "bad\nname"])
def test_invalid_channel_names_create_nothing(home, name):
    with pytest.raises(ValueError):
        Bus(home, name)
    assert not (home / "channels").exists()


def test_newer_schema_is_refused(bus):
    with sqlite3.connect(bus.db_path) as con:
        con.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="schema"):
        bus.read_all()


def test_unknown_cursor_replays_instead_of_losing_messages(bus):
    msg = bus.post("a", "recoverable")
    assert bus.read_since("removed-id") == [msg]


def test_empty_read_only_channel_does_not_create_files(home):
    bus = Bus(home, "missing", read_only=True)
    assert bus.read_all() == [] and bus.presence() == [] and bus.pins() == []
    assert not bus.channel_dir.exists()
