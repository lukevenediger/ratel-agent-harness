"""Harness command adapters; policy and briefing helpers are supplied by the facade."""
from __future__ import annotations

import uuid
from pathlib import Path

from .config import ClanConfig, RoleSpec


def launch_argv(runtime, cfg: ClanConfig, role: str, harness_dir: Path,
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
    prompt = initial_prompt or runtime.default_prompt(cfg, role)
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
        argv = runtime._claude_base_argv(spec, role, harness_dir, home=home)
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
            for tool in runtime.unattended_tools(spec):
                argv += ["--allowedTools", tool]
            return argv, extra
        return argv + [prompt], extra
    if spec.harness == "opencode":
        argv = ["opencode", "--pure", "--model", spec.model]
        if unattended:
            argv.append("--auto")
        return argv + ["--prompt", prompt], {"OPENCODE_CONFIG": str(harness_dir / "opencode.json")}
    argv = runtime._claude_base_argv(spec, role, harness_dir, home=home)
    argv[1:1] = ["--name", role]
    # a fixed session id: the transcript lands at projects/<cwd-slug>/<id>.jsonl
    # where context measurement can find it, instead of under an opaque id the
    # tab would have to guess. Headless claude-p gets one on round 1 only.
    sid = str(uuid.uuid4())
    argv += ["--session-id", sid]
    if unattended:
        argv += ["--permission-mode", "acceptEdits"]
    return argv + [prompt], {"RATEL_SESSION_ID": sid}

