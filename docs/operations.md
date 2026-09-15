# Operations and troubleshooting

Use the [user guide](USER-GUIDE.md) for your first run and
[harness setup](harness-setup.md) for configuration. Commands here use `harbor-42` as an
example channel. Pass the same `--home` or `RATEL_HOME` used to create it.

## Start with diagnostics

```bash
ratel doctor --channel harbor-42 --pretty
ratel clan status --channel harbor-42 --screen --pretty
```

`doctor` inspects configuration, required binaries, credential presence, storage and runtime
without repairing them or exposing credential values. Status shows the attach command,
worktrees, context, terminal activity and supervised run state. Screen output can contain
agent work; review it before sharing diagnostics.

| Symptom | Action |
|---|---|
| Board is empty or a channel is missing | Check the board and agents use the same home. A plain channel is created by its first post. |
| `ratel-mcp` refuses to start | Set both `AGENT_NAME` and `CHANNEL` in the MCP server environment; check its PATH. |
| Claude hook prints nothing | Set identity and channel on the hook itself. It is silent when unconfigured or when there are no unread messages. |
| Required provider key is missing | Inspect `clan catalog` → `models[].env`; export the exact name before launching. |
| `clan up` reports the branch is checked out elsewhere | Inspect `git worktree list`. Preserve local work, then free `issue-<n>` for the writer's worktree. |
| Orchestrator refuses the issue | Resolve its guard: blocking label, existing open PR or open sub-issues. |
| Proposal has no active Confirm button | Open the private board startup URL in your operator browser; check that exactly one writer is selected. |
| Role is blocked / awaiting operator | Attach using status, inspect the role's tab and answer the trust/permission prompt. |
| HerdR nudge returns `queued` | Keep the watcher running. Input is held until the recorded agent is ready; inspect activity and freshness. |
| HerdR reports unknown/unavailable | Check the server, workspace and recorded process with doctor. Do not type a dispatch into a replacement shell. |
| Zellij role missed a nudge | Inspect the pane, resolve any prompt, then use `clan nudge` when the role is idle. |
| Headless role stops receiving nudges | Inspect its budget/stop reason. A checkpoint does not reset a launch budget. |
| Board loses its live connection | Use Retry and check the board process; reconnecting reloads updates. |
| An attachment preview is stale | Hard-reload; file responses are cached. Unsupported inline types are offered as downloads. |

## Recover a terminal session

Detaching and reattaching keeps a live session running. Recover its command with:

```bash
ratel clan status --channel harbor-42 --pretty
```

HerdR restoration after a server crash is different: a restored pane or shell is not the
original agent. Ratel verifies terminal identity and launched process identity before sending
input. It refuses to treat a cold-restored shell as a working role. It also refuses to replace
an alive but unreachable managed server implicitly.

Inspect the reported failure, preserve any worktree changes, and explicitly stop the clan:

```bash
ratel clan down --channel harbor-42
```

Once the backend is reachable or its old server has stopped, start a new run with `clan new`.
The existing channel history remains. Check issue guards, saved work and any open PR before
restarting dispatch; a new launch does not mean the previous task completed. If stopping fails,
resolve the reported server/process problem before retrying. Avoid killing a whole shared
HerdR server to recover one clan because other clans may still be running there.

Use the same down/new procedure to change terminal backends. Editing the preference alone
does not migrate active roles. Do not alter pane IDs or runtime records by hand.

## Headless limits and output

Limits apply per role, per launch. Export settings before `clan new --unattended`:

```bash
export CLAN_MAX_ROUNDS=50
export CLAN_MAX_SECONDS=14400
export CLAN_MAX_FAILURES=3
ratel clan new /path/to/harbor 42 --unattended
```

| Setting | Default | Meaning |
|---|---|---|
| `CLAN_MAX_ROUNDS` | 100 | Maximum rounds in this launch |
| `CLAN_MAX_SECONDS` | 28800 | Wall-clock lifetime, including idle time |
| `CLAN_MAX_FAILURES` | 3 | Consecutive failed rounds before stopping |
| `CLAN_ROUND_BUDGET_USD` | Unset | Per-round spend cap for `claude-p`; other harnesses reject it |
| `CLAN_ROUND_LOG` | Bounded tails | Set to `full` to retain full per-round output files |

Status and the board show the stop reason; the watcher stops nudging a stopped role.
Checkpointing preserves the budget. An explicit operator launch starts a new budget.
Full output can consume disk and contain sensitive task data. Retention only removes orphan
logs that meet its criteria, not every old run log. See the
[limits contract](cli-contract.md#headless-run-limits) for exact validation and state fields.

## Stop and clean up

```bash
ratel clan down --channel harbor-42
ratel clan down --channel harbor-42 --prune-worktrees
```

Stopping preserves history and worktrees; pruning additionally removes clean worktrees
created for that clan. Dirty worktrees are refused unless you explicitly request `--force`,
which discards their changes. Review and save the diff first. With HerdR, shutdown targets
this clan's owned terminals/workspace and leaves sibling clans and the shared server alive.

## Upgrade an existing installation

For a Git installation:

```bash
uv tool upgrade ratel
```

For an editable checkout, update it through your normal Git workflow and run
`uv tool install --editable /path/to/ratel-agent-harness --force`.
Stop old agents and writers before upgrading storage; all writers must use the same format.
New homes already use SQLite and need no import.

### Import legacy channels and clan state

After stopping every process writing the affected channel, including its clan:

```bash
ratel migrate --channel harbor-42
ratel migrate-state --channel harbor-42
```

Use the first command for legacy JSONL messages and the second for legacy `clan.state.json`.
Import messages before state. Original files remain unchanged. Restart all writers on the
new version afterward. The board can browse legacy channels, but new writes require migration.
An older Ratel version cannot read subsequent SQLite-only writes; retained JSONL is not a
current backup. See [storage and migration](cli-contract.md#storage-and-migration).

## Back up and retain data

Maintenance is offline: stop the clan and all other writers first. Start with a dry run:

```bash
ratel archive --channel harbor-42
ratel retain --channel harbor-42 --older-than-days 30
```

To create a verified complete channel copy, choose a new directory outside channel storage
whose parent already exists:

```bash
ratel archive --channel harbor-42 --apply --destination /backups/harbor-42-copy
```

Archive leaves the source intact. A channel archive does not replace a Git backup of the
checkout/worktrees or a backup of home-level configuration and credentials.
To back up and then remove eligible old unreferenced attachments and orphan full-output logs:

```bash
ratel retain --channel harbor-42 --older-than-days 30 --apply --destination /backups/harbor-42-retention
```

Messages, plans, configuration and worktrees remain. Each applied operation requires a new
destination; do not reuse the archive path. Follow the verification and restore procedure in
[the maintenance contract](cli-contract.md#offline-archive-and-retention) before restoring a copy.
Keep SQLite channel storage on a local disk rather than a concurrently shared network filesystem.
