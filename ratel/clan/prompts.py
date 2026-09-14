"""Recognise a harness permission dialog in a pane's screen dump.

An interactive role that hits a permission prompt is blocked on a modal that
only the operator can answer — the orchestrator cannot type into another
role's pane (that is a permission bypass), and re-dispatching does not help.
The watcher dumps each interactive pane on a slow cadence and asks this module
whether the bottom of the screen is such a dialog; `session._activity_from`
reads the record the watcher leaves and reports `awaiting-operator`.

Headless kinds never reach here: `harness.PROMPT_MARKERS` kills a headless
round on a dialog (stdin is /dev/null, so it could never be answered) and the
round outcome `prompt` reads as `stuck`.

The detector is deliberately conservative: a family matches only when its
header AND its footer both appear in the last `tail` lines, footer after
header. A live dialog sits at the bottom of the viewport; one an agent quoted
in a report has scrolled up. A false positive mislabels a role for one probe
interval; a false negative is today's behaviour.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_CTRL = dict.fromkeys(range(32))            # C0 control characters
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")   # CSI sequences (colour, cursor)
TEXT_CAP = 200

# (family, header, footer). Strings verified against opencode 1.18.30's TUI
# and Claude Code's dialogs (the same ones harness.PROMPT_MARKERS names).
PROMPT_FAMILIES: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    ("opencode",
     re.compile(r"△\s*(Permission required|Always allow)"),
     re.compile(r"Allow once|Allow always|Reject|\bConfirm\b|\bCancel\b")),
    ("claude",
     re.compile(r"Do you want to (proceed|make this edit|create|run|allow)"
                r"|Do you trust the files in this folder"),
     re.compile(r"^\s*[❯>]?\s*1\.\s*Yes")),
)


@dataclass(frozen=True)
class Prompt:
    family: str
    text: str
    kind: str = "permission"


def prompt_text(lines: list[str]) -> str:
    """One sanitised line for the escalation: the header line and up to three
    non-empty lines after it, joined with a middle dot, control characters
    stripped, whitespace collapsed, capped. The screen is agent-influenced
    text, and this string is posted on the bus and shown on the board."""
    kept = []
    for line in lines:
        clean = " ".join(_ANSI.sub("", line).split()).translate(_CTRL)
        if clean:
            kept.append(clean)
        if len(kept) == 4:
            break
    return " · ".join(kept)[:TEXT_CAP]


def detect_prompt(screen: str, tail: int = 15) -> Prompt | None:
    """The dialog at the bottom of `screen`, or None."""
    lines = [_ANSI.sub("", line) for line in screen.splitlines()]
    if tail > 0:
        lines = lines[-tail:]
    for family, header, footer in PROMPT_FAMILIES:
        start = next((i for i, line in enumerate(lines) if header.search(line)), None)
        if start is None:
            continue
        if not any(footer.search(line) for line in lines[start + 1:]):
            continue
        return Prompt(family=family, text=prompt_text(lines[start:]))
    return None
