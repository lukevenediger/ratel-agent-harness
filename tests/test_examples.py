import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def load(name):
    return json.loads((EXAMPLES / name).read_text())


def test_every_example_json_parses():
    files = sorted(EXAMPLES.glob("*.json"))
    assert files, "no example json files"
    for p in files:
        json.loads(p.read_text())


def test_mcp_json_points_at_the_console_script():
    server = load("mcp.json")["mcpServers"]["ratel"]
    assert server["command"] == "ratel-mcp"
    assert server["type"] == "stdio"
    assert server["env"]["AGENT_NAME"] and server["env"]["CHANNEL"]


def test_claude_settings_is_hooks_only():
    cfg = load("claude-settings.json")
    assert "mcpServers" not in cfg, "mcpServers belongs in .mcp.json, not settings.json"
    (entry,) = cfg["hooks"]["UserPromptSubmit"]
    (hook,) = entry["hooks"]
    assert hook["type"] == "command" and "ratel-unread" in hook["command"]


def test_harness_doc_covers_install_and_config_location():
    doc = (ROOT / "docs" / "harness-setup.md").read_text()
    assert "uv tool install" in doc
    assert ".mcp.json" in doc


def test_harness_doc_is_the_clan_quick_start():
    doc = (ROOT / "docs" / "harness-setup.md").read_text()
    assert "clan new" in doc, "harness-setup.md must document the clan quick start"
    assert "CLAN_SANDBOX_TOKEN" in doc, "Tier 2 token requirement must be documented"


def test_cli_contract_exists_and_names_every_subcommand():
    doc = (ROOT / "docs" / "cli-contract.md").read_text()
    clan_cli = (ROOT / "ratel" / "clan" / "cli.py").read_text()
    subcommands = re.findall(r'cs\.add_parser\("(\w+)"', clan_cli)
    assert subcommands, "no clan subcommands found in clan/cli.py"
    for sub in subcommands:
        # the doc may append usage (e.g. `clan launch <role>`), so match the
        # opening backtick + subcommand only
        assert f"`clan {sub}" in doc, f"cli-contract.md does not document `clan {sub}`"
    main_cli = (ROOT / "ratel" / "cli.py").read_text()
    channel_cmds = re.findall(r'sub\.add_parser\("([\w-]+)"', main_cli)
    for cmd in channel_cmds:                           # every channel command, boundary-aware:
        assert re.search(r"`" + re.escape(cmd) + r"(?![\w-])", doc), \
            f"cli-contract.md does not document `{cmd}`"


def test_architecture_and_decisions_carry_the_clan_contract():
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    assert "rounds.jsonl` can contain secrets and is never attached to the channel or a PR" in arch
    assert "clan/clan.toml" in arch
    decisions = (ROOT / "docs" / "DECISIONS.md").read_text()
    for n in range(22, 29):
        assert f"**{n}." in decisions, f"DECISIONS.md is missing entry {n}"
    skill = ROOT / "ratel" / "clan" / "plugin" / "skills" / "dispatch-issue" / "SKILL.md"
    assert skill.exists(), "the plugin skill path the docs cite does not exist"
