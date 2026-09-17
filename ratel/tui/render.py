"""Row and attachment builders. Agent text is hostile: it is scrubbed and
appended to `rich.text.Text` as plain strings with explicit spans — never
handed to a markup parser."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone, tzinfo
from typing import Callable

from rich.style import Style
from rich.text import Text

from ..bus import MENTION_RE
from ..schema import valid_timestamp

ColourFor = Callable[[str], str]

FENCE_LINES = 6
TASK_ITEMS = 8
BAR_WIDTH = 10
MUTED = "dim"
CODE = Style(bgcolor="#262D38")
LINK = "underline #4CC2FF"
PREVIEW_MIMES = ("text/markdown", "text/plain")
PREVIEW_SUFFIXES = (".md", ".txt", ".log")

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])")  # CSI sequences and 2-byte escapes
# C0 (except \n and \t), DEL, C1, the Unicode bidi controls that reorder a line (overrides,
# isolates, LRM/RLM/ALM marks) and the Unicode line/paragraph separators.
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x80-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_INLINE = re.compile(
    r"`([^`\n]+)`"                       # 1 code
    r"|\*\*(.+?)\*\*"                    # 2 bold
    r"|~~(.+?)~~"                        # 3 strike
    r"|(?<!\w)\*([^*\n]+?)\*(?!\w)"      # 4 em
    r"|(?<!\w)_([^_\n]+?)_(?!\w)"        # 5 em
    r"|" + MENTION_RE.pattern)           # 6 mention
_FENCE = re.compile(r"^```[^\n]*\n(.*?)(?:\n```[ \t]*$|\Z)", re.M | re.S)
_URL_SAFE = re.compile(r"^[\x21-\x7e]+$")   # printable ASCII, no space: RFC 3986 percent-encodes the rest


def scrub(text) -> str:
    """Drop ANSI escape sequences, C0/C1 controls (CR included), DEL, Unicode
    bidi controls and Unicode line separators; keep newline and tab."""
    return _CONTROLS.sub("", _ANSI.sub("", text if isinstance(text, str) else ""))


def _inline(out: Text, text: str, colour_for: ColourFor) -> None:
    pos = 0
    for m in _INLINE.finditer(text):
        out.append(text[pos:m.start()])
        code, bold, strike, em1, em2, mention = m.groups()
        if code is not None:
            out.append(code, CODE)
        elif bold is not None:
            out.append(bold, "bold")
        elif strike is not None:
            out.append(strike, "strike")
        elif em1 is not None or em2 is not None:
            out.append(em1 if em1 is not None else em2, "italic")
        else:
            out.append("@" + mention, colour_for(mention))
        pos = m.end()
    out.append(text[pos:])


def _code_lines(out: Text, body: str) -> None:
    lines = body.split("\n")
    for line in lines[:FENCE_LINES]:
        out.append(line + "\n", CODE)
    if len(lines) > FENCE_LINES:
        out.append(f"… +{len(lines) - FENCE_LINES} lines (o)\n", MUTED)


def body(text, colour_for: ColourFor) -> Text:
    """Message text with inline markup only; fences show at most six lines."""
    text = scrub(text)
    out = Text()
    pos = 0
    for m in _FENCE.finditer(text):
        _inline(out, text[pos:m.start()], colour_for)
        _code_lines(out, m.group(1))
        pos = m.end()
        if pos < len(text) and text[pos] == "\n":
            pos += 1
    _inline(out, text[pos:], colour_for)
    out.rstrip()
    return out


def clock(ts, tz: tzinfo | None = None) -> str:
    if not valid_timestamp(ts):
        return "--:--"
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz).strftime("%H:%M")


def day(ts, tz: tzinfo | None = None) -> date | None:
    if not valid_timestamp(ts):
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz).date()


def day_label(d: date, today: date | None = None) -> str:
    today = today or datetime.now(timezone.utc).astimezone().date()
    delta = (today - d).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    return f"{d.strftime('%b')} {d.day}"


def header(msg: dict, colour_for: ColourFor, replies: int = 0, tz: tzinfo | None = None) -> Text:
    sender = scrub(msg.get("from"))
    out = Text()
    out.append(f"▌{sender}", Style.parse(colour_for(sender)) + Style(bold=True))
    out.append(" " + clock(msg.get("ts"), tz), MUTED)
    if replies:
        out.append(f"  ↳ {replies} " + ("reply" if replies == 1 else "replies"), MUTED)
    return out


def _scrubbed(value):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {scrub(str(k)): _scrubbed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrubbed(v) for v in value]
    return value


def _name(att: dict) -> str:
    return scrub(att.get("name")) or scrub(att.get("ref")).rsplit("/", 1)[-1] or "file"


def previewable(att) -> bool:
    """A file the preview modal may open as text: by mime or by a text-like name."""
    if not isinstance(att, dict) or att.get("type") != "file" or not isinstance(att.get("ref"), str):
        return False
    names = (scrub(att.get("name")).lower(), scrub(att.get("ref")).lower())
    return (scrub(att.get("mime")).lower() in PREVIEW_MIMES
            or any(n.endswith(PREVIEW_SUFFIXES) for n in names))


def is_http(url) -> bool:
    """An http(s) URL that may carry a terminal hyperlink: printable ASCII only,
    so the visible text can never differ from the OSC 8 click target. scrub keeps
    tab and newline, and zero-width characters (U+200B, U+FEFF, ...) render as
    nothing at all; an IDN URL is the accepted cost and renders as plain text."""
    return (isinstance(url, str) and url.lower().startswith(("http://", "https://"))
            and _URL_SAFE.match(url) is not None)


def _file(att: dict) -> Text:
    parts = [_name(att), scrub(att.get("mime")) or "application/octet-stream"]
    pages = att.get("pages")
    if type(pages) is int and pages >= 0:
        parts.append(f"{pages} " + ("page" if pages == 1 else "pages"))
    parts.append(scrub(att.get("ref")))
    out = Text("file ", MUTED)
    out.append(" · ".join(parts))
    if previewable(att):
        out.append(" · preview (a)", MUTED)
    return out


def _link(att: dict) -> Text:
    url = scrub(att.get("url", att.get("href")))
    out = Text("link ", MUTED)
    if is_http(url):
        out.append(url, Style.parse(LINK) + Style(link=url))
    else:
        out.append(url)
    return out


def _code(att: dict) -> Text:
    out = Text("code", MUTED)
    label = " · ".join(s for s in (scrub(att.get("file")), scrub(att.get("lang"))) if s)
    if label:
        out.append(" " + label)
    out.append("\n")
    _code_lines(out, scrub(att.get("body")))
    out.rstrip()
    return out


def _tasks(att: dict, colour_for: ColourFor) -> Text:
    items = [i for i in att.get("items", []) if isinstance(i, dict)] if isinstance(att.get("items"), list) else []
    done = sum(1 for i in items if i.get("done") is True)
    total = len(items)
    filled = round(BAR_WIDTH * done / total) if total else 0
    out = Text("tasks ", MUTED)
    out.append(f"{done}/{total} ")
    out.append("█" * filled, "#4CC2FF")
    out.append("░" * (BAR_WIDTH - filled), MUTED)
    ref = scrub(att.get("ref"))
    if ref:
        out.append(" " + ref, MUTED)
    for item in items[:TASK_ITEMS]:
        out.append("\n  " + ("[x] " if item.get("done") is True else "[ ] "), MUTED)
        out.append(scrub(item.get("text")))
        who = scrub(item.get("who"))
        if who:
            out.append(" ")
            out.append(who, colour_for(who))
    if total > TASK_ITEMS:
        out.append(f"\n… +{total - TASK_ITEMS} items", MUTED)
    return out


def _clan(att: dict) -> Text:
    out = Text(f"[clan {scrub(att.get('status')) or '?'}]", "bold")
    issue = att.get("issue")
    if type(issue) is int:
        out.append(f" issue #{issue}")
    roles = att.get("roles") if isinstance(att.get("roles"), list) else []
    names = [scrub(r.get("name")) + ("*" if r.get("writer") is True else "") for r in roles if isinstance(r, dict)]
    out.append(" · " + ", ".join(names) if names else "", MUTED if not names else None)
    return out


def _unknown(att) -> Text:
    doc = _scrubbed(att)
    kind = doc.get("type") if isinstance(doc, dict) and isinstance(doc.get("type"), str) else None
    out = Text((kind or "?") + " ", MUTED)
    out.append(json.dumps(doc, separators=(",", ":"), ensure_ascii=False, default=str))
    return out


def attachment(att, colour_for: ColourFor) -> Text:
    """One attachment as text. Unknown shapes become a compact JSON line, never nothing."""
    if not isinstance(att, dict):
        return _unknown(att)
    kind = att.get("type")
    if kind == "code" and isinstance(att.get("body"), str):
        return _code(att)
    if kind == "file" and isinstance(att.get("ref"), str):
        return _file(att)
    if kind == "link" and isinstance(att.get("url", att.get("href")), str):
        return _link(att)
    if kind == "tasks" and isinstance(att.get("items"), list):
        return _tasks(att, colour_for)
    if kind == "clan" and isinstance(att.get("roles"), list):
        return _clan(att)
    return _unknown(att)
