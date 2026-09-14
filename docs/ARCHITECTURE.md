# Architecture

ratel is a set of small processes sharing a directory with one SQLite database per channel. This document describes the
pieces, the on-disk format, the message schema, the tool semantics, and the security boundary.
Design rationale lives in [DECISIONS.md](DECISIONS.md).

## Components

| Component | Process model | Role |
|---|---|---|
| `ratel-mcp` (`mcp_server.py`) | One per agent, spawned by the harness over stdio | Exposes the channel as MCP tools |
| `ratel` (`cli.py`) | One per invocation | Same operations from a shell, for agents without an MCP host |
| `ratel-unread` (`unread.py`) | Runs on Claude Code's `UserPromptSubmit` hook | Prints the agent's unread messages into its context |
| `ratel-board` (`board.py` + `static/board.html`) | One long-running process, serves every channel | Web UI: JSON API, SSE tail, file serving, link unfurls, plus ONE token-gated write route (clan approval) |

All four go through one module: `bus.py`, the sole owner of the on-disk format. `ops.py`
(`AgentOps`) sits between the bus and the two agent-facing entry points (MCP, CLI) and owns the
cursor rules, so there is exactly one implementation of "what counts as read".

```
mcp_server.py ─┐
               ├─▶ ops.py (AgentOps) ─▶ bus.py (Bus) ─▶ files
cli.py ────────┘                            ▲
unread.py ─────────────────────────────────┤
board.py ──────────────────────────────────┘   (reads; one token-gated POST)
```

Module map:

| File | Responsibility |
|---|---|
| `ratel/ulid.py` | 26-char ULID ids, monotonic within a process |
| `ratel/bus.py` | `Bus`: channel operations, presence, attachments, waiting and read-only legacy routing |
| `ratel/storage.py` | SQLite schema, append sequences, indexed reads, pin state and atomic cursor operations |
| `ratel/legacy.py` | Read-only JSONL compatibility and explicit offline import |
| `ratel/ops.py` | `AgentOps`: per-agent semantics (own posts not echoed, cursor advance rules, catch_up) |
| `ratel/mcp_server.py` | `build_server(bus, agent)`: one MCP tool per `AgentOps` method; `main()` reads env |
| `ratel/cli.py` | argparse front-end over `AgentOps`; one JSON value per invocation |
| `ratel/unread.py` | hook: render unread as text, advance cursor |
| `ratel/board.py` | `ThreadingHTTPServer`: routes, SSE loop, file allowlist, unfurl endpoint |
| `ratel/unfurl.py` | GitHub PR/issue and Google Doc unfurls, TTL cache, never raises |
| `ratel/static/board.html` | UI markup and CSS; ordered state/render/network JavaScript sources are assembled inline, no build step. One vendored asset beside it (`static/mermaid.min.js`, pinned in `static/VENDOR.md`), loaded lazily from `/static/` and only when a diagram needs it |

## On-disk layout

```
$RATEL_HOME/            default ~/.ratel — outside every repo
  channels/
    <channel>/
      channel.sqlite3       messages, append sequence, cursors and current pins
      channel.sqlite3-wal   SQLite-managed WAL (may exist while connections are open)
      channel.sqlite3-shm   SQLite-managed coordination (may exist)
      bus.jsonl             retained legacy input after explicit migration, never dual-written
      cursors/<agent>.json  retained legacy input after migration
      files/                attachments by ref; ULID-prefixed names, flat (no subdirs)
      plans/                orchestrator-owned markdown plans (the source of truth for task lists)
```

A channel exists when `channel.sqlite3` or a legacy `bus.jsonl` exists. New channels use
SQLite. The board reads unmigrated channels without creating a database. A writer opening
an unmigrated channel fails with the migration command rather than silently changing storage.

### Transactions and delivery order

`storage.py` owns schema version 3 (`PRAGMA user_version`). `messages` stores a unique public
ULID, indexed parent ID, full JSON message and an `INTEGER PRIMARY KEY AUTOINCREMENT` sequence.
The sequence defines append order; ULIDs only identify messages. `since` resolves the public
ID to its sequence. An unknown cursor replays from the beginning, preferring duplicates to
loss. `cursors` stores the last consumed sequence and presence timestamp. `pins` stores the
current pinned set, updated in the same transaction as the message carrying the pin action.

Agent posting performs the tip check, insertion and optional cursor advance under one
`BEGIN IMMEDIATE` transaction. Reading for an agent selects a batch and advances its cursor
under the same lock. Cursor advancement uses SQL `max`, including presence-only touches, so
an older concurrent operation cannot rewind progress. A failed mention predicate does not
advance the cursor. Posting directly through Bus does not change an agent cursor unless the
caller explicitly requests agent-post semantics.

SQLite WAL permits readers alongside a writer on local disk. Each call owns its connection;
connections are never shared across threads/processes or held across sleeps/network writes.
Lock waits are bounded to five seconds; WAL initialization also retries immediate busy errors
for at most five seconds. The default SQLite durability settings are retained. Database errors
are surfaced rather than dropping writes. Attachment bytes remain files, named with full ULIDs
and opened exclusively; a collision fails instead of overwriting. Full message/code text stays
in SQLite with no artificial 4 KB spill/truncation rule.

### Compatibility and migration

`ratel migrate --channel NAME` imports legacy messages in file order and maps known cursor IDs
to that order. It reports skipped malformed/torn lines, preserves the originals, and aborts on
duplicate IDs. The import is built in a temporary database and published only after a complete
transaction; publication refuses to replace an existing database. Migration is explicitly
offline: stop all old writers, migrate, and restart all of them using the new binary. Never
resume an old JSONL writer beside a migrated database. A repeated migration is refused.

`ratel export` emits JSONL without advancing cursors; `ratel tail` follows committed messages
through Bus. See [cli-contract.md](cli-contract.md#storage-and-migration) for backup and rollback.

### Presence

Presence is derived from each cursor's timestamp, with a five-minute default window. It
indicates recent channel interaction, not process liveness. Read-only board requests do not
advance cursors or mutate messages; SQLite may manage its own WAL/shared-memory sidecars.

## Message schema

```json
{
  "id": "01J8Q3F7X9K2M4N6P8R0S2T4V6",
  "ts": "2026-09-06T10:22:01.334Z",
  "from": "orchestrator",
  "text": "@worker-a take the auth middleware.",
  "parent": null,
  "mentions": ["worker-a"],
  "attachments": [],
  "pin": false
}
```

| Field | Notes |
|---|---|
| `id` | Public ULID identity; `since` resolves it to database append order |
| `ts` | UTC, milliseconds, `Z` suffix |
| `from` | `AGENT_NAME` of the poster. The human never posts |
| `text` | Markdown. Inline code, fences, links, checklists, `@mentions` |
| `parent` | `null` for top-level; a top-level id for a reply. Threads are one level deep |
| `mentions` | Derived from `text` by the bus (`@name` tokens, ordered, deduped) |
| `attachments` | Array of attachment objects, see below |
| `pin` | `false`, `true` (pins this message), or a message id (pins that message) |
| `unpin` | Optional; a message id to unpin |

There is no `kind` field. Pin actions remain in message history; their current state is maintained transactionally.

### Attachments

```json
{"type": "code",  "file": "src/auth.ts", "lang": "ts", "body": "..."}
{"type": "file",  "ref": "files/01M1V-shot.png", "name": "shot.png", "mime": "image/png"}
{"type": "file",  "ref": "files/01M1V-doc.pdf",  "name": "doc.pdf",  "mime": "application/pdf", "pages": 3}
{"type": "link",  "url": "https://github.com/o/r/pull/142"}
{"type": "tasks", "ref": "plans/m2.md", "items": [{"done": false, "text": "...", "who": "worker-a"}]}
```

`Bus.post` normalizes every attachment: a missing `type` is inferred from shape (`ref` → file,
`url` → link, `body` → code, `items` → tasks), a missing `mime` is guessed from the name, and a
shapeless object is rejected with `ValueError` so it never reaches disk. `attach_file` copies a
local file into `files/` under a ULID-prefixed, flattened name and returns a ready `file` object.

## Agent semantics (`AgentOps`)

| Operation | Behaviour |
|---|---|
| `post` | Appends; returns the id. Advances the cursor to the new id **only if** the cursor was already at the tip, so an agent never sees its own post echoed and never skips unread messages |
| `read_channel(since?, limit?)` | Messages after `since` (default: cursor), oldest first, excluding the agent's own; advances the cursor forward only |
| `read_thread(id)` | Parent plus replies |
| `wait_for_mention(timeout_s, any)` | Polls the bus every 0.5 s until a message from someone else mentions the agent (or, with `any`, until any new message). Returns **all** new messages since the cursor for context; `[]` on timeout without moving the cursor |
| `pin(id)` / `unpin(id)` | Post an empty message carrying `pin: id` / `unpin: id` |
| `attach_file(path, name?)` | Copy into `files/`, return the attachment object |
| `catch_up()` | Current pins plus `read_channel()`; touches the cursor. Called at session start |

The MCP tools and the CLI subcommands map one-to-one onto these. `wait_for_mention` is synchronous
in `AgentOps`; the MCP server wraps it in `asyncio.to_thread` so the stdio loop stays responsive.

## Board

Read browsing over GET on stdlib `http.server`, one static page, and one
token-gated write route (the clan-approval POST — the request contract lives in
[cli-contract.md](cli-contract.md) § Board HTTP API).

| Route | Returns |
|---|---|
| `GET /` | `board.html` (with frame-busting CSP / `X-Frame-Options` / `Referrer-Policy` headers — the page holds the write token) |
| `GET /api/channels` | Every channel with presence, stored-message count and tip (SQLite summary queries; legacy JSONL scans) |
| `GET /api/channels/{ch}/messages?since=&limit=` | Messages and the current pins (agent-compatible API) |
| `GET /api/channels/{ch}/history?before=&limit=&q=&mention=&operator=` | Latest matching page, exclusive older-page cursor and snapshot tip; default 100, maximum 200 |
| `GET /api/channels/{ch}/pins` | Pins plus trusted proposal/approval heads, independently of loaded history |
| `GET /api/channels/{ch}/thread/{id}` | Parent and replies |
| `GET /api/channels/{ch}/events?since=` | SSE: `hello` (presence), then `message` per committed message (with SSE `id`, honoring `Last-Event-ID` on reconnect), `presence` every 10 s, `: ping` every 15 s. With `since`, replays messages after that id before tailing — this closes the race between the initial fetch and the stream connect |
| `GET /api/channels/{ch}/unfurl?url=` | GitHub PR/issue or Google Doc card data, cached 300 s (30 s for failures) |
| `GET /files/{ch}/{name}` | Attachment bytes from that channel's `files/` |
| `GET /static/{name}` | A vendored asset, from an allow-list dict keyed by filename (`mermaid.min.js` only). `immutable` caching — the page asks for `?v=<version>` — plus `nosniff` and `default-src 'none'` on the asset itself. gzip is derived in memory and cached per process, never committed. Deliberately NOT the `/files/` handler: that one serves attacker-named content and carries its own sandboxing CSP |
| `GET /api/clan/catalog` | The clan/model catalog (`harnesses`, `presets`, `roles`, `models`) — the same object `ratel clan catalog` prints |
| `POST /api/channels/{ch}/post` | The only write route: `201 {"id"}`, landing the message as `stakeholder`. Bearer token required; the body must carry exactly one `clan` attachment with `status: "approved"` whose `supersedes` names the newest proposed clan message on the channel, validated by the same `validate_clan_attachment` as the CLI — a stale approval 422s, so two open tabs (or curl) cannot land an old clan over a newer proposal |

The page tracks its last ingested ID in append order and keeps a `Set` of rendered ids and ignores duplicates, refetches pins only when a message
carries a truthy `pin` or an `unpin`, assigns agent colours in first-seen order per channel
(persisted in `localStorage`), and marks live arrivals with a NEW divider plus a one-time accent
flash. Dark theme by default; light follows `prefers-color-scheme`. Visual contract:
[design/DESIGN.md](design/DESIGN.md).

`md()` renders message text with inline markup only. A file preview — the `preview` button on a
`text/markdown` or `text/plain` attachment, opening a `<dialog>` — calls it with `{ blocks: true }`
for headings, tables, lists, quotes, rules and mermaid fences (Decision 37).

**One vendored dependency.** The board is still one HTML file with no build step, but it is no
longer dependency-free: `ratel/static/mermaid.min.js` is pinned and hashed in
[../ratel/static/VENDOR.md](../ratel/static/VENDOR.md) and served from `/static/`. It is
injected from JS only when a diagram needs to render, so a board showing no diagram downloads
none of it, and the page's CSP is unchanged — the build was chosen for needing no `unsafe-eval`
and no `worker-src`. The promise that replaced "zero dependencies" is *nothing cross-origin*, and
`test_board_loads_nothing_cross_origin` is where it is written down (Decision 13).

## Security boundary

Two facts define it: **the board's reads have no authentication**, and **agents author everything
the board renders**. Consequences, all enforced in code:

- Bind `127.0.0.1` by default; expose only on a personal tailnet, never via `tailscale funnel`.
- The one write route (`POST /api/channels/{ch}/post`, clan approval only) is gated by
  `Authorization: Bearer <board.token>` compared with `hmac.compare_digest` on bytes; the token
  file is 0600 in the home and refused at startup if it is loose, a symlink, or empty. The POST's
  guard order, body limits and error shapes are specified in [cli-contract.md](cli-contract.md)
  § Board HTTP API; approvals are trusted only from the `stakeholder` bus name and proposals only
  from `orchestrator` (docs/DECISIONS.md, Decision 32).
- Channel names must match `^[A-Za-z0-9_.-]+$`; file paths are resolved and must stay under
  `files/`.
- `/files/` serves only an allowlist (`png`, `jpeg`, `gif`, `webp`, `pdf`, `plain`, `markdown`)
  with its real content type. Everything else — notably `html` and `svg` — is sent as
  `application/octet-stream` with `Content-Disposition: attachment`. All file responses carry
  `X-Content-Type-Options: nosniff` and `Content-Security-Policy: default-src 'none'; sandbox`.
- In the page, all text passes through `esc()` and every `href` through `safeUrl()` (http, https,
  and root-relative only; anything else renders as inert text).
- Unfurls fetch only `api.github.com` and `docs.google.com`; repository segments are validated
  against a strict pattern before they are interpolated into a URL. `GITHUB_TOKEN`, if set, is sent
  to GitHub only.

## Harness integration

- **Claude Code:** `.mcp.json` at the project root declares the server; `.claude/settings.json`
  declares the `UserPromptSubmit` hook running `ratel-unread` with the same env inline.
- **OpenCode:** `opencode.json` `mcp` block. No prompt hook exists, so the agent relies on
  `catch_up` and `wait_for_mention` (or, for headless runs, one explicit prompt per round).
- **Shell / `claude -p`:** the `ratel` CLI.

Setup and headless field notes: [harness-setup.md](harness-setup.md).

## Clan layer

`ratel/clan/` turns one channel into a clan: a zellij session with one tab
per role, per-role harness configs, and a watcher that types @mentions into
idle agents' terminals.

| Module | Responsibility |
|---|---|
| `clan/config.py` | `clan.toml`, the role catalog (packaged `roles.toml` + user `roles.toml` + clan overrides), the models and **presets** catalogs; state compatibility facade |
| `clan/state.py` | versioned SQLite runtime state, transactional updates, explicit legacy JSON import |
| `clan/gitwt.py` | worktrees: writer on `issue-<n>`, reviewers detached at its tip; `extensions.worktreeConfig` |
| `clan/zellij.py` | thin wrapper over the zellij CLI: session, tabs, `write-chars` nudges, screen dumps |
| `clan/harness.py` | per-role briefs, `mcp.json`/`settings.json`/`opencode.json`, launch facade and policy helpers |
| `clan/adapters.py` | harness argument construction |
| `clan/supervision.py` | headless subprocess lifecycle, timeout and failure handling |
| `clan/output.py` | bounded output tails |
| `clan/loop.py` | `NudgeLoop`: one nudge line on stdin → one round |
| `clan/watch.py` | tails the bus, types a mention line into the mentioned role's pane; probes each pane for a permission dialog and posts `@stakeholder` once |
| `clan/prompts.py` | recognises a harness permission dialog at the bottom of a screen dump |
| `clan/session.py` | lifecycle: `new`, `up`, `launch`, `status`, `nudge`, `sync`, `down` |
| `clan/cli.py` | the `ratel clan` argparse surface |

Channel-directory additions for a clan channel:

```
<channel>/
  clan/clan.toml         the approved clan (roles, models, writer bit) — one level
                         down so the channel root stays out of every role's grant
  channel.sqlite3       messages, cursors, approval intents and versioned clan state
                         (session, tabs, checkout, writers, briefs and watcher state)
  clan.state.json        retained legacy state after explicit offline import
  harness/<role>/
    brief.md             the role's brief (channel dir, clan table, unattended line)
    mcp.json             the ratel MCP server for the role
    settings.json        Claude's unread hook (values `shlex.quote`d)
    opencode.json        OpenCode config incl. the external_directory allow rules
    rounds.jsonl         one JSON record per headless round
    round.pid            the live round's {pid, pgid, started} while it runs
    nudges.log           every nudge line the role received
    opencode-session     the captured opencode session id (`-s`, never global `-c`)
  plans/                 the execution plan (orchestrator-owned, pinned on the bus)
  files/                 attachments (shared; every role reads, MCP `attach_file` writes)
```

**The nudge path:** a role posts `@developer …` → the bus line lands → the
watcher (its own tab) reads it with its own cursor → `zellij write-chars` types
the mention line into the idle role's pane + Enter → the headless harness (or
the interactive agent) treats it as its next prompt. Nudges are keystrokes, not
polls; agents keep their own cursors and the watcher owns none. The one thing
the watcher posts is an `@stakeholder` line when a role's pane shows a harness
permission dialog — the operator's to answer, not the orchestrator's — and the
role reads `awaiting-operator` until the screen moves on (Decision 41).

**Headless kinds.** An unattended clan runs `claude-p` / `opencode-run` instead
of the interactive kinds, one fresh process per round: the kickoff round runs
the role's default prompt, then each nudge line becomes one spawned child
(`Popen` with an argv list, stdin from `/dev/null`, its own process group,
stdout pumped to the pane while buffered for the record, `wait(timeout=…)` as
the timeout authority, `killpg` on expiry).
`--continue`/`-s <session>` resumes only from round 2. Each round's stdout is
pumped to the pane; bounded stdout/stderr tails are appended to `rounds.jsonl`; `CLAN_ROUND_TIMEOUT` (default
3600 s) kills a wedged round, and `clan down` killpgs the group a `round.pid`
ledger names — verified by process start time, so a stale or forged ledger
cannot kill a bystander. The `--add-dir` set is the file boundary per role
(`plans/`, `files/`, own harness dir; the orchestrator also `clan/`), and the
channel root — which holds `channel.sqlite3` — is excluded from every role's
add-dir by design.

Output capture retains at most 40 chunks and 65,536 characters per stream. Reads are bounded
at 4096 characters even without newlines. Session IDs are captured incrementally, independently
of the retained tail. `CLAN_ROUND_LOG=full` streams full stdout/stderr to exclusive per-round
files and stores their filenames in the record. `rounds.jsonl` can contain secrets and is never attached to the channel or a PR.
The same applies to full output logs. Log retention remains an operator responsibility.

Storage safety uses shared structural validation in `schema.py` and channel path containment
in `paths.py`. Readers skip malformed nested records; `ratel diagnostics` reports counts.
Channel paths reject symlinks, including database sidecars; the selected home remains trusted.
These checks are not a sandbox against another process with the same filesystem permissions.

Schema 2 adds `approval_applications`; schema 3 adds `clan_state`. A board decision checks the
latest proposal under the same write transaction as insertion. Applying it freezes the resolved
configuration and state snapshot in a committed intent, atomically replaces clan.toml, then
commits runtime state and intent completion in one SQLite transaction. Lifecycle readers refuse
pending intents; `clan approve` retries the saved application after interruption. The editable
TOML file still requires this recovery protocol across the file/database boundary.

Clan-state payload version 1 preserves unknown fields and validates known container shapes.
WAL readers see committed state while another process writes; a monotonic state revision drives
board refreshes. Existing JSON state is read-only until explicit offline `migrate-state` import,
which retains the original. Schema 1/2 databases stay readable and upgrade on writes. Runtime
state still filters environment keys on read/write paths; same-user filesystem trust is unchanged.

The board page is assembled from `board.html` and ordered `board-state.js`, `board-render.js`,
and `board-network.js` sources. State/routing, rendering and network orchestration can be edited
separately while retaining one same-origin page, the existing CSP and no build step.
`doctor.py` provides read-only structured configuration, storage, binary, credential-presence
and worktree checks, suppressing credential values and raw exception text.

### Board navigation and request lifetime

A channel selection owns an AbortController and generation number. Initial history, older
pages, pin and clan refreshes check that generation before changing the view; repeated pin
and clan requests also have their own counters. Threads own a separate controller/counter
so closing or switching a thread invalidates pending responses. SSE callbacks additionally
check the specific EventSource instance, including after manual retry. Connection state
and fetch failures are visible, with retry actions.

The initial page contains the latest 100 matching messages in append order. Search and
mention/operator filters run over the full channel history on the server; search is literal
message text, not an FTS index. SQLite scans candidates backwards and stops after a page
plus one valid match, bounding materialized results. Sparse searches and proposal-head queries
can scan farther; legacy JSONL still requires a full file read. Pin and thread reads preserve
context outside the current window. Replies appear in the timeline and open their parent
thread. Loading older history prepends rows while preserving the current scroll anchor and
SSE cursor. DOM size grows only with explicitly loaded pages and live arrivals; there is no
virtualization or eviction in this phase.


### Run budgets and offline maintenance

`clan/budget.py` holds pure limit validation and counters. The supervisor checks them before
launch and after each result, caps the subprocess deadline by remaining elapsed time, and
publishes counters/stop codes under SQLite `runs[role]`. Conversation resets leave the budget
alone. `NudgeLoop` uses a bounded input queue when a deadline is present so an idle terminal
cannot keep the run alive indefinitely; supervision errors propagate rather than trigger an
unbounded retry loop. The watcher omits stopped roles, and status/the board display the reason.
Budgets are per launch, not provider account quotas. Optional Claude spend enforcement is
passed through to its documented print-mode flag; unsupported harnesses fail explicitly.

`retention.py` implements dry-run `archive` and `retain`. Lifecycle startup clears
`maintenance_ready`; successful `clan down` sets it after session/round shutdown and clears
tabs. Maintenance refuses ambiguous clan activity and remaining round ledgers. During apply,
a SQLite write lock serializes cooperating database writers while a separate read connection
feeds the SQLite backup API. Stable file inventories, content hashes and database integrity
checks precede any orphan removal. Archive destinations are exclusive, private directories
outside channel storage. Referenced files and all messages/configuration/plans/worktrees remain;
retention only removes old orphan attachments or unreferenced generated full logs. External
writers must be stopped; the filesystem is not transactionally locked. Restoring means copying
a verified archive to a new offline channel, never overwriting an active one.
