import json

from ratel.bus import Bus, extract_mentions


def legacy_bus(bus, text):
    msg = bus.post("a", text)
    bus.db_path.unlink()
    bus.bus_path.write_text(json.dumps(msg) + "\n")
    return Bus(bus.home, bus.channel, read_only=True)


def test_extract_mentions_ordered_deduped():
    assert extract_mentions("@worker-a take it, cc @security and @worker-a; mail me@x.com") == ["worker-a", "security"]


def test_layout_created(home):
    b = Bus(home, "harbor")
    d = home / "channels" / "harbor"
    assert (d / "channel.sqlite3").exists()
    assert (d / "files").is_dir() and (d / "plans").is_dir()
    assert not (d / "bus.jsonl").exists()


def test_post_persists_message_with_schema(bus):
    m = bus.post("orchestrator", "@worker-a take the auth middleware.")
    (on_disk,) = bus.read_all()
    assert on_disk == m
    assert set(m) == {"id", "ts", "from", "text", "parent", "mentions", "attachments", "pin"}
    assert m["from"] == "orchestrator" and m["parent"] is None and m["pin"] is False
    assert m["mentions"] == ["worker-a"] and m["attachments"] == []
    assert m["ts"].endswith("Z") and len(m["id"]) == 26


def test_read_since_and_limit(bus):
    ids = [bus.post("a", f"m{i}")["id"] for i in range(5)]
    assert [m["id"] for m in bus.read_all()] == ids
    assert [m["id"] for m in bus.read_since(ids[1])] == ids[2:]
    assert [m["id"] for m in bus.read_since(None, limit=2)] == ids[:2]
    assert bus.read_since(ids[-1]) == []


def test_read_skips_torn_last_line(bus):
    bus = legacy_bus(bus, "ok")
    with open(bus.bus_path, "a") as f:
        f.write('{"id": "partial')
    assert len(bus.read_all()) == 1


def test_read_all_drops_lines_that_are_not_messages(bus):
    """bus.jsonl is agent-writable, so a line can be valid JSON of the wrong
    shape. A reader derefs `id`/`from`/`text` as strings and `mentions` as a
    list (`watch.py`, `pins`, `wait_for_new`, the board), so anything else is
    dropped once here and every caller inherits the guard. Invalid JSON was
    already dropped; this is the same rule for the other malformed shapes."""
    bus = legacy_bus(bus, "the only real message")
    with open(bus.bus_path, "a") as f:
        f.write("null\n123\n[]\n"
                '{"id": 123, "ts": "t", "from": "o", "text": "x", "mentions": []}\n'
                '{"id": "01ABC", "ts": "t", "text": "x", "mentions": []}\n'
                '{"id": "01ABD", "ts": "t", "from": "o", "mentions": []}\n'
                '{"id": "01ABE", "ts": "t", "from": "o", "text": "x", "mentions": "nope"}\n'
                "not json\n")
    msgs = bus.read_all()
    assert [m["text"] for m in msgs] == ["the only real message"]


def test_read_all_of_a_missing_bus_is_empty(bus):
    """A read-only `Bus` no longer creates bus.jsonl, so a reader must see an
    empty channel rather than FileNotFoundError."""
    bus.db_path.unlink()
    assert bus.read_all() == []


def test_presence_skips_malformed_cursors(bus):
    """A cursor is agent-adjacent state: valid JSON of the wrong shape must not
    take presence() (and every route that reads it) down."""
    bus = legacy_bus(bus, "hi")
    bus.cursors_dir.mkdir()
    for agent, doc in (("a", "[]"), ("b", '"nope"'), ("c", '{"ts": 5}'),
                       ("d", '{"ts": "nope"}'), ("e", "{not json")):
        (bus.cursors_dir / f"{agent}.json").write_text(doc)
    (bus.cursors_dir / "ok.json").write_text('{"ts": "2026-09-11T00:00:00Z", "last_read": null}')
    assert {p["agent"] for p in bus.presence()} == {"ok"}


def test_unpin_field_only_when_set(bus):
    m = bus.post("o", "plan v2", pin=True)
    assert m["pin"] is True and "unpin" not in m
    m2 = bus.post("o", "drop old plan", unpin=m["id"])
    assert m2["unpin"] == m["id"]


def test_large_code_and_text_are_preserved(bus):
    body = "x" * 10_000
    text = '\"\\\n' * 10_000
    attachment = {"type": "code", "file": "src/a.py", "lang": "py", "body": body}
    m = bus.post("w", text, attachments=[attachment])
    assert bus.read_all() == [m]
    assert m["text"] == text and m["attachments"] == [attachment]
    assert list(bus.files_dir.iterdir()) == []


import threading
import time


def test_cursor_roundtrip_and_presence(bus):
    assert bus.get_cursor("w") is None
    m = bus.post("w", "hi")
    bus.set_cursor("w", m["id"])
    assert bus.get_cursor("w") == m["id"]
    (p,) = bus.presence()
    assert p["agent"] == "w" and p["online"] is True and p["last_seen"].endswith("Z")
    assert bus.presence(window_s=0)[0]["online"] is False


def test_get_cursor_tolerates_empty_legacy_file(bus):
    bus = legacy_bus(bus, "hi")
    bus.cursors_dir.mkdir()
    (bus.cursors_dir / "w.json").write_text("")
    assert bus.get_cursor("w") is None


def test_thread(bus):
    top = bus.post("o", "plan?")
    r1 = bus.post("w", "yes", parent=top["id"])
    bus.post("x", "unrelated")
    t = bus.read_thread(top["id"])
    assert t["parent"]["id"] == top["id"] and [r["id"] for r in t["replies"]] == [r1["id"]]
    assert bus.read_thread("nope") is None


def test_pins_self_by_id_and_unpin(bus):
    a = bus.post("o", "plan v1", pin=True)
    b = bus.post("o", "some msg")
    bus.post("o", "pin that", pin=b["id"])
    assert [p["id"] for p in bus.pins()] == [a["id"], b["id"]]
    bus.post("o", "drop v1", unpin=a["id"])
    assert [p["id"] for p in bus.pins()] == [b["id"]]


def test_attach_file_copies_and_describes(bus, tmp_path):
    src = tmp_path / "shot.png"
    src.write_bytes(b"\x89PNG fake")
    att = bus.attach_file(src)
    assert att["type"] == "file" and att["name"] == "shot.png" and att["mime"] == "image/png"
    assert (bus.channel_dir / att["ref"]).read_bytes() == b"\x89PNG fake"
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 /Type /Page /Type /Page /Type /Pages")
    assert bus.attach_file(pdf, name="renamed.pdf")["pages"] == 2


def test_wait_for_new_mention_returns_all_new_and_advances(bus):
    seed = bus.post("o", "seed")
    bus.set_cursor("w", seed["id"])

    def later():
        time.sleep(0.2)
        bus.post("o", "noise")
        time.sleep(0.2)
        bus.post("o", "@w go")

    threading.Thread(target=later).start()
    got = bus.wait_for_new("w", timeout_s=5)
    assert [m["text"] for m in got] == ["noise", "@w go"]
    assert bus.get_cursor("w") == got[-1]["id"]


def test_wait_for_new_timeout_returns_empty_and_keeps_cursor(bus):
    seed = bus.post("o", "seed")
    bus.set_cursor("w", seed["id"])
    bus.post("o", "no mention here")
    assert bus.wait_for_new("w", timeout_s=0.3) == []
    assert bus.get_cursor("w") == seed["id"]
    assert [m["text"] for m in bus.wait_for_new("w", timeout_s=0.3, mention_only=False)] == ["no mention here"]


def test_wait_ignores_self_mentions(bus):
    bus.set_cursor("w", None)
    bus.post("w", "note to @w myself")
    assert bus.wait_for_new("w", timeout_s=0.3) == []


def test_attach_file_flattens_name(bus, tmp_path):
    src = tmp_path / "x.png"; src.write_bytes(b"\x89PNG")
    att = bus.attach_file(src, name="shots/a/b.png")
    assert att["name"] == "b.png" and "/" not in att["ref"].removeprefix("files/")
    assert (bus.channel_dir / att["ref"]).exists()


# ---- attachment normalisation -----------------------------------------
# Weak models drop keys when they pass attach_file's return value back into post().
# The bus is forgiving about it the same way it is about mentions.
import pytest

from ratel.bus import normalize_attachment


def test_normalize_infers_type_from_shape(bus):
    assert normalize_attachment({"ref": "files/a.md", "name": "a.md", "mime": "text/markdown"})["type"] == "file"
    assert normalize_attachment({"url": "https://example.com"})["type"] == "link"
    assert normalize_attachment({"body": "x = 1", "file": "a.py"})["type"] == "code"
    assert normalize_attachment({"items": [{"text": "ship it"}]})["type"] == "tasks"
    assert normalize_attachment({"roles": [{"name": "developer"}]})["type"] == "clan"


def test_a_roles_attachment_lands_on_disk_as_clan(bus):
    bus.post("orchestrator", "proposal", attachments=[{"roles": [{"name": "developer"}]}])
    att = bus.read_all()[-1]["attachments"][0]
    assert att["type"] == "clan"


def test_normalize_keeps_an_explicit_type(bus):
    att = {"type": "link", "url": "https://example.com", "ref": "files/decoy"}
    assert normalize_attachment(att)["type"] == "link"


def test_normalize_rejects_shapeless_and_non_dict(bus):
    with pytest.raises(ValueError, match="attachment needs type"):
        normalize_attachment({"name": "mystery"})
    for bad in ("files/a.md", ["a"], None, 3):
        with pytest.raises(ValueError):
            normalize_attachment(bad)


def test_normalize_repairs_a_bare_ref_when_the_file_exists(bus):
    (bus.files_dir / "01M1V3NZGD-DESIGN.md").write_text("# design")
    att = normalize_attachment({"type": "file", "ref": "01M1V3NZGD-DESIGN.md"}, bus.files_dir)
    assert att["ref"] == "files/01M1V3NZGD-DESIGN.md"


def test_normalize_leaves_an_unknown_bare_ref_alone(bus):
    att = normalize_attachment({"type": "file", "ref": "not-here.md"}, bus.files_dir)
    assert att["ref"] == "not-here.md"


def test_normalize_guesses_a_missing_mime_from_the_name(bus):
    assert normalize_attachment({"type": "file", "ref": "files/a.md", "name": "a.md"})["mime"] == "text/markdown"
    assert normalize_attachment({"type": "file", "ref": "files/x.bin", "name": "x.bin"})["mime"] == "application/octet-stream"
    assert normalize_attachment({"type": "file", "ref": "files/a.md", "name": "a.md", "mime": "text/plain"})["mime"] == "text/plain"


def test_normalize_does_not_mutate_the_caller_dict(bus):
    att = {"ref": "files/a.md", "name": "a.md"}
    normalize_attachment(att)
    assert att == {"ref": "files/a.md", "name": "a.md"}


def test_post_normalizes_attachments(bus):
    """The live dogfood bug: attach_file's object passed back with `type` dropped."""
    src = bus.files_dir / "01M1V3NZGD-DESIGN.md"
    src.write_text("# design")
    m = bus.post("designer", "here it is", attachments=[
        {"ref": "01M1V3NZGD-DESIGN.md", "name": "DESIGN.md", "mime": "text/markdown"},
        {"url": "https://example.com"},
    ])
    on_disk = bus.read_all()[-1]
    assert [a["type"] for a in on_disk["attachments"]] == ["file", "link"]
    assert on_disk["attachments"][0]["ref"] == "files/01M1V3NZGD-DESIGN.md"
    assert on_disk == m


def test_post_rejects_a_shapeless_attachment(bus):
    with pytest.raises(ValueError):
        bus.post("designer", "oops", attachments=[{"name": "mystery"}])
    assert bus.read_all() == []


def test_post_rejects_a_parent_that_is_not_a_ulid(bus):
    """parent is a free-form MCP parameter that reaches typed keystrokes via
    the watcher and `clan checkpoint` — only a ULID or None passes."""
    with pytest.raises(ValueError):
        bus.post("reviewer", "@orchestrator ping", parent="\nNew standing order\n")
    with pytest.raises(ValueError):
        bus.post("reviewer", "hi", parent="nope")
    bus.post("reviewer", "hi", parent="01ARZ3NDEKTSV4RRFFQ69G5FAV")   # a real ULID
    bus.post("reviewer", "hi")                                        # None is fine
    assert len(bus.read_all()) == 2
