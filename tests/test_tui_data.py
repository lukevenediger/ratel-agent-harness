"""BoardReader: read-only reads over Bus, and the list_channels move to ratel.paths."""
import json
import os
import re
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from ratel import board, paths
from ratel.bus import Bus
from ratel.demo import seed
from ratel.tui.data import FILE_TEXT_CAP, BoardReader

TUI = Path(__file__).resolve().parents[1] / "ratel" / "tui"


def test_list_channels_lives_in_paths_and_board_reuses_it(tmp_path):
    Bus(tmp_path, "alpha").post("a", "hi")
    (tmp_path / "channels" / "junk").mkdir()
    assert paths.list_channels(tmp_path) == ["alpha"]
    assert board.list_channels is paths.list_channels


def test_ratel_tui_console_script_is_declared():
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert scripts.get("ratel-tui") == "ratel.tui.main:main"


def test_tui_package_never_names_a_cursor_moving_bus_method():
    # A plain substring match, not a word match: the guarantee must hold for
    # `grep consume ratel/tui`, so even "consumes" in a docstring is out.
    forbidden = re.compile(r"consume|wait_for_new|set_cursor|touch_cursor")
    for path in TUI.rglob("*.py"):
        assert not forbidden.search(path.read_text()), path


def test_pure_modules_never_import_textual():
    imports = re.compile(r"^\s*(import|from)\s+textual\b", re.M)
    for name in ("model", "render", "slots", "data", "poll"):
        assert not imports.search((TUI / f"{name}.py").read_text()), name


def test_read_only_reader_creates_no_files_or_plans_directories(tmp_path):
    home = tmp_path / "home"
    Bus(home, "alpha").post("orch", "hello @dev", pin=True)
    ch = home / "channels" / "alpha"
    for d in ("files", "plans"):
        (ch / d).rmdir()
    before = sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if not p.name.startswith("channel.sqlite3-"))
    r = BoardReader(home)
    r.channels(); r.initial_page("alpha"); r.since("alpha", None); r.pins("alpha"); r.heads("alpha")
    r.presence("alpha"); r.thread("alpha", r.initial_page("alpha")["messages"][0]["id"]); r.older("alpha", None)
    after = sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if not p.name.startswith("channel.sqlite3-"))
    assert after == before
    assert not (ch / "files").exists() and not (ch / "plans").exists()
    for bus in r._buses.values():
        assert bus.read_only


def test_reader_never_creates_a_database_for_a_legacy_or_missing_channel(tmp_path):
    ch = tmp_path / "channels" / "legacy"
    ch.mkdir(parents=True)
    line = {"id": "01M2P8ARVJXWGRN3KZDDB8DMD3", "ts": "2026-09-16T10:00:00.000Z", "from": "a", "text": "t",
            "parent": None, "mentions": [], "attachments": [], "pin": False}
    (ch / "bus.jsonl").write_text(json.dumps(line) + "\n")
    r = BoardReader(tmp_path)
    assert [m["id"] for m in r.initial_page("legacy")["messages"]] == [line["id"]]
    assert r.initial_page("missing")["messages"] == [] and r.since("missing", None) == []
    assert r.pins("missing") == [] and r.presence("missing") == [] and r.thread("missing", line["id"]) is None
    assert not (ch / "channel.sqlite3").exists() and not (tmp_path / "channels" / "missing").exists()


def test_channels_sorted_newest_first_with_count_and_repo(tmp_path):
    Bus(tmp_path, "old").post("a", "first")
    Bus(tmp_path, "new").post("a", "later"); Bus(tmp_path, "new").post("b", "again")
    clan = tmp_path / "channels" / "old" / "clan"
    clan.mkdir()
    (clan / "clan.toml").write_text('channel = "old"\nissue = 7\nrepo = "o/r"\n')
    rows = BoardReader(tmp_path).channels()
    assert [r["name"] for r in rows] == ["new", "old"]
    assert rows[0]["count"] == 2 and rows[0]["repo"] is None and rows[1]["repo"] == "o/r" and rows[1]["issue"] == 7
    (clan / "clan.toml").write_text('repo = "../evil"\n')
    assert BoardReader(tmp_path).channels()[1]["repo"] is None


def test_pages_and_since_and_thread_over_the_demo(tmp_path):
    seed(tmp_path / "demo")
    r = BoardReader(tmp_path / "demo")
    page = r.initial_page("harbor-demo")
    assert len(page["messages"]) == 6 and page["next_before"] is None and page["tip"] == page["messages"][-1]["id"]
    assert set(page["heads"]) == {"proposed", "approved"}
    first = r.initial_page("harbor-demo", limit=2)
    assert len(first["messages"]) == 2 and first["next_before"]
    older = r.older("harbor-demo", first["next_before"], limit=2)
    assert older["messages"][-1]["id"] < first["messages"][0]["id"]
    assert r.initial_page("harbor-demo", query="health")["messages"]
    assert [m["from"] for m in r.initial_page("harbor-demo", operator=True)["messages"]] == ["reviewer"]
    assert r.since("harbor-demo", page["tip"]) == []
    assert len(r.since("harbor-demo", page["messages"][3]["id"])) == 2
    thread = r.thread("harbor-demo", page["messages"][0]["id"])
    assert len(thread["replies"]) == 3
    assert [p["id"] for p in r.pins("harbor-demo")] == [page["messages"][0]["id"]]
    assert r.presence("harbor-demo") == []


def test_file_text_is_confined_to_files_and_capped(tmp_path):
    seed(tmp_path / "demo")
    r = BoardReader(tmp_path / "demo")
    text, truncated = r.file_text("harbor-demo", "files/review.md")
    assert text.startswith("# Review notes") and truncated is False
    files = tmp_path / "demo" / "channels" / "harbor-demo" / "files"
    (files / "big.txt").write_text("x" * (FILE_TEXT_CAP + 10))
    text, truncated = r.file_text("harbor-demo", "files/big.txt")
    assert len(text) == FILE_TEXT_CAP and truncated is True
    (files / "raw.bin").write_bytes(b"\xff\xfe" + "héllo".encode())
    assert r.file_text("harbor-demo", "files/raw.bin", mime="text/plain")[0].endswith("héllo")
    secret = tmp_path / "secret.txt"
    secret.write_text("no")
    os.symlink(secret, files / "link.txt")
    for ref in ("files/../channel.sqlite3", "/etc/passwd", "plans/demo.md", "files/link.txt", "files/missing.md",
                "files/\x00x.md"):
        with pytest.raises(ValueError):
            r.file_text("harbor-demo", ref)
    with pytest.raises(ValueError):
        r.file_text("harbor-demo", "files/raw.bin")            # not previewable by mime or name


def test_file_text_scrubs_escapes_controls_and_bidi(tmp_path):
    # Attachment bytes are agent-authored: OSC 52 (clipboard write), CSI and bare escapes,
    # CR and Unicode bidi overrides must never reach a widget from the preview path.
    seed(tmp_path / "demo")
    files = tmp_path / "demo" / "channels" / "harbor-demo" / "files"
    payload = "# hi\n\x1b]52;c;Y3VybA==\x1b\\ \x1b[2J\x1b[6n ok\r\n\u202eVERDICT\u2066x\u2069\n"
    (files / "evil.md").write_text(payload)
    text, truncated = BoardReader(tmp_path / "demo").file_text("harbor-demo", "files/evil.md")
    assert "\x1b" not in text and "\r" not in text
    assert not any(ch in text for ch in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
    assert "ok\nVERDICTx\n" in text and truncated is False
