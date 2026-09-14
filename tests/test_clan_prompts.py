"""The permission-dialog detector: screen text in, a Prompt or None out.

Conservative on purpose: header AND footer, both inside the tail of the
screen, footer after header. The screen is agent-influenced text, so the
one string that leaves here is sanitised and capped.
"""
from ratel.clan.prompts import TEXT_CAP, Prompt, detect_prompt, prompt_text

OPENCODE_PERMISSION = """\
  ● Bash  cp /home/x/proj/.env .env

  △ Permission required
    Access external directory /home/x/proj
    - Command argument references external directory /home/x/proj.

    Allow once   Allow always   Reject
"""

OPENCODE_ALWAYS = """\
  △ Always allow
    Access external directory /home/x/proj
    This will be remembered for the project.

    Confirm   Cancel
"""

CLAUDE_PROCEED = """\
  Bash command

    git push origin issue-4

  Do you want to proceed?
  ❯ 1. Yes
    2. Yes, and don't ask again for git push commands
    3. No, and tell Claude what to do differently (esc)
"""

CLAUDE_TRUST = """\
  Do you trust the files in this folder?

  /home/x/proj-wt/4-developer

  ❯ 1. Yes, proceed
    2. No, exit
"""

WORKING = """\
  ● Read  ratel/clan/watch.py
  ● Edit  ratel/clan/watch.py
  Working on the awaiting record now…
  >
"""


def test_opencode_permission_block_is_detected():
    p = detect_prompt(OPENCODE_PERMISSION)
    assert p == Prompt(family="opencode", kind="permission", text=p.text)
    assert p.text.startswith("△ Permission required · Access external directory")


def test_opencode_always_allow_block_is_detected():
    p = detect_prompt(OPENCODE_ALWAYS)
    assert p is not None and p.family == "opencode"
    assert "Confirm" in p.text


def test_claude_proceed_menu_is_detected():
    p = detect_prompt(CLAUDE_PROCEED)
    assert p is not None and p.family == "claude"
    assert p.text.startswith("Do you want to proceed? · ❯ 1. Yes")


def test_claude_trust_dialog_is_detected():
    p = detect_prompt(CLAUDE_TRUST)
    assert p is not None and p.family == "claude"


def test_ordinary_work_is_not_a_prompt():
    assert detect_prompt(WORKING) is None
    assert detect_prompt("") is None


def test_a_header_without_a_footer_is_not_a_prompt():
    """A role narrating "the △ Permission required block went away" has the
    header and no option row: prose, not a dialog."""
    assert detect_prompt("  △ Permission required — cleared it, carrying on\n  >\n") is None
    assert detect_prompt("  I answered 'Do you want to proceed?' myself\n  >\n") is None


def test_a_footer_before_the_header_is_not_a_prompt():
    assert detect_prompt("  Allow once\n  △ Permission required\n") is None


def test_a_quoted_dialog_above_the_tail_is_not_a_prompt():
    """A live dialog sits at the bottom of the viewport; a quoted one has
    scrolled up. Only the last `tail` lines count."""
    screen = OPENCODE_PERMISSION + "\n".join(f"  line {i}" for i in range(20)) + "\n"
    assert detect_prompt(screen) is None
    assert detect_prompt(screen, tail=0) is not None      # the whole screen, on request


def test_prompt_text_is_one_sanitised_capped_line():
    lines = ["△ Permission required\x1b[0m", "", "   Access   external\tdirectory  ",
             "x" * 300, "Allow once", "never reached"]
    text = prompt_text(lines)
    assert "\x1b" not in text and "\t" not in text and "\n" not in text
    assert text.startswith("△ Permission required · Access external directory · xxx")
    assert len(text) == TEXT_CAP
    assert "never reached" not in prompt_text(lines[:5] + ["never reached"])
