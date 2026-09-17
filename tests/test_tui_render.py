"""render: agent text is hostile. Explicit spans only, never parsed markup."""
import json
from datetime import date, timezone

from rich.text import Text

from ratel.tui import render

BLUE = "#82a7ff"


def colour(agent):
    return BLUE


def styles(text: Text, needle: str):
    """The style strings covering `needle` in `text`."""
    start = text.plain.index(needle)
    end = start + len(needle)
    return [str(s.style).lower() for s in text.spans if s.start <= start and s.end >= end]


def test_scrub_removes_c0_c1_and_del_but_keeps_newline_and_tab():
    assert render.scrub("a\x00b\x1b[31mc\x7fd\x85e\n\tf") == "abcde\n\tf"


def test_scrub_removes_cr_and_unicode_bidi_controls():
    # CR is C0 too; bidi overrides and isolates reorder a line visually and can
    # spoof a VERDICT or a @mention in an audit console.
    assert render.scrub("a\rb\u202ac\u202bd\u202ce\u202df\u202eg\u2066h\u2067i\u2068j\u2069k\n") == "abcdefghijk\n"


def test_body_never_parses_rich_markup():
    text = render.body("[bold red]x[/] [link=http://e]y[/link]", colour)
    assert text.plain == "[bold red]x[/] [link=http://e]y[/link]"
    assert text.spans == []


def test_body_inline_markup_becomes_spans():
    text = render.body("**b** *e* `c` ~~s~~ @dev", colour)
    assert text.plain == "b e c s @dev"
    assert "bold" in styles(text, "b")[0]
    assert "italic" in styles(text, "e")[0]
    assert styles(text, "c")
    assert "strike" in styles(text, "s")[0]
    assert BLUE in styles(text, "@dev")[0]


def test_body_scrubs_controls_before_rendering():
    assert render.body("hi\x1b[2Jthere", colour).plain == "hithere"


def test_body_fence_truncated_to_six_lines_with_hint():
    lines = "\n".join(f"l{i}" for i in range(1, 11))
    text = render.body(f"intro\n```py\n{lines}\n```\nafter", colour)
    assert "l6" in text.plain and "l7" not in text.plain
    assert "… +4 lines (o)" in text.plain
    assert text.plain.startswith("intro\n") and text.plain.endswith("after")


def test_header_shows_sender_time_and_replies():
    msg = {"id": "01A", "from": "dev", "ts": "2026-09-16T10:22:01.334Z"}
    text = render.header(msg, colour, replies=2, tz=timezone.utc)
    assert text.plain == "▌dev 10:22  ↳ 2 replies"
    assert BLUE in styles(text, "▌dev")[0]
    assert render.header(msg, colour, replies=1, tz=timezone.utc).plain.endswith("↳ 1 reply")
    assert render.header(msg, colour, replies=0, tz=timezone.utc).plain == "▌dev 10:22"


def test_header_scrubs_hostile_sender_and_bad_timestamp():
    msg = {"id": "01A", "from": "d\x1bev", "ts": "not a time"}
    assert render.header(msg, colour, replies=0, tz=timezone.utc).plain == "▌dev --:--"


def test_day_label():
    today = date(2026, 9, 16)
    assert render.day_label(date(2026, 9, 16), today) == "Today"
    assert render.day_label(date(2026, 9, 15), today) == "Yesterday"
    assert render.day_label(date(2026, 9, 1), today) == "Sep 1"


def test_attachment_code_shows_six_body_lines_then_hint():
    body = "\n".join(f"l{i}" for i in range(1, 10))
    text = render.attachment({"type": "code", "file": "a.py", "lang": "py", "body": body}, colour)
    assert text.plain.splitlines()[0] == "code a.py · py"
    assert "l6" in text.plain and "l7" not in text.plain and "… +3 lines (o)" in text.plain


def test_attachment_file_preview_hint_for_text_and_metadata_otherwise():
    text = render.attachment({"type": "file", "ref": "files/01A-r.md", "name": "Review", "mime": "text/markdown"}, colour)
    assert text.plain == "file Review · text/markdown · files/01A-r.md · preview (a)"
    log = render.attachment({"type": "file", "ref": "files/01A-x.log", "name": "x.log", "mime": "application/octet-stream"}, colour)
    assert log.plain.endswith("· preview (a)")
    pdf = render.attachment({"type": "file", "ref": "files/01A-d.pdf", "name": "d.pdf", "mime": "application/pdf", "pages": 3}, colour)
    assert pdf.plain == "file d.pdf · application/pdf · 3 pages · files/01A-d.pdf"
    png = render.attachment({"type": "file", "ref": "files/01A-s.png", "name": "s.png", "mime": "image/png"}, colour)
    assert png.plain == "file s.png · image/png · files/01A-s.png"


def test_attachment_link_http_is_a_link_and_anything_else_is_inert():
    ok = render.attachment({"type": "link", "url": "https://github.com/o/r/pull/1"}, colour)
    assert ok.plain == "link https://github.com/o/r/pull/1"
    assert any(s.style.link == "https://github.com/o/r/pull/1" for s in ok.spans)
    bad = render.attachment({"type": "link", "url": "javascript:alert(1)"}, colour)
    assert bad.plain == "link javascript:alert(1)"
    assert all(not getattr(s.style, "link", None) for s in bad.spans)
    ftp = render.attachment({"type": "link", "href": "ftp://x/y"}, colour)
    assert all(not getattr(s.style, "link", None) for s in ftp.spans)


def test_attachment_tasks_bar_caps_items_and_colours_who():
    items = [{"text": f"t{i}", "who": "dev", "done": i < 3} for i in range(10)]
    text = render.attachment({"type": "tasks", "ref": "plans/p.md", "items": items}, colour)
    lines = text.plain.splitlines()
    assert lines[0].startswith("tasks 3/10 ")
    assert len(lines) == 1 + 8 + 1 and lines[-1] == "… +2 items"
    assert lines[1] == "  [x] t0 dev" and lines[4] == "  [ ] t3 dev"
    assert BLUE in styles(text, "dev")[0]


def test_attachment_clan_line():
    att = {"type": "clan", "status": "proposed", "issue": 2,
           "roles": [{"name": "developer", "writer": True}, {"name": "reviewer"}]}
    assert render.attachment(att, colour).plain == "[clan proposed] issue #2 · developer*, reviewer"


def test_attachment_unknown_type_is_compact_json_never_dropped():
    att = {"type": "blob", "x": [1, 2], "note": "a\x00b"}
    text = render.attachment(att, colour)
    assert text.plain == "blob " + json.dumps({"type": "blob", "x": [1, 2], "note": "ab"}, separators=(",", ":"))
    assert render.attachment("not an object", colour).plain.startswith("? ")


def test_attachment_strings_are_scrubbed_and_never_parsed():
    text = render.attachment({"type": "file", "ref": "files/[red]x", "name": "n\x1bame", "mime": "text/plain"}, colour)
    assert "[red]x" in text.plain and "name" in text.plain
    assert all(not s.style.link for s in text.spans if hasattr(s.style, "link"))
