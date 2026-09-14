# CLI contract

The `ratel` CLI is the shell-side face of a channel: one invocation, one
JSON value on stdout, exit 0 on success. It exists for agents without an MCP
host (scripts, hooks, tests) and for the operator; agents living in tabs use
the MCP server instead. Both share one implementation — `AgentOps` — so cursor
and read semantics cannot drift between them.

## Invocation rules

- **One JSON value per invocation.** Every command prints exactly one JSON
  value (an object, an array, a string, or `null`-shaped empty result) and
  exits 0. `wait` returning empty is success: an idle channel is not an error.
- **`--pretty`** indents the JSON for humans; scripts should not use it.
- **Environment:** `AGENT_NAME` and `CHANNEL` select the agent and channel;
  `RATEL_HOME` (default `~/.ratel`) locates the channel directory;
  `AGENTCHAT_HOME` is accepted as a deprecated fallback (a warning prints) so an
  existing `~/.agentchat` stays reachable until the operator `mv`s it.
  `CLAN_NO_NESTED=1` makes `ratel clan new`, `up` and `launch` exit
  non-zero without doing anything (docs/DECISIONS.md, Decision 29).
  `--agent` and `--channel` override the env per invocation. `--home` exists on
  the `clan` subcommands only — channel commands take `RATEL_HOME` from the
  environment.
- **Errors:** non-zero exit with `ratel <cmd>: <message>` on stderr and
  nothing (or no new value) on stdout.
- **Agents in tabs use MCP** (`ratel-mcp`, one stdio server per agent);
  **scripts, tests and hooks use the CLI.** Hooks (`ratel-unread`) render
  unread messages as text rather than JSON because their output goes into a
  prompt.

## Channel commands

| Command | Purpose | JSON value |
|---|---|---|
| `post <text>` | post a message (`--parent` threads, `--pin` pins, `--attach PATH` repeatable) | the new message id |
| `read --agent A` | messages from others after A's cursor, oldest first (`--since`, `--limit`) | array of messages |
| `catch-up --agent A` | everything since A's cursor plus the current pins | object `{pins, messages}` |
| `thread <id>` | one top-level message plus its replies | one message object |
| `wait --agent A` | block until A is @mentioned (`--any` for any traffic, `--timeout S` to bound) | array of new messages; empty is success |
| `pin <id>` / `unpin <id>` | pin a message / unpin a message by id | the id |
| `pins` | the current pins | array |
| `attach <path>` | copy a file into the channel's `files/` (`--name` renames) | attachment object |

## Clan commands

`ratel clan …` drives a clan of role agents on one issue. The clan's
channel, zellij session and config all share one name.

| Command | Purpose | JSON value |
|---|---|---|
| `clan roles` | the merged role catalog (alias of `clan catalog`) | the catalog object |
| `clan catalog` | harnesses, presets, roles and models — the same object `GET /api/clan/catalog` serves the board | `{"harnesses": […], "presets": [{id, order, label, harness, model, effort}], "roles": {role: {preset, harness, model, effort, writer, brief, checkpoint_at}}, "models": [{id, name, harness, provider, env, expires, effort_levels}]}` — presets sorted by `(order, id)`, models by id, `effort_levels` the words that model DECLARES. There is no top-level `efforts`: the level vocabulary is gone (Decision 31) |
| `clan propose --file PATH\|-` | validate a `{"roles": […]}` proposal — one object per role, exactly `{name, preset, writer, skills, why}`, no harness/model/effort — against the catalog, write `clan/proposal.json`, and post it pinned on the bus as `orchestrator`; `-` reads the proposal JSON from stdin (nothing is approved by that) | `{"id": "<bus msg id>", "roles": [names]}`; with `--auto-approve` also `"result"` — the clan landed as `clan.toml` with no board round trip (the approval supersedes the proposal's own message) |
| `clan approve [MSG_ID]` | land the newest approved clan attachment from `stakeholder` on the bus (or the named message, which must also be from `stakeholder`) as `clan.toml` + writer bits; each role's `preset` is RE-RESOLVED against the catalog at this point, not at propose time, and a preset the catalog no longer ships is a clear error; refuses (exit 1) with no proposal on the bus, when `supersedes` is not the newest proposed clan message, or when the approval is not from `stakeholder` | the clan: `{"channel", "issue", "repo", "checkout", "roles": {role: {harness, model, writer, brief, checkpoint_at, effort}}}` — the preset spread into the three keys it carries |
| `clan new <checkout> <issue>` | create the channel + zellij session, open the orchestrator, watch and bus tabs | `{"session", "channel", "attach"}` |
| `clan up` | worktree, config and tab for every role not yet up (idempotent); refuses (exit 1) before any zellij/worktree side effect when a role's model needs an unset env var, naming the role and the missing keys | `{"started": [roles]}` |
| `clan launch <role>` | run one role's harness inside its tab (interactive kinds exec and replace the process; headless kinds run one round per nudge and log to `rounds.jsonl`) | none — runs until `clan down` |
| `clan watch` | tail the bus and type a mention line into the mentioned role's pane (runs in its own tab) | none — long-running |
| `clan status` | per-role rows (`--screen` adds a pane dump each); rows gain `effort` and `model_expired`, top-level `warnings` names expired models | `{"session", "channel", "issue", "repo", "roles": [{role, harness, model, writer, effort, model_expired, tab_id, pane_id, worktree, branch, dirty, last_nudge, context_tokens, checkpoint_at[, screen]}], "warnings", "checkpoints", "presence"}` |
| `clan nudge <role>` | type one line into a role's pane (`text` optional; default is a mention of the newest message) | `{"role", "pane", "text"}` |
| `clan checkpoint <role>` | reset a role's context: `/clear`/`/new` (default `--mode clear`; `--mode compact` types `/compact` on both) typed into an interactive pane with a re-orient nudge, or a `reset` marker for headless kinds; refuses a busy role (in `watch.pending` or `watch.nudged`) unless `--force`, exiting 1 with `{"role", "reason": "busy", "hint"}` on stderr; refuses `--mode clear` on the orchestrator outright (`"reason": "orchestrator is never cleared"`, no override — compact is allowed) and refuses a role checkpointing itself (`AGENT_NAME` equals the role, `"reason": "self-checkpoint"`) unless `--force` (Decision 38) | checkpoint record `{"role", "mode", "ts", "context_tokens", "reason"}` |
| `clan sync <role>` | fast-forward a detached reviewer worktree to the branch tip | `{"role", "worktree", "head"}` |
| `clan down` | kill the zellij session (`--prune-worktrees` removes its clean worktrees; add `--force` to discard uncommitted changes) and kill any headless round that outlived its pane | `{"session", "worktrees_removed", "rounds_killed"}` |

Notes:

- `clan new` records `checkout` and the orchestrator's writer bit in
  `clan.state.json` at creation; `clan up` pins each role's writer bit as it
  starts them. Later commands refuse to run against a `clan.toml` that changed
  either.
- `clan status` reports the *effective* harness kind from state — on an
  unattended clan an interactive `claude`/`opencode` role reads as
  `claude-p`/`opencode-run` — because that is what is actually running in the
  tab.
- `clan new --unattended` (and `clan up` on an unattended clan) map interactive
  kinds to their headless twins, pre-accepts Claude's workspace trust dialog,
  and sets `CLAN_UNATTENDED=1` in the harness environment.
- `clan new --zellij-tmp DIR` runs the clan's zellij server under `DIR`
  (tests); the directory must be short, because zellij's socket path is capped
  at 103 bytes.
- `clan approve` trusts approvals only from the `stakeholder` bus name and
  proposals only from `orchestrator` (docs/DECISIONS.md, Decision 32);
  `--auto-approve` still posts the proposal on the bus — it approves it
  in-process after posting a stakeholder approval, with no board round trip.

## Board HTTP API

The board serves the channel JSON API over GET and exactly one write route.
Request and error bodies are JSON; errors are `{"error": "<message>"}`.

| Route | Returns |
|---|---|
| `GET /api/channels/{ch}/history?before=&limit=&q=&mention=&operator=` | Latest matching page in append order: `messages`, `next_before`, `tip`; initial pages also include proposal `heads`. Default 100, maximum 200; invalid parameters or unknown `before` return 400. |
| `GET /api/channels/{ch}/pins` | Current `pins` and newest trusted proposal/approval `heads`, independent of the history window. |
| `GET /api/clan/catalog` | the same object as `ratel clan catalog` — `presets` is what fills the clan card's one `setup` dropdown per role |
| `GET /static/{name}` | a vendored asset from an allow-list dict (`mermaid.min.js` only); `immutable` + `nosniff` + `default-src 'none'`, gzip when `Accept-Encoding` allows. Anything else, including `board.html`, is `404` |
| `POST /api/channels/{ch}/post` | `201 {"id": "<bus msg id>"}` — posts one message as `stakeholder` |

The POST is gated by `Authorization: Bearer <token>` where `<token>` is the
0600 `<RATEL_HOME>/board.token` file, created on first board start and
printed once by `ratel board` as an `open once: http://<host>:<port>/?token=…` line.
The body is a message document: `text` (≤ 1000 bytes), optional `parent` (ULID),
and exactly one attachment of `type: "clan"`, `status: "approved"`, validated by
the same `validate_clan_attachment` as the CLI (`from` in the body is ignored —
the message always lands as `stakeholder`). A role in that attachment is
`{name, preset, writer, skills, why}` and nothing else: the preset id is the
only setup the bus can name, and `clan approve` resolves it against the catalog
(Decision 36).

Guards run in this order, each an early return before the body is read where
noted: unknown route → `404 {"error": "not found"}` with `Connection: close`
(nothing is read); bad or missing bearer → `401 {"error": "unauthorized"}` with
`WWW-Authenticate: Bearer` and `Connection: close`; `Transfer-Encoding` → `411
{"error": "length required"}`; missing, unparseable, zero or `> 8192`
`Content-Length` → `413 {"error": "payload too large"}`; non-`application/json`
content type →
`415 {"error": "unsupported media type"}`; body is read as exactly
`Content-Length` bytes, then non-JSON or non-object → `400 {"error": "body must
be JSON"}` / `{"error": "body must be a JSON object"}`; unknown channel →
`404 {"error": "no such channel"}`; schema violations → `422` naming the field
(e.g. `{"error": "attachments: status must be approved"}`) including the
newest-proposal guard: `{"error": "attachment supersedes … but the newest
proposed clan message is …"}`. On `401`/`411`/`413`/`415` the connection is
closed without reading the body, so a keep-alive client cannot desync.


## Storage and migration

New channels use `channels/<channel>/channel.sqlite3`, schema version 2, on local disk.
All database processes must run on the same host; remote board clients use HTTP. Do not put
an active channel database on a network filesystem. SQLite connections use WAL and bounded
lock waits; write failures are reported rather than acknowledged without persistence.

`--home PATH` overrides RATEL_HOME for channel commands. Public message objects keep their
ULIDs and existing fields. `read --since ID` resolves ID to database append order, not ULID
sorting. An unknown ID replays from the beginning. A provided `--limit` must be positive.
A cursor only advances; presence-only touches never reset it.

| Command | Contract |
|---|---|
| `diagnostics --channel NAME` | Read-only counts of malformed messages and cursors; no AGENT_NAME required. |
| `migrate --channel NAME` | Offline legacy import; prints one JSON report with message/cursor/skipped-line counts, database path and original-file retention. Refuses an existing database. No AGENT_NAME required. |
| `export --channel NAME` | Prints JSONL messages in append order without consuming agent cursors. No AGENT_NAME required. |
| `tail --channel NAME [--since ID]` | Replays history (or messages after ID), then follows committed messages as JSONL. Ctrl-C exits cleanly. No AGENT_NAME required. |

To migrate: stop every writer for that channel, run `ratel migrate`, inspect its counts and
`ratel export`, then restart all writers with the new version. Old JSONL and cursor files are
retained verbatim; they are never dual-written. Unknown legacy cursor IDs replay history.
Malformed/torn message lines are counted and retained in the original file; duplicate IDs
abort the whole import. Legacy attachments retain their existing references and files.

Before accepting new writes, rollback is possible by stopping channel processes and moving
the newly created database and any associated WAL/SHM files aside, then using the retained
legacy files with the old version. After accepting new writes the retained files are stale:
export/reconcile new messages first; do not roll back by simply deleting the database.
Back up active databases with SQLite's backup API/tooling, or stop all channel processes before
copying the database and attachments. Never copy only the main file of an active WAL database.

The SSE endpoint emits each message's public ID as `id:`. On reconnect, `Last-Event-ID`
takes precedence over the original `since` query parameter. Stream polling finishes its
read transaction before writing to the browser or sleeping. The board still has exactly one
write route; an unmigrated channel answers its approval POST with 409 and must be migrated first.

Approval posting checks the newest orchestrator proposal and inserts the stakeholder decision
in one transaction. An identical retry returns the original message id; a conflicting second
decision returns 409 and requires a new proposal. `clan approve` records a durable application
intent before saving config and state. If interrupted, rerun `clan approve` to finish applying
the saved snapshot; lifecycle commands refuse a pending application. Completed retries are
idempotent. This does not authenticate arbitrary bus writers sharing the same OS account.

Channel and agent names use letters, digits, underscores, dots and hyphens, excluding
`.` and `..`. Clan roles must also be valid mention names. Symlinks within channel storage (including SQLite sidecars) are refused. The
operator-selected home may itself be a symlink. Malformed messages are skipped on reads and
counted by `diagnostics`; an invalid cursor replays history and is repaired on advancement.

History search (`q`, at most 200 characters) is a literal, case-insensitive match in message
text, including replies. `mention` matches an exact mention; `operator=1` selects posts by
or mentioning `stakeholder`. Filters combine with AND. `before` is an exclusive public
message ID resolved to append sequence; a null `next_before` means no older matches.
`tip` is the snapshot's last valid message, even when a filter matches nothing. The board
connects SSE from that tip so arrivals between the fetch and connection are replayed.
Unfiltered agent-facing `/messages?since=&limit=` semantics are unchanged.

Board links retain the original `#channel` form and add optional hash parameters:
`#channel?thread=ID&q=search&mention=agent&operator=1`. Browser Back/Forward restores this
view. Token query parameters are removed before saving navigation history and are never
included in Channel link or Thread link. Missing channels/threads show recoverable errors.
