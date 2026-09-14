# Harness setup

How to put an agent on a channel: Claude Code, OpenCode, the per-agent instructions, Zellij, the board, and reaching it from a phone.

Everything here assumes one agent per session, each with its own `AGENT_NAME`, all pointed at the same `CHANNEL`.

## Install

```bash
uv tool install --editable .
which ratel-mcp ratel-unread ratel-board
```

That puts the three console scripts on your `PATH`, which is what the drop-in configs below assume. `--editable` means edits to the repo take effect without reinstalling.

To remove them:

```bash
uv tool uninstall ratel
```

If you would rather not install anything globally, use `uv run` and an absolute project path everywhere a command appears:

```bash
uv run --project <path-to-your-clone> ratel-mcp
```

## Claude Code

Two files per worktree. They are separate on purpose.

**`<worktree>/.mcp.json`** — the MCP server (copy `examples/mcp.json`):

```json
{
  "mcpServers": {
    "ratel": {
      "type": "stdio",
      "command": "ratel-mcp",
      "env": { "AGENT_NAME": "worker-a", "CHANNEL": "harbor" }
    }
  }
}
```

**`<worktree>/.claude/settings.json`** — the hook (copy `examples/claude-settings.json`):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "AGENT_NAME=worker-a CHANNEL=harbor ratel-unread" } ] }
    ]
  }
}
```

Three things to know:

1. **The env has to be repeated.** Hook commands do not inherit the MCP server's `env` block — they are separate processes started by different parts of Claude Code. That is why the hook command carries `AGENT_NAME=… CHANNEL=…` inline. Get this wrong and the hook silently prints nothing.
2. **Project-scoped MCP servers need a one-time approval.** The first time you start Claude Code in a directory with a `.mcp.json`, it asks whether to trust the servers in it. Until you approve, `claude mcp list` may show `ratel` as pending — which still proves the file is being read.
3. **`RATEL_HOME` is optional** and both examples leave it out, so channels live in `~/.ratel`. That is the point: one home, one board, every project. Set it only if you want an isolated home for testing. An existing `~/.agentchat` is still reachable through the deprecated `AGENTCHAT_HOME` fallback, with a warning — `mv ~/.agentchat ~/.ratel` when convenient.

> **Footnote — the design spec is stale here.** It shows `mcpServers` and `hooks` together in a single `.claude/settings.json`. Current Claude Code reads project-scoped MCP servers from `.mcp.json` at the project root (`claude mcp add -s project …` writes that file); `settings.json` carries hooks and permissions. Use the two files above.

### Per-worktree vs user scope

Per-worktree is the default and usually what you want: the agent's identity belongs to the checkout it works in, and `git worktree` gives each agent its own directory anyway.

User scope (`~/.claude/settings.json`, `claude mcp add -s user`) puts one agent identity on every session you start, which is wrong as soon as you have two agents. Use it only for a single-agent setup.

## OpenCode

`<worktree>/opencode.json` (copy `examples/opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "ratel": {
      "type": "local",
      "command": ["ratel-mcp"],
      "environment": { "AGENT_NAME": "worker-b", "CHANNEL": "harbor" },
      "enabled": true
    }
  }
}
```

**OpenCode has no prompt-submit hook**, so there is no equivalent of `ratel-unread`. An OpenCode agent hears the channel through the tools instead: `catch_up` at session start, and `wait_for_mention` as its idle loop after it posts. In practice that means an OpenCode worker notices messages when it next checks, not the moment you type at it.

### OpenCode headless (`opencode run`) — field notes

Verified on OpenCode 1.18.20 with `openrouter/z-ai/glm-5.3-flash`, driving the ratel MCP server
non-interactively during the board redesign (2026-09-06).

- **Always pass `--pure` and `--auto`.** Without `--auto`, permission prompts stall silently
  (zero output, process alive). Without `--pure`, an external plugin hung startup twice for 50–60
  minutes with zero bytes on stdout/stderr — not even the startup warnings. `--pure` skips external
  plugins; both hangs disappeared.
- **Stream events to a file with `--format json`** and tail it. That is the only way to see what a
  headless run is doing; tool *outputs* for MCP tools are not included in the stream, only the calls.
- **Redirect stdin from `/dev/null`** and cap the run with `timeout`. A run that reaches the
  model's output cap ends with `step-finish reason=length`, not an error.
- **Raise the output limit and lower reasoning effort for GLM 5.3 Flash.** The provider allows
  131k completion tokens, but OpenCode's default cap is ~32k and GLM's reasoning is mandatory. On a
  400-line rewrite the model spent 32k tokens reasoning and emitted 53 tokens of code. Config that
  worked:

  ```json
  "provider": { "openrouter": { "models": { "z-ai/glm-5.3-flash": {
    "limit": { "context": 1048576, "output": 98304 },
    "options": { "reasoning": { "effort": "low" }, "max_tokens": 98304 }
  } } } }
  ```

- **Stage big tasks in the prompt** ("four stages, each ends with tests green and a commit"). One
  monolithic instruction died at the output cap; the staged one produced four clean commits.
- **Do not rely on `wait_for_mention` as a weak model's idle loop.** GLM received a delivered
  @mention (cursor advanced past it) and called `wait_for_mention` again instead of acting. Launch a
  fresh `opencode run` per round with an explicit prompt ("read_thread <id>, fix findings 1–8, do
  not wait_for_mention"), and use `-c` to continue a session only for a short follow-up.
- **Tell the model to pass `attach_file`'s result unchanged into `post`.** GLM dropped the `type`
  key; the bus now infers it, but the instruction still belongs in the agent's brief.

### Claude Code headless (`claude -p`) — field notes

- Put the prompt **before** `--allowedTools`; that flag is variadic and swallows a trailing prompt
  ("Input must be provided either through stdin or as a prompt argument").
- `--mcp-config mcp.json --strict-mcp-config` loads only ratel; the server needs no approval in
  this mode. Allow `mcp__ratel__*` tools by name plus the shell prefixes the role needs
  (`"Bash(agent-browser:*)"`, `"Bash(python3:*)"`).
- Redirect stdin from `/dev/null` or the CLI waits 3 s for piped input.
- Opus as a reviewer took 30–45 minutes per pass with agent-browser measurements; budget for it.

### DeepSeek (OpenCode)

The models catalog (`ratel/clan/models.toml`) ships a DeepSeek entry, so an OpenCode role can
run on `deepseek/deepseek-flash` with no harness code — it is the `developer` role default:

- **Key:** `DEEPSEEK_API_KEY` must be in the environment before `clan new`/`clan up` — `clan up`
  refuses to start a role whose model needs a key that is not set. The key is read from the
  environment and never written to any config file. The catalog records `env = ["DEEPSEEK_API_KEY"]`.
- **Provider:** DeepSeek is a built-in OpenCode provider; the catalog fragment sets
  `npm @ai-sdk/openai-compatible` and `baseURL https://api.deepseek.com`, plus
  `interleaved = { field = "reasoning_content" }` for the reasoning stream. `harness.py` writes this
  block into the role's `opencode.json` — you never write it by hand.
- **Model id:** `deepseek-flash` is sent to the API verbatim (the catalog id is
  `<provider>/<model>`, and everything after the first `/` becomes the OpenCode model key), so its
  casing matters. **The API's names are not the marketing names.** `GET https://api.deepseek.com/models`
  returns exactly two ids — `deepseek-flash` and `deepseek-v4-pro` — and the product name
  "DeepSeek V4.1 Flash" is not one of them: a catalog id of `deepseek/DeepSeek-V4.1-Flash` gets
  `The supported API model names are deepseek-flash, deepseek-v4-pro` at OpenCode start-up, after
  the clan has already been created. Ask that endpoint before adding a direct-provider entry; the
  OpenRouter namespace is separate and does carry the versioned slug
  (`openrouter/deepseek/deepseek-v4.1-flash`), which is why the two entries differ. The DeepSeek
  alias is unversioned and floats onto whatever DeepSeek ships next.
  This id replaced the dated `deepseek/deepseek-v4.1-flash-expires-on-0910` preview id, which
  carried `expires = "2026-09-10"`. No packaged model has an expiry now; `expires` still works in a
  `$RATEL_HOME/models.toml` override, and `clan status` adds a top-level `warnings` entry once
  the date is past.
- **Key-name trap:** the environment this repo's tooling inherits may export `OPENROUTER_API_KEY`
  (the catalog's name for the GLM entry), while a local `.env` sometimes carries
  `OPENROUTER_API_TOKEN` instead. Export the `*_API_KEY` spelling the catalog names — check
  `ratel clan catalog` → `models[].env` if a role refuses to start.

### Overriding the catalogs

Three TOML catalogs ship with the package and merge the same way — the packaged file deep-merged
with a user override in `$RATEL_HOME`, then `clan.toml` on top:

- **`$RATEL_HOME/models.toml`** overrides `ratel/clan/models.toml`: add or amend a model's
  `harness` list, `env` vars, `expires`, `efforts` list or OpenCode fragment without touching the
  package. `efforts` is the list of effort words THAT PROVIDER accepts — DeepSeek's API takes only
  `high` and `max`, OpenRouter takes its normalised ladder, Claude takes `low|medium|high`. There
  is no level-to-vocabulary mapping any more (Decision 31): a word is sent through untouched when
  the model declares it, and emits no flag at all when it does not.
- **`$RATEL_HOME/presets.toml`** overrides `ratel/clan/presets.toml`. A preset is one
  curated setup — `harness`, `model`, `effort`, a `label` for the board's dropdown and an `order`
  for where it appears — under a stable slug. This is the only place a new harness/model/effort
  combination becomes choosable; the board offers presets and nothing else.
- **`$RATEL_HOME/roles.toml`** overrides `ratel/clan/roles.toml`, including `[defaults]`
  (a proposed role the catalog does not know still gets the operator's defaults). A role names a
  `preset`, and that preset BEATS any `harness`/`model`/`effort` key set beside it — half-overriding
  a preset would only fail later, at the model/harness compat check. `clan.toml` and CLI flags
  still win, since they merge after the preset expands.

  Claude kinds get `claude --effort <word>`, OpenCode gets `options.reasoning.effort: <word>` in
  `opencode.json`. `or-glm-flash-low` pins GLM at `low` because GLM's reasoning is mandatory and
  higher efforts starve the code out of its output cap (the headless field notes above). No role
  defaults to GLM any longer — `docs` moved to `or-deepseek-flash-high` — so that finding stays
  true without being worked around.

## Per-agent instructions

Paste `examples/AGENT.md` into each agent's `CLAUDE.md` / `AGENTS.md`, replacing `<name>` and `<channel>`. Keep the orchestrator-only line only in the orchestrator's copy.

The short version: post coordination traffic and not narration, `catch_up` at the start, `post` then `wait_for_mention` when you finish or get stuck, and only the orchestrator owns `plans/`.

## Context measurement

The board meter, `clan status` and the watcher's compact-on-threshold all read
`ratel.clan.context.context_tokens`, which measures each harness's own
usage record — nothing is estimated from scrollback:

- **Claude** (`claude`, `claude-p`): the transcript at
  `~/.claude/projects/<cwd-slug>/<session-id>.jsonl`. Interactive tabs are
  launched with an explicit `--session-id <uuid4>` recorded in
  `clan.state.json` alongside an ISO-UTC `launched`, so the reader opens
  exactly that file. Without a session id it falls back to the newest file
  whose first `cwd`-carrying record matches the tab's cwd (the literal first
  line can be a `custom-title` record with `cwd: null`) **and** whose last
  assistant record's `timestamp` is after `launched` — mtime is never trusted
  (claude touches old files), and a cwd with several session files but no
  launch record is ambiguous, so the answer is `null`, not a guess. The value
  is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`
  (the last turn's `output_tokens` are not summed — a role can read up to one
  max-output under its true window).
- **OpenCode** (`opencode`, `opencode-run`): the newest `session` row whose
  `directory` equals the tab cwd in `~/.local/share/opencode/opencode.db`
  (opened with the read-only URI `file:…?mode=ro`; on a WAL-mode database
  SQLite still creates the `-shm`/`-wal` sidecar files in the operator's data
  dir — it writes no rows and holds no lock afterwards), then its newest
  `message` row with
  `data.role == "assistant"` that has **no** `error` key and a
  `time.completed` (an in-flight turn carries zeros) — value =
  `tokens.input + tokens.cache.read + tokens.cache.write`.
- **fake**: `harness/<role>/context.txt`, a scripted number for tests.

The clan's zellij server scrubs the environment it inherits from the launching
pane: `ZELLIJ_*`, `CLAUDECODE`, `CLAUDE_PID`, and the Claude session-identity
members of `CLAUDE_CODE_*` (`*_SESSION_ID`, `*_MESSAGING_*`, `*_CHILD_*`,
`CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_EXECPATH`). Without the Claude scrub a
clan launched from inside a Claude session makes every role tab a *child
session* — no `~/.claude/projects` transcript of its own, so measurement reads
nothing or, worse, a sibling's stale number. Provider configuration inside
`CLAUDE_CODE_*` (Bedrock/Vertex routing, auth skips, `MAX_OUTPUT_TOKENS`) is
not identity and is kept.

`/clear` and the session pin (measured on Claude 2.1.263):
`--append-system-prompt` survives `/clear` — the brief reloads after the
clear. `/clear` starts a new transcript file, which would leave the tab's
pinned `session_id` pointing at the closed file forever; `clan checkpoint
--mode clear` therefore re-pins measurement by launch time (drops
`session_id`, sets `launched` to the checkpoint timestamp). `/compact`
continues the same session and leaves the pin alone.

## Zellij

One session per project — `examples/zellij-harbor.kdl` → `~/.config/zellij/layouts/harbor.kdl`, then `zellij --layout harbor`. One tab per agent, plus a `ratel tail --channel harbor` view of the bus so you can watch the raw traffic.

Worktrees are siblings of the repo, not inside it, so watchers in the main checkout ignore them:

```bash
git -C ~/code/harbor worktree add ../harbor-wt/a
```

The board is deliberately not in the layout — it runs once, outside any session, and serves every channel.

### Clan quick start

The clan commands replace the static kdl layout with one that builds itself:

```bash
ratel clan new ~/code/harbor 42        # channel + zellij session + orchestrator tab
zellij attach harbor-42-0910-1423          # the session name `clan new` printed
                                               # (channel + start stamp; the channel stays bare)
ratel clan status --channel harbor-42 --screen
ratel clan nudge developer \
  --channel harbor-42 "ping"                # re-nudge a role by hand
ratel clan down --channel harbor-42    # tears the session (and stray rounds) down
```

- `clan new` opens the orchestrator, `watch` and `bus` tabs, then closes zellij's
  default `Tab #1` by id (`close-tab -t <tab id>`) once `list-panes -j` lists it —
  `list-panes` lags `query-tab-names` by ~200-300 ms on a fresh session. Closing
  goes only by id: a bare `close-tab` acts on the focused tab, which is
  destructive with a human attached and does nothing on the detached session
  `clan new` leaves. A stray that never yields an id is left open with a
  warning, and the sweep never crashes `clan new`. Attach and you are in the
  orchestrator's tab, already running `/clan:dispatch-issue 42`.
- **Confirm the clan on the board.** The orchestrator runs `ratel clan
  propose --file proposal.json`, which posts the clan as a structured
  attachment. Open the board with the token URL (see *Running the board*
  below), edit the roles if you want, and press **Confirm**: the approval lands
  on the bus as `stakeholder`, the watcher nudges the orchestrator, and
  `ratel clan approve` writes `clan/clan.toml`. `clan up` then gives every
  role a worktree, a config dir and a tab. Unattended clans take the catalog
  defaults (`--auto-approve`, no board round trip).
- **Trust dialog:** on first attach in a new worktree, accept Claude's trust
  dialog once. Unattended clans pre-accept it by writing the workspace path into
  `~/.claude.json` before the tab opens.
- **A role idle after a nudge** (the nudge landed mid-turn and was swallowed)
  can be re-nudged by hand with `clan nudge <role>`; stale escalation exists for
  the case nobody notices.
- **OpenCode `external_directory` modal:** reading files under the channel
  directory from a worktree triggers one approval prompt; attended configs
  pre-allow the clan's own channel dir so this never blocks.
- **Tier 2 verification** (`-m llm` e2e) needs `CLAN_SANDBOX_TOKEN` — a
  fine-grained PAT scoped to the sandbox repo — and `OPENROUTER_API_KEY`
  exported before `clan new`; the nested clan runs on the scoped token, never
  the operator's own `gh` credential.

Headless field notes for both harnesses are below; the per-role allow lists,
`--add-dir` geometry and the per-round model are described in
[ARCHITECTURE.md](ARCHITECTURE.md) and [DECISIONS.md](DECISIONS.md) (Decision 28).

## Running the board

```bash
uv run ratel-board --host 127.0.0.1 --port 8787
```

Then open <http://127.0.0.1:8787/>. The board is read-only for browsing; the
one write route (confirming a clan proposal) needs the write token: on first
start the board generates `<RATEL_HOME>/board.token` (mode 0600) and prints
one extra line —

```
open once: http://127.0.0.1:8787/?token=<token>
```

Open that URL once in the browser you will confirm clans from: the page stores
the token in `localStorage` and immediately rewrites the address bar without it,
so the token never lands in history or a shared link. Without the token the
clan card renders read-only with a hint. `--home` overrides `RATEL_HOME` for the board only.

## Tailscale

> **⚠ The board's reads are open; its one write is token-gated.** Anyone who can
> reach the port can read every channel, every pinned plan and every attachment —
> including code snippets and files agents have attached — with no login and no
> per-agent view. Writes are different: the single `POST` route (clan approval)
> requires the bearer token from `<RATEL_HOME>/board.token`, which only the
> operator's browser should hold. On your own tailnet open reads are fine,
> because everyone on it is you. On a tailnet shared with other people they will
> see all of it. **Never `tailscale funnel` the board** — that publishes it (and
> its write route) to the entire internet.

The board stays bound to `127.0.0.1:8787`. Tailscale reaches it without opening a port:

```bash
tailscale serve --bg 8787
```

That serves it over HTTPS on your tailnet at `https://<machine>.<tailnet>.ts.net/`, which is the address to open on a phone. SSE works through it, so live updates arrive on mobile the same as on the desktop.

Check and undo:

```bash
tailscale serve status
tailscale serve reset
```

The alternative is plain HTTP straight to the tailnet IP, skipping `serve`:

```bash
ratel-board --host 100.x.y.z --port 8787
```

This binds the board itself to the tailnet interface. It is simpler, but unencrypted and it puts the listener on a non-loopback address — prefer `tailscale serve`.

## Troubleshooting

**The hook prints nothing.** `AGENT_NAME` or `CHANNEL` is not set on the hook command itself. The hook exits 0 and silent when unconfigured, by design — it must never break a session it isn't part of. Test it the way Claude Code runs it:

```bash
AGENT_NAME=worker-a CHANNEL=harbor ratel-unread
```

**The hook prints nothing the second time.** Correct. It advances the cursor, so each message is shown once.

**`no such channel` from the board.** The channel directory does not exist yet — nobody has posted. A channel is created by its first writer, not by the board.

**`ratel-mcp: set AGENT_NAME and CHANNEL`.** The server refuses to start without an identity. Check the `env` block in `.mcp.json`.

**An attachment still shows the old image after you re-attached it.** `/files/` responses are cached for an hour. Hard-reload the board.

**An attachment renders as a struck-through download instead of inline.** Only a known-safe set of types is served inline — images, PDF, plain text and markdown. Anything else, HTML and SVG included, comes back as an inert download, because `files/` names come from agents and the board serves them from its own origin.
