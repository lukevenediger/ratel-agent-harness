"""Everything a role needs to start talking: its brief, its config files, its argv.

Config files live in the channel directory, never in a checkout — a clan must
not dirty the diff it is there to produce. The tab command runs this
interpreter by absolute path rather than relying on the zellij server's PATH.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from .config import ClanConfig, ClanPaths, RoleSpec, deep_merge, effort_word, load_models, update_state
from .gitwt import exclude_in_worktree
from .loop import NudgeLoop
from .tools import require_binary

PKG = Path(__file__).parent
PLUGIN_DIR = PKG / "plugin"
SKILL_MD = PLUGIN_DIR / "skills" / "dispatch-issue" / "SKILL.md"
BRIEFS = PKG / "briefs"
ROLE_PROMPT = ("Start: call catch_up, read the pinned plan and the clan table, then wait for your "
               "first dispatch. Act when you are nudged.")


def _opencode_provider(spec: RoleSpec, models: dict[str, dict]) -> dict | None:
    """The OpenCode `provider` block for an opencode-kind role, built only from
    the model's catalog entry. API keys are never written: OpenCode reads
    DEEPSEEK_API_KEY / OPENROUTER_API_KEY from the environment."""
    if not spec.harness.startswith("opencode"):
        return None
    entry = models.get(spec.model) or {}
    provider_id = entry.get("provider")
    if not provider_id:
        return None
    fragment = json.loads(json.dumps(entry.get("opencode", {})))   # deep copy
    npm = fragment.pop("npm", None)
    base_url = fragment.pop("baseURL", None)
    mapped = effort_word(entry, spec.effort)
    if mapped is not None:                       # merged into the fragment's options
        fragment["options"] = deep_merge({"reasoning": {"effort": mapped}},
                                         fragment.get("options", {}))
    block: dict = {"models": {spec.model.split("/", 1)[-1]: fragment}}
    if npm:
        block["npm"] = npm
    if base_url:
        block.setdefault("options", {})["baseURL"] = base_url
    return {provider_id: block}


def clan_table(cfg: ClanConfig) -> str:
    rows = ["| role | harness | model | writer |", "|---|---|---|---|"]
    rows += [f"| {r.name} | {r.harness} | {r.model} | {'yes' if r.writer else 'no'} |"
             for r in cfg.roles.values()]
    return "\n".join(rows)


def render_brief(cfg: ClanConfig, role: str, worktree: str | Path | None,
                 unattended: bool = False) -> str:
    spec = cfg.roles[role]
    text = (BRIEFS / f"{spec.brief}.md").read_text().format(
        channel=cfg.channel, issue=cfg.issue, repo=cfg.repo, role=role,
        branch=f"issue-{cfg.issue}", worktree=worktree or "the repository checkout",
        clan_table=clan_table(cfg))
    if unattended:
        # A headless role cannot poll an env var it may not run `echo` for:
        # the brief is the one place it learns the run is unattended.
        text += ("\n\nThis clan is unattended: nobody is in your tab. Take the catalog "
                 "defaults, do not wait for approval.\n")
    return text


def _agent_env(paths: ClanPaths, cfg: ClanConfig, role: str) -> dict[str, str]:
    return {"AGENT_NAME": role, "CHANNEL": cfg.channel, "RATEL_HOME": str(paths.home)}


def missing_env(cfg: ClanConfig, models: dict[str, dict],
                environ: dict = os.environ) -> dict[str, list[str]]:
    """role → the model's env vars that are not set. Pure; `clan up` refuses
    on a non-empty result before creating any tab or worktree."""
    out: dict[str, list[str]] = {}
    for name, role in cfg.roles.items():
        unset = [e for e in (models.get(role.model) or {}).get("env", [])
                 if not environ.get(e)]
        if unset:
            out[name] = unset
    return out


def write_configs(paths: ClanPaths, cfg: ClanConfig, role: str,
                  worktree: str | Path | None = None, unattended: bool = False) -> Path:
    """Write brief.md, mcp.json, settings.json and opencode.json for one role."""
    spec = cfg.roles[role]
    hd = paths.harness_dir(role)
    hd.mkdir(parents=True, exist_ok=True)
    env = _agent_env(paths, cfg, role)
    brief = hd / "brief.md"
    brief.write_text(render_brief(cfg, role, worktree, unattended=unattended)
                     + f"\nChannel directory: {paths.channel_dir}"
                     + f"\nclan.toml: {paths.clan_toml}\n")

    (hd / "mcp.json").write_text(json.dumps({"mcpServers": {"ratel": {
        "type": "stdio", "command": sys.executable,
        "args": ["-m", "ratel.mcp_server"], "env": env}}}, indent=2))

    inline = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    (hd / "settings.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command",
         "command": f"{inline} {shlex.quote(sys.executable)} -m ratel.unread"}]}]}}, indent=2))

    instructions = [str(brief)]
    if spec.brief == "orchestrator":
        instructions.append(str(SKILL_MD))   # OpenCode has no --plugin-dir
    oc: dict = {"$schema": "https://opencode.ai/config.json",
                "instructions": instructions,
                "mcp": {"ratel": {"type": "local",
                                      "command": [sys.executable, "-m", "ratel.mcp_server"],
                                      "enabled": True, "environment": env}}}
    provider = _opencode_provider(spec, load_models(paths.home))
    if provider:
        oc["provider"] = provider
    # A role must read its own pins and files without blocking on a modal: the
    # clan's channel dir, the role's worktree and the checkout's .git are
    # pre-allowed under external_directory and nothing else is loosened.
    # Object syntax, last matching rule wins (OpenCode 1.18). realpath can
    # differ from the given path (/tmp vs /private/tmp on macOS) — allow both.
    # NOT a containment boundary: bash is unconditional, so an unattended clan
    # role runs arbitrary code as the operator — sandbox repo and a scoped
    # token only. See docs/DECISIONS.md, Decision 28.
    def _patterns(base: str) -> list[str]:
        real = os.path.realpath(base)
        return [base, real] if real != base else [base]

    allowed = [f"{d}/**" for d in
               _patterns(str(paths.channel_dir))
               + (_patterns(str(worktree)) if worktree else [])
               + _patterns(str(Path(cfg.checkout) / ".git"))]
    if unattended:
        oc["permission"] = {"bash": "allow", "edit": "allow", "webfetch": "allow",
                            "question": "deny", "task": "deny",
                            "external_directory": {"*": "deny", **{p: "allow" for p in allowed}}}
    else:
        oc["permission"] = {"external_directory": {p: "allow" for p in allowed}}
    (hd / "opencode.json").write_text(json.dumps(oc, indent=2))
    if spec.playbook:
        (hd / "playbook.toml").write_text(spec.playbook)
    return hd


def claude_config_file() -> Path:
    """Claude Code's config; CLAUDE_CONFIG_DIR moves the whole dir including this file."""
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(d).expanduser() / ".claude.json" if d else Path.home() / ".claude.json"


def mark_trusted(path: str | Path) -> bool:
    """Record Claude's workspace trust for one project path, so an unattended
    headless round does not die on the first-run trust dialog (nothing can
    answer it: the round's stdin is /dev/null). Read-modify-write of one key in
    `projects[<abs path>]`; the file holds account data, so its contents are
    never logged or printed. Attended clans do not call this — the human
    accepts the dialog on first attach.

    Returns whether a write happened.
    """
    return bool(_update_claude_config(lambda data: _trust_mutator(data, str(path))))


def _trust_mutator(data: dict, path: str) -> bool:
    projects = data.setdefault("projects", {})
    entry = projects.setdefault(path, {})
    if entry.get("hasTrustDialogAccepted") is True:
        return False
    entry["hasTrustDialogAccepted"] = True
    return True


def untrust(prefix: str | Path) -> int:
    """Remove every `projects` key under `prefix` (the trust entries a clan run
    added) from the live Claude config. Atomic, flock-protected against other
    ratel writers, contents never printed. Returns how many were removed."""
    return _update_claude_config(lambda data: _untrust_mutator(data, str(prefix)))


def _untrust_mutator(data: dict, prefix: str) -> int:
    projects = data.get("projects", {})
    # the separator keeps /tmp/foo from matching a sibling /tmp/foobar
    stale = [k for k in projects
             if k.startswith(prefix.rstrip(os.sep) + os.sep) or k == prefix]
    for k in stale:
        del projects[k]
    return len(stale)


def _update_claude_config(mutator) -> object:
    """Flocked read-modify-write of the live ~/.claude.json; atomic replace.
    Returns whatever mutator returns, or None when the file is unparsable
    (never clobber a config we cannot parse). Claude Code itself takes no
    lock, so a write racing Claude's own can still be lost — accepted."""
    f = claude_config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.touch(exist_ok=True)
    # os.replace swaps the inode, so the lock must live on a stable sidecar
    # file, not on the config itself — and the config is re-read after locking.
    # The whole RMW, including the no-change check, happens under the lock: a
    # fast path outside it would let two racing callers both see "no change
    # yet" and one would skip its write entirely.
    with open(f"{f}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            raw = f.read_text()
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                return None
            before = json.dumps(data, sort_keys=True)
            result = mutator(data)
            if json.dumps(data, sort_keys=True) == before:
                return result                        # no change: skip the write
            fd, tmp = tempfile.mkstemp(dir=str(f.parent))
            with os.fdopen(fd, "w") as out:
                json.dump(data, out, indent=2)
            os.replace(tmp, f)
            return result
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def extract_opencode_session(output: str) -> str | None:
    """The session id from `opencode run --format json` event output. Pinned by
    tests/fixtures/opencode-run-session.jsonl, captured from the real binary —
    a shape change must fail loudly, never fall back to a global `-c`."""
    m = re.search(r'"sessionID":"(ses_[^"]+)"', output)
    return m.group(1) if m else None


def _ps_lstart(pid: int) -> str:
    """The process's start time, as `ps` prints it — a pid-recycling guard."""
    try:
        return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""          # ps unavailable or a stubbed Popen: ledger verification
                           # will simply not match, so the round is never killed


def terminate_round(pid_file: Path) -> bool:
    """Kill the round group a `round.pid` ledger names — but only if the ledger
    still describes the process that is actually running there (a round that
    died before unlinking, or pid reuse, must not make `clan down` kill a
    bystander). Returns whether a kill happened."""
    try:
        rec = json.loads(pid_file.read_text())
        pid, pgid, started = rec["pid"], rec["pgid"], rec["started"]
        if not (isinstance(pid, int) and isinstance(pgid, int) and started):
            return False
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return False
    if _ps_lstart(pid) != started:
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return False
    pid_file.unlink(missing_ok=True)
    return True


def _claude_base_argv(spec: RoleSpec, role: str, harness_dir: Path,
                      home: Path | None = None) -> list[str]:
    """The flags both claude kinds share; [1] says `-p` or `--name <role>`.

    --plugin-dir (the dispatch-issue skill) arms only the orchestrator brief:
    a probe running on a claude kind must not load it."""
    argv = ["claude", "--model", spec.model,
            "--mcp-config", str(harness_dir / "mcp.json"), "--strict-mcp-config",
            "--settings", str(harness_dir / "settings.json"),
            "--append-system-prompt", (harness_dir / "brief.md").read_text()]
    models = load_models(home) if home is not None else load_models(_home_of(harness_dir))
    mapped = effort_word(models.get(spec.model) or {}, spec.effort)
    if mapped:
        argv += ["--effort", mapped]
    if spec.brief == "orchestrator":
        argv += ["--plugin-dir", str(PLUGIN_DIR)]
    return argv


def _home_of(harness_dir: Path) -> Path:
    """<home> = harness_dir's channel's home: <home>/channels/<chan>/harness/<role>."""
    return harness_dir.parent.parent.parent.parent


def default_prompt(cfg: ClanConfig, role: str) -> str:
    # keyed on the brief like every other arming decision: a probe named
    # "orchestrator" must not be handed the dispatch command in words
    if cfg.roles[role].brief != "orchestrator":
        return ROLE_PROMPT
    if cfg.roles[role].harness.startswith("claude"):
        return f"/clan:dispatch-issue {cfg.issue}"
    return f"Follow the dispatch-issue skill for issue {cfg.issue}."


HEADLESS_KINDS = ("claude-p", "opencode-run")
# The allow list for an unattended headless claude role, narrowed to the
# subcommands the dispatch-issue skill actually runs. Measured against claude
# 2.1.263: the `mcp__ratel__*` wildcard grants the whole ratel MCP
# server, and --allowedTools accumulates across repeated flags. Narrowed is
# not a containment boundary: `git push <url>` and `gh … -R <repo>` still
# reach beyond the clan's repo with the operator's credentials — run
# unattended clans against a sandbox repo with a scoped token
# (docs/DECISIONS.md, Decision 28). Never --dangerously-skip-permissions.
# git verbs a headless writer needs to build its branch; with edit + commit +
# pytest this is arbitrary execution by construction — accepted residual risk
# (docs/DECISIONS.md, Decision 28)
WRITER_GIT = ("Bash(git add:*)", "Bash(git commit:*)", "Bash(git checkout:*)",
              "Bash(git switch:*)", "Bash(git restore:*)", "Bash(git rm:*)",
              "Bash(git mv:*)")
# what a detached reviewer may run: read-only git only
GIT_READ_ONLY = ("Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
                 "Bash(git show:*)", "Bash(git fetch:*)")
# every headless claude role must be able to run the repo's own checks
TEST_RUNNERS = ("Bash(python -m pytest:*)", "Bash(python3 -m pytest:*)",
                "Bash(pytest:*)", "Bash(uv run pytest:*)")

MCP_RATEL = ("mcp__ratel__*",          # measured on claude 2.1.263: grants the
                 "Bash(ratel:*)")          # whole ratel MCP server... and the CLI
UNATTENDED_TOOLS = MCP_RATEL + (
    # measured on claude 2.1.263: --strict-mcp-config loads no other server
    "Bash(gh issue:*)", "Bash(gh pr:*)", "Bash(gh label:*)", "Bash(gh api graphql:*)",
    "Bash(git merge:*)", "Bash(git push:*)", "Bash(uv run pytest:*)",
    "Bash(npx skills:*)") + GIT_READ_ONLY


def unattended_tools(role: RoleSpec) -> tuple[str, ...]:
    """The allow list for one headless claude role, by what the role does.
    Keyed on the brief: a probe — whatever its role name — must never hold the
    ratel CLI (`Bash(ratel:*)`) nor the test runners: pytest executes
    conftest.py at collection, so `uv run pytest` over a probe-writable tree
    is arbitrary code (security review of issue #6). Only the channel MCP
    server and read-only git remain."""
    if role.brief == "probe":
        return ("mcp__ratel__*",) + GIT_READ_ONLY
    if role.brief == "orchestrator":
        return UNATTENDED_TOOLS
    if role.writer:
        return MCP_RATEL + GIT_READ_ONLY + WRITER_GIT + ("Bash(git push:*)",) + TEST_RUNNERS
    return MCP_RATEL + GIT_READ_ONLY + TEST_RUNNERS
ROUND_OUTPUT_LINES = 40
DEFAULT_ROUND_TIMEOUT_S = 3600
PUMP_JOIN_TIMEOUT_S = 10.0
# An interactive prompt in a headless round can never be answered (stdin is
# /dev/null) — it would sit until the timeout. Any of these in a round's
# stdout is a configuration error: kill the round and record why.
PROMPT_MARKERS = ("Do you trust the files in this folder",
                  "trust this folder",
                  "No, exit",
                  "This command requires approval",
                  "Do you want to proceed?")
# A marker only counts when the round has gone QUIET on it (no further output
# for PROMPT_QUIET_S and the marker still in the last line) — that is what a
# real dialog looks like. A marker quoted mid-stream in an agent's report is
# prose; killing on it would murder healthy rounds.
PROMPT_QUIET_S = 5.0
# Dialogs appear at process start; a marker late in a round is the model
# quoting its own traffic. Only markers inside this window count.
PROMPT_WINDOW_S = 30.0
AUTH_MARKERS = ("Not logged in",)               # a headless round cannot /login either


def launch_argv(cfg: ClanConfig, role: str, harness_dir: Path,
                initial_prompt: str | None = None, unattended: bool = False,
                round_n: int = 1, channel_dir: Path | None = None,
                project_dir: Path | None = None, session_id: str | None = None,
                home: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """(argv, extra env). The prompt is last — except where a trailing variadic
    flag would eat it: an unattended `claude-p` puts the prompt BEFORE its
    --allowedTools list (measured: `--allowedTools` is variadic and swallows a
    trailing prompt).

    Headless kinds take a `round_n`: the kickoff is round 1 and every nudge
    gets its own process, resuming the previous conversation only from round 2
    on (a fresh cwd has no conversation to continue).
    """
    spec: RoleSpec = cfg.roles[role]
    prompt = initial_prompt or default_prompt(cfg, role)
    if spec.harness == "fake":
        # Tier 1: a scripted agent driven by the same nudges as a real one.
        return (["ratel-fake-harness", "--playbook", str(harness_dir / "playbook.toml"),
                 "--log", str(harness_dir / "nudges.log")], {})
    if spec.harness == "opencode-run":
        # --dir pins the project: `-c` continues "the last session" GLOBALLY, so
        # round 2+ resumes this role's own session by id (captured from round
        # 1's --format json output) instead.
        argv = ["opencode", "run", "--dir", str(project_dir or cfg.checkout),
                "--pure", "--auto", "--format", "json", "--model", spec.model]
        if round_n > 1 and session_id:
            argv += ["-s", session_id]
        elif round_n > 1:
            argv.append("-c")            # no id captured: best effort, never silently global
        return argv + [prompt], {"OPENCODE_CONFIG": str(harness_dir / "opencode.json")}
    if spec.harness == "claude-p":
        argv = _claude_base_argv(spec, role, harness_dir, home=home)
        argv[1:1] = ["-p"]
        extra: dict[str, str] = {}
        if round_n == 1:
            # pin the headless transcript the same way the interactive kind does:
            # a fixed file `projects/<cwd-slug>/<id>.jsonl` measurement can read.
            # Round 2+ resumes with --continue, which picks this same session.
            sid = str(uuid.uuid4())
            argv += ["--session-id", sid]
            extra = {"RATEL_SESSION_ID": sid}
        elif round_n > 1:
            argv.append("--continue")
        if unattended:
            argv.append("--permission-mode")
            argv.append("acceptEdits")
            argv.append(prompt)              # BEFORE the variadic flag: see the docstring
            # The channel dir is outside cwd and Claude confines Read/Write/Edit
            # to its working directories — a permission rule alone is not enough
            # (/add-dir is unavailable in -p), so grant the dir itself, variadic
            # flags after the prompt.
            channel_dir = channel_dir or harness_dir.parent.parent
            # Geometry, not rules: with acceptEdits, everything inside an
            # --add-dir tree is auto-accept territory and per-path Edit rules
            # never enter the decision (measured on claude 2.1.263). So add-dir
            # only the trees a role may write. clan.state.json sits at the
            # channel root, outside every add-dir, for every role. The channel
            # ROOT is deliberately excluded for every role, including the
            # orchestrator: a role that cannot find a file there needs its
            # brief corrected, not the root granted — granting the root
            # re-opens the whole control plane (state, peer harness dirs).
            clan_dir = channel_dir / "clan"
            add_dirs = [channel_dir / "plans", channel_dir / "files", harness_dir]
            if spec.brief == "orchestrator":         # owns clan.toml + the plans
                add_dirs.append(clan_dir)
            for d in add_dirs:
                argv += ["--add-dir", str(d)]
            # Per-path Read()/Edit() rules are deleted, deliberately: under
            # acceptEdits the --add-dir set IS the file boundary and the rules
            # are inert under both modes (measured on claude 2.1.263). Bash(…)
            # and mcp__… rules are NOT inert — they are the command/MCP
            # boundary and do narrow (see unattended_tools).
            argv += ["--allowedTools", "Bash(ls:*)"]
            for tool in unattended_tools(spec):
                argv += ["--allowedTools", tool]
            return argv, extra
        return argv + [prompt], extra
    if spec.harness == "opencode":
        argv = ["opencode", "--pure", "--model", spec.model]
        if unattended:
            argv.append("--auto")
        return argv + ["--prompt", prompt], {"OPENCODE_CONFIG": str(harness_dir / "opencode.json")}
    argv = _claude_base_argv(spec, role, harness_dir, home=home)
    argv[1:1] = ["--name", role]
    # a fixed session id: the transcript lands at projects/<cwd-slug>/<id>.jsonl
    # where context measurement can find it, instead of under an opaque id the
    # tab would have to guess. Headless claude-p gets one on round 1 only.
    sid = str(uuid.uuid4())
    argv += ["--session-id", sid]
    if unattended:
        argv += ["--permission-mode", "acceptEdits"]
    return argv + [prompt], {"RATEL_SESSION_ID": sid}


def install_skills(worktree: str | Path, specs: list[str]) -> list[str]:
    """Registry skills for one role, kept out of the clan's diff. Specs come
    from clan.toml, validated by proposal.py's SKILL_SPEC_RE — that regex is
    the only argv guard (the skills CLI ignores `--`, measured).

    Returns the specs whose install exited non-zero. The CLI runs with
    check=False, so that code is the only signal that an offline machine, a
    missing `npx` or a bad spec left the role without a skill it was promised.
    A missing `npx` is reported the same way rather than raising."""
    if not specs:
        return []
    if shutil.which("npx") is None:
        print("ratel clan: 'npx' is not on PATH — no skills were installed: "
              + ", ".join(specs), file=sys.stderr)
        return list(specs)
    failed = []
    for spec in specs:
        p = subprocess.run(["npx", "skills", "add", spec, "-a", "claude,opencode", "-y"],
                           cwd=str(worktree), check=False)
        if p.returncode != 0:
            failed.append(spec)
    exclude_in_worktree(worktree, [".agents/", ".claude/skills/"])
    return failed


BINARY_FOR_HARNESS = {"claude": "claude", "claude-p": "claude",
                      "opencode": "opencode", "opencode-run": "opencode"}


def launch(paths: ClanPaths, cfg: ClanConfig, role: str, worktree: str | Path | None = None,
           unattended: bool = False, stdin: TextIO | None = None) -> None:
    """Replace this process with the role's harness, with the agent env set explicitly.

    The tab inherits whoever ran `clan new`, so AGENT_NAME and CHANNEL are set
    here rather than trusted. Headless kinds do not exec: they run one round
    per nudge line and log each round to rounds.jsonl.
    """
    hd = paths.harness_dir(role)
    env = {**os.environ, **_agent_env(paths, cfg, role)}
    if unattended:
        env["CLAN_UNATTENDED"] = "1"
    kind = cfg.roles[role].harness
    binary = BINARY_FOR_HARNESS.get(kind)
    if binary:                               # before any side effect, so a miss is clean
        require_binary(binary, f"the {kind} harness for role {role!r}")
    record_launched(paths, role)             # the measurement launch gate, every kind
    if cfg.roles[role].harness in HEADLESS_KINDS:
        _run_headless(cfg, role, hd, paths.channel_dir, env, unattended, stdin, paths,
                      project_dir=Path(worktree) if worktree else Path(cfg.checkout))
        return
    argv, extra = launch_argv(cfg, role, hd, unattended=unattended,
                              channel_dir=paths.channel_dir, home=paths.home)
    sid = extra.pop("RATEL_SESSION_ID", None)
    if sid:
        record_session(paths, role, sid)
    os.execvpe(argv[0], argv, {**env, **extra})


def record_session(paths: ClanPaths, role: str, session_id: str) -> None:
    """Pin the tab's claude session id and launch time so context measurement
    reads exactly this session's transcript, never a sibling's."""
    update_state(paths, lambda s: (s.setdefault("tabs", {}).setdefault(role, {}).update(
        {"session_id": session_id,
         "launched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})))


def record_launched(paths: ClanPaths, role: str) -> None:
    """The launch half alone — opencode measurement matches on directory and
    only needs the launch gate."""
    update_state(paths, lambda s: s.setdefault("tabs", {}).setdefault(role, {}).update(
        {"launched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}))


def record_round_session(paths: ClanPaths, role: str, extra: dict) -> None:
    """A headless round's argv carried a pinned `--session-id` (round 1, or
    the first round after a reset): record it so measurement reads that file."""
    sid = extra.get("RATEL_SESSION_ID")
    if sid:
        record_session(paths, role, sid)
    else:
        record_launched(paths, role)


def _run_headless(cfg: ClanConfig, role: str, hd: Path, channel_dir: Path,
                  env: dict[str, str], unattended: bool, stdin: TextIO | None,
                  paths: ClanPaths, project_dir: Path | None = None) -> None:
    """One subprocess per round: the kickoff, then one per nudge line.

    The child gets stdin from /dev/null — it must never see the pane tty the
    watcher types nudges into, or it eats the next round's line. stdout is
    pumped through to the pane as it arrives (the pane is the agent's face)
    while being buffered for the rounds.jsonl record; CLAN_ROUND_TIMEOUT
    seconds (default 3600) kill a wedged round and the loop moves on.
    """
    rounds_path = hd / "rounds.jsonl"
    session_file = hd / "opencode-session"
    timeout_s = float(os.environ.get("CLAN_ROUND_TIMEOUT") or DEFAULT_ROUND_TIMEOUT_S)
    state = {"n": 0, "session_id": session_file.read_text().strip()
             if session_file.exists() else None}
    current: dict = {}

    def _kill_current() -> None:
        pid = current.get("pid")
        if pid:
            try:                                 # the round group, not just the child
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def _on_signal(signum, _frame) -> None:
        # `clan down` kills the pane; the round, in its own session, would
        # otherwise outlive it. Take the group with us.
        _kill_current()
        sys.exit(f"ratel clan launch: round killed by signal {signum}")

    for sig in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _on_signal)

    def run_round(prompt: str) -> None:
        reset = False
        if (hd / "reset").exists():          # `clan checkpoint` left a rewind marker
            (hd / "reset").unlink()
            reset = True
            state["n"] = 0                   # this round launches as round 1: no --continue / -s
            state["session_id"] = None
            session_file.unlink(missing_ok=True)
        state["n"] += 1
        round_start = time.monotonic()
        logged: list[str] = []
        out_lines: list[str] = []
        err_lines: list[str] = []
        timed_out = False
        error: str | None = None
        prompt_line: list[str] = []                 # first interactive-prompt line, if any
        prompt_hit = threading.Event()
        auth_hit = threading.Event()
        candidate: list[str] = []                   # last marker line, cleared by any output
        last_output = {"t": time.monotonic()}

        def pump(stream, sink) -> None:
            for line in stream:
                last_output["t"] = time.monotonic()
                sink(line)
                if time.monotonic() - round_start <= PROMPT_WINDOW_S:
                    if any(m in line for m in PROMPT_MARKERS):
                        if not candidate:           # keep the FIRST marker line: the question
                            candidate[:] = [line.strip()]
                    else:
                        candidate.clear()           # prose quoting a marker moves on
                if not auth_hit.is_set() and any(m in line for m in AUTH_MARKERS):
                    auth_hit.set()
        try:
            argv, extra = launch_argv(cfg, role, hd, initial_prompt=prompt,
                                      unattended=unattended, round_n=state["n"],
                                      channel_dir=channel_dir, project_dir=project_dir,
                                      session_id=state.get("session_id"),
                                      home=paths.home)
            if state["n"] == 1:                     # a session begins: round 1, or the reset round
                record_round_session(paths, role, extra)   # (round 1's --session-id, or the gate)
            logged = list(argv)                     # prompt-free, wherever it sits
            logged.pop(argv.index(prompt))
            if "--append-system-prompt" in logged:  # the brief as its path, not its text
                logged[logged.index("--append-system-prompt") + 1] = str(hd / "brief.md")
            p = subprocess.Popen(argv, env={**env, **extra}, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 start_new_session=True)
            current["pid"] = p.pid               # `clan down` finds us here if the pane dies
            try:
                pgid = os.getpgid(p.pid)         # start_new_session: pgid == pid
            except ProcessLookupError:
                pgid = p.pid
            (hd / "round.pid").write_text(json.dumps(
                {"pid": p.pid, "pgid": pgid, "started": _ps_lstart(p.pid)}))
            t_out = threading.Thread(
                target=pump, args=(p.stdout, lambda line: (out_lines.append(line),
                                                           sys.stdout.write(line), sys.stdout.flush())))
            t_err = threading.Thread(
                target=pump, args=(p.stderr, lambda line: (err_lines.append(line),
                                                           sys.stderr.write(line))))
            t_out.start()
            t_err.start()
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    p.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if candidate and time.monotonic() - last_output["t"] >= PROMPT_QUIET_S:
                    prompt_line.append(candidate[0])          # quiet on the marker: a real dialog
                    prompt_hit.set()
                if prompt_hit.is_set() or auth_hit.is_set():   # fail fast, say why
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
            if timed_out or prompt_hit.is_set() or auth_hit.is_set():
                try:                                # kill the whole group: a grandchild holding
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)   # the pipe must not wedge us
                except (ProcessLookupError, PermissionError, AttributeError):
                    p.kill()
            p.wait()
            for t in (t_out, t_err):                # bounded: a surviving grandchild cannot hang us
                t.join(timeout=PUMP_JOIN_TIMEOUT_S)
        except Exception as exc:                    # even the kickoff round must not kill the tab
            traceback.print_exc(file=sys.stderr)
            error = f"{type(exc).__name__}: {exc}"
        if os.environ.get("CLAN_ROUND_LOG") == "full":
            output = "".join(out_lines)
        else:
            output = "".join(out_lines[-ROUND_OUTPUT_LINES:])
        if prompt_hit.is_set() and not error:
            error = f"interactive prompt the round cannot answer: {prompt_line[0]}"
        if auth_hit.is_set() and not error:
            error = "the harness is not authenticated — run its login once as the operator"
        current.pop("pid", None)
        (hd / "round.pid").unlink(missing_ok=True)
        session_id = extract_opencode_session("".join(out_lines))
        if session_id:                           # opencode: resume THIS session, never a global -c
            state["session_id"] = session_id
            session_file.write_text(session_id)
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")
                       .replace("+00:00", "Z"),
                  "round": state["n"], "prompt": prompt, "argv": logged,
                  "returncode": "prompt" if prompt_hit.is_set() else
                        "auth" if auth_hit.is_set() else
                        "timeout" if timed_out else
                        "error" if error else p.returncode,
                  "output": error or output, "reset": reset}
        with open(rounds_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    run_round(default_prompt(cfg, role))     # the kickoff runs before the loop reads stdin
    NudgeLoop(run_round, stdin=stdin, log=hd / "nudges.log").run()
