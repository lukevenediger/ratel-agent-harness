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
  `--agent` and `--channel` override the env per invocation. `--home PATH` overrides `RATEL_HOME` for channel and clan commands.
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
channel identifies its coordination history. Runtime state records the selected terminal backend and its session.

| Command | Purpose | JSON value |
|---|---|---|
| `clan roles` | the merged role catalog (alias of `clan catalog`) | the catalog object |
| `clan catalog` | harnesses, presets, roles and models — the same object `GET /api/clan/catalog` serves the board | `{"harnesses": […], "presets": [{id, order, label, harness, model, effort}], "roles": {role: {preset, harness, model, effort, writer, brief, checkpoint_at}}, "models": [{id, name, harness, provider, env, expires, effort_levels}]}` — presets sorted by `(order, id)`, models by id, `effort_levels` the words that model DECLARES. There is no top-level `efforts`: the level vocabulary is gone (Decision 31) |
| `clan propose --file PATH\|-` | validate a `{"roles": […]}` proposal — one object per role, exactly `{name, preset, writer, skills, why}`, no harness/model/effort — against the catalog, write `clan/proposal.json`, and post it pinned on the bus as `orchestrator`; `-` reads the proposal JSON from stdin (nothing is approved by that) | `{"id": "<bus msg id>", "roles": [names]}`; with `--auto-approve` also `"result"` — the clan landed as `clan.toml` with no board round trip (the approval supersedes the proposal's own message) |
| `clan approve [MSG_ID]` | land the newest approved clan attachment from `stakeholder` on the bus (or the named message, which must also be from `stakeholder`) as `clan.toml` + writer bits; each role's `preset` is RE-RESOLVED against the catalog at this point, not at propose time, and a preset the catalog no longer ships is a clear error; refuses (exit 1) with no proposal on the bus, when `supersedes` is not the newest proposed clan message, or when the approval is not from `stakeholder` | the clan: `{"channel", "issue", "repo", "checkout", "roles": {role: {harness, model, writer, brief, checkpoint_at, effort}}}` — the preset spread into the three keys it carries |
| `clan new <checkout> <issue>` | create the channel and its terminal workspace/session, open the orchestrator, watch and bus tabs | `{"session", "channel", "terminal_backend", "workspace_id", "attach"}` |
| `clan up` | worktree, config and tab for every role not yet up (idempotent); refuses (exit 1) before any terminal/worktree side effect when a role's model needs an unset env var, naming the role and the missing keys | `{"started": [roles]}` |
| `clan launch <role>` | run one role's harness inside its tab (interactive kinds exec and replace the process; headless kinds run one round per nudge and log to `rounds.jsonl`) | none — runs until `clan down` |
| `clan watch` | tail the bus and type a mention line into the mentioned role's pane (runs in its own tab) | none — long-running |
| `clan status` | per-role rows (`--screen` adds a pane dump each); rows gain `effort` and `model_expired`, top-level `warnings` names expired models | `{"session", "channel", "issue", "repo", "roles": [{role, harness, model, writer, effort, model_expired, tab_id, pane_id, worktree, branch, dirty, last_nudge, context_tokens, checkpoint_at[, screen]}], "warnings", "checkpoints", "presence"}` |
| `clan nudge <role>` | type one line into a role's pane (`text` optional; default is a mention of the newest message) | `{"role", "pane", "text"}` |
| `clan checkpoint <role>` | reset a role's context: `/clear`/`/new` (default `--mode clear`; `--mode compact` types `/compact` on both) typed into an interactive pane with a re-orient nudge, or a `reset` marker for headless kinds; refuses a busy role (in `watch.pending` or `watch.nudged`) unless `--force`, exiting 1 with `{"role", "reason": "busy", "hint"}` on stderr; refuses `--mode clear` on the orchestrator outright (`"reason": "orchestrator is never cleared"`, no override — compact is allowed) and refuses a role checkpointing itself (`AGENT_NAME` equals the role, `"reason": "self-checkpoint"`) unless `--force` (Decision 38) | checkpoint record `{"role", "mode", "ts", "context_tokens", "reason"}` |
| `clan sync <role>` | fast-forward a detached reviewer worktree to the branch tip | `{"role", "worktree", "head"}` |
| `clan down` | stop the selected clan’s terminals (`--prune-worktrees` removes its clean worktrees; add `--force` to discard uncommitted changes) and kill any headless round that outlived its pane | `{"session", "terminal_backend", "workspace_id", "worktrees_removed", "rounds_killed"}` |

Notes:

- `clan new` records `checkout` and the orchestrator's writer bit in
  SQLite clan state at creation; `clan up` pins each role's writer bit as it
  starts them. Later commands refuse to run against a `clan.toml` that changed
  either.
- `clan status` reports the *effective* harness kind from state — on an
  unattended clan an interactive `claude`/`opencode` role reads as
  `claude-p`/`opencode-run` — because that is what is actually running in the
  tab.
- `clan new --unattended` (and `clan up` on an unattended clan) map interactive
  kinds to their headless twins, pre-accepts Claude's workspace trust dialog,
  and sets `CLAN_UNATTENDED=1` in the harness environment.
- `clan new --terminal zellij --zellij-tmp DIR` runs the clan's zellij server under `DIR`
  (tests); the directory must be short, because zellij's socket path is capped
  at 103 bytes.
- `clan approve` trusts approvals only from the `stakeholder` bus name and
  proposals only from `orchestrator` (docs/DECISIONS.md, Decision 32);
  `--auto-approve` still posts the proposal on the bus — it approves it
  in-process after posting a stakeholder approval, with no board round trip.

## Terminal backends

`clan new --terminal herdr|zellij` overrides `[terminal] backend` in
`$RATEL_HOME/config.toml`. The default for new clans is `herdr` (minimum 0.9.0).
Invalid choices and missing binaries fail explicitly; there is no automatic fallback.
The runtime's persisted `terminal_backend` controls every subsequent command.
Records without this field mean Zellij. Changing a preference does not migrate a clan;
stop it with `clan down` before creating it with a different backend. `--unattended`
remains independent of the backend and still selects the headless harness twins.

HerdR uses a deterministic `ratel-<home-hash>` session with one owned workspace per
clan. Attach using the command returned by `new` or `status`; plain `herdr` attaches
to the user's separate default session. Managed configuration and server identity/logs
live under `$RATEL_HOME/terminal/herdr`. Short Unix socket paths live beneath a private
`/tmp/ratel-herdr-<uid>` directory. Agent processes retain their normal XDG configuration
and state paths. Ratel does not install global integrations or change the user's HerdR config.

`clan status` adds `terminal_backend`, `workspace_id` and `attach`. Role rows include
`terminal: {state, at, stale, source}` for HerdR, or null for Zellij. `state` is one of
`idle`, `done`, `working`, `blocked`, `unknown`, `unavailable`; `stale` is true when the
watcher's observation is missing or over ten seconds old. This is separate from the
existing coordination `state`. Pane/tab IDs may be strings for HerdR; legacy Zellij
numeric values and numeric-string coercion remain compatible.

For HerdR, `clan nudge` persists a control message and returns `queued: true`. A running
watcher submits it when the launch is ready. Verdicts and escalations are also retained
until delivery; blocked/working/unknown/unavailable targets receive no automatic input.
A delayed response or process crash can cause a duplicate nudge; channel cursors and
thread IDs still determine unread work. Manual approval remains in the agent terminal.
Checkpoints check readiness even with `--force`; reorientation is queued until ready.
Headless reset markers retain their existing boundary-at-next-round behavior.

`clan down` closes owned terminals (including moved ones), preserves unrelated panes
and workspaces, and leaves the shared server running. It still terminates headless
process groups and applies the same worktree-pruning preflight. Detaching the client
keeps launches running. A cold server restart loses launches; native agent auto-resume
is disabled so it cannot bypass Ratel launch configuration and budgets. Restored shells
cannot receive queued input. Recover explicitly with `clan down`, then `clan new` and
approve/up as usual. An unreachable but still-live managed server requires restoring
its socket before retrying. `ratel doctor --channel NAME` checks workspace and launch
availability without posting lifecycle reports or changing configuration.

Validation commands (all homes and sessions are temporary):

```bash
uv run --frozen pytest -q tests/test_clan_herdr.py
HERDR_TEST_BINARY=/path/to/herdr uv run --frozen pytest -q tests/test_clan_herdr.py
HERDR_NATIVE_SMOKE=1 HERDR_TEST_BINARY=/path/to/herdr uv run --frozen pytest -q tests/test_clan_herdr.py -k native
```

Native smoke checks start installed Claude Code/OpenCode without a prompt. Set
`HERDR_TEST_CLAUDE` / `HERDR_TEST_OPENCODE` for binaries outside PATH. They validate
startup and detection, without spending model tokens. Full provider workflows remain
the separately opted-in provider tests.

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

New channels use `channels/<channel>/channel.sqlite3`, schema version 3, on local disk.
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


## Runtime state and diagnostics

`migrate-state --channel NAME` imports legacy `clan.state.json` into the channel database.
Stop the clan and all channel processes first; migrate legacy messages with `migrate` first
if needed. The command prints `{"version": 1, "original_retained": "<path>"}`. It refuses
malformed state or an existing SQLite state row and retains the original file unchanged.
Legacy state remains readable, but runtime writes require this explicit import. New clans
use SQLite immediately. Schema 3 adds versioned clan state and a monotonic revision; older
schema 1/2 databases upgrade transactionally on writes. State payload version 1 retains
unknown fields; unsupported versions fail explicitly. Messages without a `version` field
use message contract version 1; an explicit unsupported version is rejected.

`doctor [--channel NAME]` performs read-only local checks without creating a home or database.
It prints `{"ok": true|false, "checks": [{"check", "status", "detail"}]}`. Status is `ok`,
`warning`, or `error`; errors produce exit 1 **after** printing the report, while warnings
alone exit 0. Checks cover SQLite integrity and malformed-record counts, pending approvals,
clan/catalog configuration, binaries, required credential presence, and recorded worktrees.
Credentials are reported only as present/not set. Without a channel it checks the home and
suggests selecting a channel. It neither repairs state nor runs provider authentication.

Headless round records retain bounded stdout and stderr tails: at most 40 chunks of up to
4096 characters and 65,536 characters per stream. Records include `stderr`, per-stream
`truncated` flags and `logs` filenames. `CLAN_ROUND_LOG=full` streams complete output into
exclusive per-round stdout/stderr files in the role's harness directory; it does not enlarge
in-memory buffers. `CLAN_ROUND_TIMEOUT` must be a finite positive number of seconds.


## Headless run limits

`claude-p` and `opencode-run` have per-role, per-launch limits. Set these environment variables
before creating the clan's Zellij session (or before a direct launch):

| Variable | Default | Meaning |
|---|---|---|
| `CLAN_MAX_ROUNDS` | 100 | Maximum attempted rounds, including failed launches. |
| `CLAN_MAX_SECONDS` | 28800 | Elapsed seconds since launch, including idle time between nudges. |
| `CLAN_MAX_FAILURES` | 3 | Consecutive nonzero exits, timeouts, authentication/prompt errors or capture failures. A successful round resets this count. |
| `CLAN_ROUND_BUDGET_USD` | unset | Optional provider-enforced spend limit per Claude print-mode round. |

All configured limits must be positive and finite; rounds/failures must be integers. They
apply to both attended and unattended headless launches. Interactive and scripted fake harnesses
have no supervisor budget. A checkpoint resets conversation state but not these counters.
An operator's new launch starts a new budget; automatic retries and watcher nudges cannot
restart a stopped role. This is not an account-wide or shared clan spend cap.

A running child is killed at the earlier of its round timeout and remaining launch time,
with bounded pipe-cleanup waits afterward. Idle input also expires at the launch deadline.
`clan status` and the board expose state `stopped`, a `stop_reason` code (`max_rounds`,
`max_seconds`, `max_failures`) and a human-readable reason. SQLite `runs[role]` retains the
configured limits and counters. The watcher excludes stopped roles from nudges, stale-work
escalations and automatic compaction. Records remain available for inspection.

Claude's documented [`--max-budget-usd`](https://code.claude.com/docs/en/cli-usage) print-mode
flag implements the per-round spend limit. Every round gets the same configured allowance;
provider accounting/enforcement determines actual spend. `opencode-run` rejects this option
before launching a child. No portable token budget or cost estimate is inferred from context
size; token accounting differs by harness/provider. Ratel's round/time/failure limits work
without provider usage reporting.

## Offline archive and retention

`archive --channel NAME` reports the number of files to archive. `retain --channel NAME
[--older-than-days N]` lists eligible orphans (default 30 days). Both default to a read-only
dry run and require an existing SQLite channel. `--apply --destination PATH` creates a
new complete backup directory; its parent must exist and it must be outside all channel
storage. An existing destination is always refused. Both return:
`{"channel", "dry_run", "candidates", "files_to_archive", "removed", "archive"}`.

Stop **all** channel writers before applying maintenance. For a clan, run `clan down` first:
recorded tabs, missing explicit shutdown state or any remaining round PID ledger block the
operation, even if they might be stale. Pending approval applications must be recovered first.
`clan down` keeps worktrees by default; maintenance never prunes or changes them. Symlinks,
special files and `.env` files in channel storage are refused.

The backup uses SQLite's backup API, then verifies integrity and SHA-256 hashes of copied
files. It includes the database, attachments, plans, configuration, runtime records and retained
legacy inputs. SQLite-managed WAL/SHM/journal files are excluded because the backup contains
their committed state. `archive-manifest.json` lists content hashes and the source channel.
Backup creation uses a SQLite write lock to hold off cooperating writers; file inventory and
content checks detect ordinary changes during copying. This does not lock external editors or
old binaries, which is why offline operation is required. Failed backup verification removes
only the new incomplete destination and never authorizes source cleanup.

`archive` never removes source files. `retain` considers only direct files in `files/` and
ULID-named `harness/<role>/*-stdout.log` / `*-stderr.log`. A reference from any message (including
malformed records skipped by readers), clan state, clan configuration, plan text or round log
protects a candidate. Filename mentions are deliberately conservative. Referenced full logs
and all history remain; this command does not bound retained message/log history automatically.
Only old, unreferenced candidates are removed, after a complete verified backup. If cleanup
is interrupted, the backup remains and a fresh dry run reflects what is left.

To restore, stop writers and copy the backup into a **new** `channels/<name>` directory under
an offline home. Verify the manifest's SHA-256 hashes before opening it with ratel. Message IDs,
append order, pins, cursors and runtime state are preserved. Worktree paths and external provider
transcripts still refer to their original locations; a data restore does not recreate or launch
those processes. Keep the backup until the restored data has been checked. Do not overwrite an
existing active channel to restore it.


## Synthetic demo

`demo --home PATH [--channel NAME]` creates six sample messages, a pinned task plan, threaded
review and local attachments. `PATH` must be a new directory; even an empty existing directory
is refused. `--home` is mandatory: neither `RATEL_HOME` nor the default live home is selected
implicitly. The channel defaults to `harbor-demo`. The JSON report contains `home`, `channel`,
`messages` and the root `thread` ID. No credentials, providers or clan processes are used.
Run `ratel-board --home PATH` or `ratel-tui --home PATH` to browse the result. Repeating the
command requires a new path; there is no overwrite or reset flag.

## Terminal console

`ratel-tui [--home PATH] [--channel NAME] [--no-persist-colours]` opens one channel in the
terminal. It is a read-only twin of the board that reads the channel in-process through
`Bus`: no `ratel-board` process, no HTTP, no token.

**Invocation.** Flags win over the environment. `--home` falls back to `RATEL_HOME`, then
`~/.ratel`; `--channel` falls back to `CHANNEL`, then the channel with the newest message.
Inside a clan role's shell both variables are already set, so a bare `ratel-tui` opens that
clan's channel. Two conditions exit 1 with a message on stderr and nothing on the screen: a
home with no channels prints `ratel-tui: no channels in <home>.` followed by a
`ratel demo --home <home>/demo` hint, and a `--channel` that does not exist lists the known
channels. The console runs until `q`; it prints no JSON.

**Read-only guarantee.** Every read goes through `Bus(home, channel, read_only=True)` and uses
only `history`, `read_since`, `pins`, `clan_heads`, `read_thread`, `presence` and `summary`.
The package never calls `consume`, `wait_for_new`, `set_cursor` or `touch_cursor`, a test greps
`ratel/tui/` for those four names as plain substrings, and another proves a read-only reader
creates no `files/` or `plans/` directory and no database for a legacy or missing channel. An
agent's cursor, and therefore its unread state, is untouched by anything the console does.

**Live.** Messages are polled with `read_since(cursor, 200)` every 0.5 s from the page tip;
presence and the channel list every 10 s; pins are refetched only when a batch carries a truthy
`pin` or an `unpin`. A live arrival gets a **NEW** divider (before the first one while the cursor
is not on the last row) and a brief flash; the view follows only when already at the bottom.
A read failure shows `storage unavailable — retrying in Ns` in the top bar, fixed text and never
exception text, and the loop retries after 1, 2, 4, 8 and then every 10 s until a read succeeds,
when the top bar returns to `live`. The initial page retries after 1 s under the same text.
Switching channels bumps a generation counter and cancels the workers; a late result from an
earlier generation is dropped.

**Layout.** From 110 columns: sidebar (24, channels newest-activity first with count and repo,
then the presence dots of the open channel, `●` online and `○` offline), pins strip (one line
collapsed), timeline, thread panel (40). Below 110 columns the sidebar is hidden (`s` overlays
it) and a thread opens as its own screen. Top bar: channel · `tasks N/M` (from the newest
pinned task list) · `live` or the storage text. Status bar: selected message id · active filter
(`q:<text>`, `@agent`, `operator`, or `no filter`) · last action.

**Keys.**

| Key | Action |
|---|---|
| `j`/`k`, arrows | move |
| `g` / `G` | first / last row |
| `Enter` | open the thread of the selected message (a reply opens its parent's thread); in the sidebar, select the channel |
| `Esc` | close the sidebar overlay, the thread, or the open modal |
| `Tab` | cycle focus |
| `n` / `p` | next / previous channel |
| `1`–`9` | jump to the n-th channel in the sidebar |
| `s` | sidebar (overlay below 110 columns) |
| `t` | toggle the thread of the selected message |
| `P` | expand the pins strip: the newest pin in full plus the older list |
| `o` | message modal: header, full text as block Markdown, every attachment in full |
| `a` | attachment picker, then preview |
| `/` | filter modal: text (≤200 chars), `@agent`, operator-only; Enter applies, Esc keeps the current filter |
| `m` | cycle the mention filter over the agents seen on the channel, then off |
| `O` | toggle operator-only |
| `[` | load the older page (`next_before`) |
| `N` | jump to the NEW divider and clear it |
| `r` | reload the channel list and the current page |
| `?` | this key map |
| `q` | quit |

Filters run server-side through `Bus.history`, exactly as the board's history route: `/`, `m`
and `O` each reload the page. Timeline rows render inline markup only (bold, emphasis, code,
strike, `@name` in that agent's colour, fences cut at 6 lines with `… +N lines (o)`); block
Markdown appears only in the `o` and `a` modals, capped at 512 KiB, with links inert. A preview
opens only a `file` attachment under `files/` whose mime is `text/markdown` or `text/plain` or
whose name ends in `.md`, `.txt` or `.log`; `text/plain` shows verbatim, the rest as Markdown.
Any other file shows name · mime · pages · ref and its bytes are never read. Message and
attachment text are scrubbed of ANSI escapes, control characters, Unicode bidi controls and
Unicode line separators before they reach the screen. Links render as text and the console opens
nothing; an `http(s)` link attachment whose URL contains no whitespace also carries a terminal
hyperlink to exactly the visible text, so the click target can never differ from what is shown.

**Colour slots.** Agents are coloured from the board's eight-slot palette in first-seen order
per channel, persisted to `$RATEL_HOME/tui.toml` so a later agent never re-colours an earlier
one:

```toml
# ratel-tui colour slots: first-seen agent order per channel. Safe to delete.

[slots."harbor-demo"]
"orchestrator" = 1
"developer" = 2
```

The file is written atomically after each new assignment. A malformed file, or one without a
`slots` table, starts a fresh map that the next assignment overwrites; inside a valid file every
entry that is not `valid channel → valid agent → positive integer` is dropped. A write failure
is ignored so a read-only home still gets a console. `--no-persist-colours` neither reads nor
writes the file: slots live in memory for that run.
