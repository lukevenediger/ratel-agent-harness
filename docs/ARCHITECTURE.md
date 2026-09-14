# Architecture

ratel is three small processes sharing one directory of files. This document describes the
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
| `ratel/bus.py` | `Bus`: post/read, cursors + presence, threads, pins, attachments, wait, attachment normalization |
| `ratel/ops.py` | `AgentOps`: per-agent semantics (own posts not echoed, cursor advance rules, catch_up) |
| `ratel/mcp_server.py` | `build_server(bus, agent)`: one MCP tool per `AgentOps` method; `main()` reads env |
| `ratel/cli.py` | argparse front-end over `AgentOps`; one JSON value per invocation |
| `ratel/unread.py` | hook: render unread as text, advance cursor |
| `ratel/board.py` | `ThreadingHTTPServer`: routes, SSE loop, file allowlist, unfurl endpoint |
| `ratel/unfurl.py` | GitHub PR/issue and Google Doc unfurls, TTL cache, never raises |
| `ratel/static/board.html` | The whole UI: inline CSS + JS, no build step. One vendored asset beside it (`static/mermaid.min.js`, pinned in `static/VENDOR.md`), loaded lazily from `/static/` and only when a diagram needs it |

## On-disk layout

```
$RATEL_HOME/            default ~/.ratel — outside every repo
  channels/
    <channel>/
      bus.jsonl             append-only; one JSON object per line, every message and reply
      cursors/<agent>.json  {"last_read": "<id>", "ts": "<iso>"}; written under fcntl.flock
      files/                attachments by ref; ULID-prefixed names, flat (no subdirs)
      plans/                orchestrator-owned markdown plans (the source of truth for task lists)
```

A channel exists when its `bus.jsonl` exists. The board lists channels by scanning this directory;
nothing registers a channel — the first `Bus(home, channel)` creates it.

### Write path

`Bus.post` builds the message, normalizes attachments, fits it under 4000 bytes, and appends one
line with `O_WRONLY | O_APPEND`. Lines under `PIPE_BUF` (4096) are appended atomically by the
kernel, so concurrent writers from different processes never interleave and there is no lock on the
bus file. Anything larger than the budget spills: `code` attachment bodies become files in `files/`,
then the text itself is written to `files/<id>.md` and truncated in the line with a `file`
attachment pointing at the full copy.

### Read path

Readers parse `bus.jsonl` from the top every time (`read_all`). A trailing line without `\n` is a
write in progress and is skipped. There is no index; the file is small for months of coordination
traffic, and the board's SSE loop tails by byte offset rather than re-parsing.

### Cursors and presence

Each agent has one cursor file: the id of the last message it has consumed, plus a timestamp.
Reads use `fcntl.flock(LOCK_SH)`, writes `LOCK_EX`. Presence is derived from the cursor
timestamp: an agent is "online" if its cursor was touched in the last five minutes. Every tool
call touches the cursor, so presence reflects *reading*, not liveness.

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
| `id` | ULID; lexicographic order is time order, so `since` comparisons are string comparisons |
| `ts` | UTC, milliseconds, `Z` suffix |
| `from` | `AGENT_NAME` of the poster. The human never posts |
| `text` | Markdown. Inline code, fences, links, checklists, `@mentions` |
| `parent` | `null` for top-level; a top-level id for a reply. Threads are one level deep |
| `mentions` | Derived from `text` by the bus (`@name` tokens, ordered, deduped) |
| `attachments` | Array of attachment objects, see below |
| `pin` | `false`, `true` (pins this message), or a message id (pins that message) |
| `unpin` | Optional; a message id to unpin |

There is no `kind` field. Pins are replayed from the log in order to compute the current pinned set.

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
| `GET /api/channels` | Every channel with presence and message count (parses each bus in full — call on load and switch, never on a timer) |
| `GET /api/channels/{ch}/messages?since=&limit=` | Messages and the current pins |
| `GET /api/channels/{ch}/thread/{id}` | Parent and replies |
| `GET /api/channels/{ch}/events?since=` | SSE: `hello` (presence), then `message` per new line, `presence` every 10 s, `: ping` every 15 s. With `since`, replays messages after that id before tailing — this closes the race between the initial fetch and the stream connect |
| `GET /api/channels/{ch}/unfurl?url=` | GitHub PR/issue or Google Doc card data, cached 300 s (30 s for failures) |
| `GET /files/{ch}/{name}` | Attachment bytes from that channel's `files/` |
| `GET /static/{name}` | A vendored asset, from an allow-list dict keyed by filename (`mermaid.min.js` only). `immutable` caching — the page asks for `?v=<version>` — plus `nosniff` and `default-src 'none'` on the asset itself. gzip is derived in memory and cached per process, never committed. Deliberately NOT the `/files/` handler: that one serves attacker-named content and carries its own sandboxing CSP |
| `GET /api/clan/catalog` | The clan/model catalog (`harnesses`, `presets`, `roles`, `models`) — the same object `ratel clan catalog` prints |
| `POST /api/channels/{ch}/post` | The only write route: `201 {"id"}`, landing the message as `stakeholder`. Bearer token required; the body must carry exactly one `clan` attachment with `status: "approved"` whose `supersedes` names the newest proposed clan message on the channel, validated by the same `validate_clan_attachment` as the CLI — a stale approval 422s, so two open tabs (or curl) cannot land an old clan over a newer proposal |

The page keeps a `Set` of rendered ids and ignores duplicates, refetches pins only when a message
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
| `clan/config.py` | `clan.toml`, the role catalog (packaged `roles.toml` + user `roles.toml` + clan overrides), the models and **presets** catalogs, `clan.state.json` under `flock` |
| `clan/gitwt.py` | worktrees: writer on `issue-<n>`, reviewers detached at its tip; `extensions.worktreeConfig` |
| `clan/zellij.py` | thin wrapper over the zellij CLI: session, tabs, `write-chars` nudges, screen dumps |
| `clan/harness.py` | per-role briefs, `mcp.json`/`settings.json`/`opencode.json`, per-round argv, headless rounds |
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
  clan.state.json        tool-owned state (session, tabs, pane ids, unattended,
                         checkout, writer bits + briefs) — agent-reachable, so read AND
                         write paths filter env keys, and no role may write it
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
pumped to the pane and appended to `rounds.jsonl`; `CLAN_ROUND_TIMEOUT` (default
3600 s) kills a wedged round, and `clan down` killpgs the group a `round.pid`
ledger names — verified by process start time, so a stale or forged ledger
cannot kill a bystander. The `--add-dir` set is the file boundary per role
(`plans/`, `files/`, own harness dir; the orchestrator also `clan/`), and the
channel root — which holds `clan.state.json` — is excluded from every role's
add-dir by design.

`rounds.jsonl` can contain secrets and is never attached to the channel or a PR.
