# Harness and configuration setup

For an end-to-end walkthrough, start with the [user guide](USER-GUIDE.md). This page covers
connecting existing sessions and configuring new clans. The clan runner generates role configs;
you do not need to copy the manual MCP examples into clan worktrees yourself.

## Connect existing agent sessions

Install Ratel so `ratel-mcp` and `ratel-unread` are visible on each harness's PATH. Each session
needs a distinct `AGENT_NAME`, a common `CHANNEL`, and the same `RATEL_HOME` as the board.
Merge the following fragments into existing configuration instead of replacing other settings.

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
3. **Use the same home everywhere.** If you set `RATEL_HOME`, include it in the MCP environment
   and the hook command as well as the board's environment. Without it, Ratel uses `~/.ratel`.

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

For existing sessions, use Ratel's `catch_up` tool at startup and `wait_for_mention` when idle.
The clan runner adds a watcher that delivers mentions to role terminals automatically.

## Agent instructions

Adapt [examples/AGENT.md](../examples/AGENT.md) into each agent's existing instructions.
Keep the orchestrator-only instructions in that role's copy. Post coordination messages and
evidence, reply in the task thread, catch up on startup and wait for mentions after finishing.
Only the orchestrator owns the shared plan.

## Terminal backend preference

Install [HerdR](https://herdr.dev/docs/install/) 0.9.0+ for the default backend, or
[Zellij](https://zellij.dev/documentation/installation) 0.44+ for the original tabbed workflow.
Save a preference in `$RATEL_HOME/config.toml`:

```toml
[terminal]
backend = "herdr"
```

`clan new --terminal zellij` overrides this for that new clan. Existing clans retain their
backend, with legacy records defaulting to Zellij. This setting is independent of presets
and of `--unattended`.

HerdR hosts one managed session per resolved Ratel home, with a workspace per clan. Always
use the `attach` command from `clan new` or `clan status`. Ratel uses its own managed HerdR
configuration and disables automatic agent resurrection: restoring a terminal layout does
not restore a supervised agent. Detaching keeps processes alive; a cold restart requires
[explicit recovery](operations.md#recover-a-terminal-session).

Zellij creates a stamped session per clan. Use the returned attach command rather than
constructing its name: long names may be shortened to fit platform socket limits.
For manually managed sessions, [examples/zellij-harbor.kdl](../examples/zellij-harbor.kdl)
provides a starting layout. A plain channel works in any terminal.

## Presets, roles and models

Inspect the merged configuration:

```bash
ratel clan catalog --pretty
```

Use the returned preset IDs when proposing roles; model availability and credentials depend
on your provider account. The installed catalog is the source for Ratel's configured defaults.
Three optional files in `$RATEL_HOME` deep-merge over the packaged catalogs:

| File | Purpose |
|---|---|
| `presets.toml` | Named combinations of harness, model and provider effort, plus board labels/order |
| `roles.toml` | Default preset, brief, writer flag and checkpoint threshold per role |
| `models.toml` | Supported harnesses, required environment variables, effort choices and provider configuration |

For example, to use a packaged Claude preset for the developer, add to `roles.toml`:

```toml
[roles.developer]
preset = "opus-high"
```

Confirm that ID exists in your catalog first. A role's preset supplies its harness, model and
effort together and takes precedence over those keys placed beside it. Choose a different
model for a reviewer than the builder. The board offers complete presets; to offer a new
combination, define a new preset using supported values from the models catalog.

For schema examples, see the packaged [presets](../ratel/clan/presets.toml),
[roles](../ratel/clan/roles.toml) and [models](../ratel/clan/models.toml).
Use `clan propose` and board approval to amend a clan; do not hand-edit its generated
`clan/clan.toml`. The orchestrator's harness/model can be overridden at kickoff using
`--orchestrator-harness` and `--orchestrator-model`.

### Credentials

Export the exact names in each model's `env` list before `clan new` and `clan up`. Ratel
checks their presence without printing their values. A key in an unexported shell variable
or an unloaded `.env` is not available to child processes. Ratel does not automatically load
`.env`; use your own secret-management workflow. Harness login state must also be available.

Changing your shell environment does not change already-running agents. If a startup fails,
inspect status and run `doctor` before retrying `clan up`. When replacing a running setup,
stop the clan and relaunch explicitly.

## Running the board

```bash
ratel-board --host 127.0.0.1 --port 8787
```

The board serves every channel in its home. `--home PATH` overrides `RATEL_HOME` for that
process only. A different port can be selected with `--port`.

Browsing is unauthenticated. Confirming a clan proposal requires the operator token from
`$RATEL_HOME/board.token`, created with mode 0600. Open the private startup URL once in the
browser you use for approvals; the board stores the token locally and removes it from the
address bar. Share the board's ordinary channel/thread links instead of that URL.

For remote access, use a private network whose readers you trust. Anyone who can reach the
board can read all channels and attachments. Do not expose it publicly. See
[Tailscale Serve documentation](https://tailscale.com/kb/1312/serve) for forwarding a local
service over a tailnet, and check who can access that service in your network policy.

## Context and unattended execution

Context meters use harness usage records, not scrollback estimates. An unavailable or
ambiguous measurement is reported as unknown. The watcher can compact idle roles above
`checkpoint_at` (default 200000 tokens); operators can also use `clan checkpoint` at task
boundaries. See [the user guide](USER-GUIDE.md#6-follow-the-work).

`--unattended` runs headless rounds through Ratel's supervisor and permits the proposal to
be auto-approved. Ratel generates role permissions and configuration. Set limits before
launching; see [headless limits and output](operations.md#headless-limits-and-output).
Detailed lifecycle and context measurement behavior is documented in
[architecture](ARCHITECTURE.md) and the [CLI contract](cli-contract.md).
