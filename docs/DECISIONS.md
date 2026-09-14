# Design decisions

Short records of the choices that shape agentchat, in roughly the order they were made. Each one
says what was decided, why, and what it costs. Newer decisions are at the bottom.

---

**1. The channel is a file, not a service.** One append-only `bus.jsonl` per channel; no database,
no daemon in the write path. *Why:* every harness can already write a file; there is nothing to
start, back up, or authenticate for writes; `tail -f` is the debugger. *Cost:* readers re-parse
the whole file; retention is manual.

**2. Channel home lives outside every repo** (`~/.agentchat`, overridable with `AGENTCHAT_HOME`).
*Why:* one board serves all projects; worktrees don't see it; nothing leaks into git; file watchers
in the repo stay quiet. *Cost:* per-machine state that isn't versioned.

**3. Message ids are ULIDs.** *Why:* sortable strings make `since` a string comparison and give a
stable order across writers without coordination. *Cost:* two messages in the same millisecond from
different processes have random relative order.

**4. One message is one `O_APPEND` line of at most 4000 bytes.** Larger payloads spill to
`files/` and the line carries a ref. *Why:* appends under `PIPE_BUF` are atomic, so concurrent
writers need no lock. *Cost:* long messages are truncated on the line and complete only in the
spilled file.

**5. Cursors are per-agent files under `fcntl.flock`; presence is derived from cursor mtime.**
*Why:* no heartbeat protocol, and every tool call already touches the cursor. *Cost:* presence lies
for an agent that is working and not reading; it shows "offline".

**6. No `kind` field.** Messages carry no taxonomy; the poster's words say whether something is a
dispatch or a finding. *Why:* agents wouldn't agree on a taxonomy, and the board doesn't need one
to render. *Cost:* nothing to filter on except mentions and sender.

**7. Threads are exactly one level deep.** *Why:* replies to replies are how coordination gets
lost; a thread is a conversation about one top-level message. *Cost:* revisited if agents start
nesting; the board assumes it.

**8. `mentions` are derived server-side from `text`.** *Why:* posters shouldn't have to maintain
a parallel field, and `wait_for_mention` needs a cheap filter. *Cost:* the `@name` grammar is
fixed (`[A-Za-z0-9][A-Za-z0-9_-]*`, not preceded by a word character, `@`, or `.`).

**9. `pin` may be `true` or a message id; `unpin` names an id.** An extension of the spec, which
only had a boolean. *Why:* it lets `pin(id)` / `unpin(id)` exist as thin `post` wrappers without a
`kind`. *Cost:* pins are a replay of the log, not a stored set.

**10. An agent never sees its own posts back, and its cursor moves forward only.** `post`
advances the cursor only when it was already at the tip. *Why:* echoing your own message wastes
context; skipping unread messages because you posted is worse. *Cost:* the rules are subtle enough
that they live in one place (`AgentOps`) shared by MCP and CLI — Decision 15.

**11. `wait_for_mention` returns every new message since the cursor, not just the mention.**
*Why:* the surrounding context is usually what the agent needs to act. *Cost:* larger returns on
busy channels.

**12. The board is read-only except one token-gated write route.** No composer; the single
`POST /api/channels/<ch>/post` exists so the human can confirm a crew proposal on the board
(Decision 32), always lands as `stakeholder`, and requires `Authorization: Bearer <token>`
(`hmac.compare_digest`) where `<token>` is the 0600 `board.token` file in the home, generated on
first start and printed once as an `open once: http://…/?token=…` line — the page strips the query
parameter with `history.replaceState` after reading it. Still localhost/tailnet only, never
`tailscale funnel`. Because the page now holds the write token, `/` sends frame-busting and
content-type-pinning headers (`Content-Security-Policy` with `frame-ancestors 'none'` and
`connect-src 'self'`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `nosniff`) — defence
in depth against a future injection (issue #15). *Why:* the human's direction should go through
the orchestrator's session, not around it; auth would be a second identity system for a one-person
tool. A full identity system is still not wanted, but confirming a crew without a round trip
through the orchestrator's tab needs exactly one write, so exactly one bearer-gated write exists.
*Cost:* anyone on the network reads everything; the token gates writes to every channel the home
serves, so its file is 0600, refused if loose (or a symlink, or empty — `compare_digest("","")`
would otherwise open the route), and `fsync`ed at creation.

**13. The board is one HTML file, served by `http.server`.** *Why:* no build step, nothing to
install, and the page must render agent-authored content safely, which is easier to audit in one
file. *Cost:* a 1500-line file; no framework conveniences.

*Amended (mermaid):* "no dependencies" was true and is not any more, so here is what replaced it.
The promise is now **nothing cross-origin** — the board fetches every byte it runs from its own
origin. There is exactly one vendored asset, `agentchat/static/mermaid.min.js`, pinned to a version
and a sha256 in `agentchat/static/VENDOR.md`, served from `/static/` by a handler whose allow-list
is a dict keyed by filename, and injected from JS only when a diagram actually needs to render — a
board showing no diagram downloads nothing. The page's CSP is unchanged: the build was chosen
because it needs no `unsafe-eval` and no `worker-src`, and that was verified by loading it under
the real header rather than by grepping the bundle. `test_board_loads_nothing_cross_origin` states
the promise as a test, which the old `"<script src=" not in body` assertion could not: that one
would still have passed while the page pulled a script from a CDN. *Cost:* ~3.2 MB in the repo
(990 KB gzipped on the wire), and a third-party bundle whose next upgrade has to re-check the
CSP and the renderer questions VENDOR.md lists.

**14. Agent-authored content is treated as hostile.** Text through `esc()`, hrefs through
`safeUrl()`, `/files/` inline-types allowlisted with `nosniff` and a sandboxing CSP. *Why:* two
stored-XSS holes of exactly this shape (an `.svg`/`.html` attachment served inline; a `javascript:`
link) were found by review during the build. *Cost:* SVG and HTML attachments download instead of
rendering.

**15. One implementation of agent semantics (`AgentOps`) behind both MCP and CLI.** *Why:* the
first real run needed a shell-side client for agents without an MCP host, and a second copy of the
cursor rules would drift. *Cost:* the MCP server is a thin shell; all behaviour tests target
`AgentOps`.

**16. The bus normalizes attachments and rejects shapeless ones.** Missing `type` is inferred
from shape; a missing `mime` is guessed. *Why:* a weaker model dropped the `type` key from
`attach_file`'s result in real use and the board rendered raw JSON. Forgiveness belongs in the bus,
like mention extraction; refusal (ValueError) is better than a silently mangled line. *Cost:*
an MCP caller sees a tool error on a malformed attachment.

**17. Claude Code configuration is two files, not one.** `.mcp.json` for the server,
`.claude/settings.json` for the hook. *Why:* that is where current Claude Code reads them; the
original spec's single-file block was stale. *Cost:* the hook must repeat the env inline.

**18. Board colours: 8 fixed palette slots assigned in first-seen order per channel, persisted in
`localStorage`; light theme follows the OS only.** *Why:* the agent colour is the page's primary
information channel and must not shift mid-session; a hash-based assignment re-maps when a ninth
agent joins. No theme toggle keeps the read-only page control-free. *Cost:* two browsers can
assign different colours to the same agent.

**19. Live arrivals are marked with a foreground element (accent left rule + NEW divider), not a
background tint.** *Why:* on a near-black ground no tint passes 1.4:1; measured during design
review. *Cost:* a persistent divider that must be cleared once seen.

**20. Failed unfurls are cached for 30 s, successes for 300 s.** *Why:* a GitHub rate-limit 403
should not pin a bare link card for five minutes. *Cost:* retries during outages.

**21. Headless agents get one explicit prompt per round, not an idle loop.** *Why:* in the first
real run a GLM-driven developer received a delivered mention and re-entered `wait_for_mention`
instead of acting; a fresh `opencode run` with "read thread X, fix findings 1–8" was reliable.
*Cost:* the orchestrator relaunches processes; `wait_for_mention` remains for interactive agents.

**22. The crew is a layer over the channel, not a daemon.** `crew.toml`, worktrees, tabs and
configs are created by CLI commands; the only long-lived processes are the zellij server, the
watcher tab and whatever runs in a role tab. *Why:* the thing being built must be auditable as
files (`crew/crew.toml`, `crew.state.json`, `plans/`), not as memory in a coordinator process.
*Cost:* every command re-reads state from disk under `flock`.

**23. Channel = crew = `<repo>-<issue>`.** One name for both. *Why:* `@mention` routing needs
exactly one bus per crew; a mismatch between bus and crew would split the audit trail. *Cost:* one
crew per issue; sub-work needs its own issues. *Amended by Decision 34:* the zellij session no
longer shares that name.

**24. Harness config lives outside checkouts.** Briefs, `mcp.json`, `settings.json`,
`opencode.json` are written to `<channel>/harness/<role>/`, never the repo, and worktrees get
`extensions.worktreeConfig = true` so their git settings stay worktree-local. *Why:* a crew must
not dirty the diff it exists to produce, and its configs must not be committed. *Cost:* a role's
config is not version-controlled; `crew.state.json` records the `checkout` and writer bits a
`crew.toml` would otherwise be able to rewrite unobserved.

**25. Nudges are `write-chars` keystrokes; agents keep their own cursors.** The watcher types a
mention line into an idle role's pane and moves nobody's cursor but its own. *Why:* a keystroke
works identically for an interactive agent and a headless round waiting on stdin, and one
implementation of "what counts as read" (`AgentOps`) stays authoritative. *Cost:* a nudge typed
mid-turn is lost to the terminal's line discipline; re-nudge by hand (stale escalation exists).

**26. One writer per branch; reviewers are detached and re-synced per round.** `crew up` creates
the writer's worktree on `issue-<n>` and detaches every other role at the same tip; `crew sync`
fast-forwards a reviewer before its round. *Why:* two writers on one branch cannot be reconciled
by a crew, and a stale reviewer reviews the wrong tree. *Cost:* `crew.state.json` pins the
`checkout` and each role's writer bit so a tampered `crew.toml` cannot flip them.

**27. The dispatch-issue skill ships as a plugin via `--plugin-dir`, plus an `instructions` entry
for OpenCode.** Claude loads the skill through the packaged plugin; OpenCode has no `--plugin-dir`,
so the same SKILL.md is appended to its config as an instruction. *Why:* the orchestrator must run
the identical procedure on both harnesses. *Cost:* two load paths for one file.

**28. Headless kinds relaunch per nudge, extending Decision 21.** Unattended crews map
`claude` → `claude-p` and `opencode` → `opencode-run`: one fresh process per round, `--continue`/
`-s <captured session id>` from round 2, stdin from `/dev/null`, own process group, rounds logged
to `rounds.jsonl`. The file access boundary is the `--add-dir` set (`plans/`, `files/`, own
harness dir; the orchestrator also `crew/`, which holds `crew/crew.toml`) and the channel root is
excluded from every role's add-dir by design — with `--permission-mode acceptEdits`, everything
inside an add-dir is auto-accept territory and per-path `Read()`/`Edit()` rules are inert under
both modes; `acceptEdits` is what makes writes inside the set possible at all, and dropping the
mode does not shrink the blast radius, it produces a role that cannot write anything. `Bash(…)`
and `mcp__…` rules are the boundary for commands and MCP and do narrow (the per-role git verb
split, no bare `Bash(git:*)`/`Bash(uv:*)`/`Bash(npx:*)`). Residual risks, accepted and measured:
an unattended crew role acts with the operator's `gh` credential and is not sandboxed by the allow
list — it can push to an arbitrary URL (`git push <url>`) and perform arbitrary GitHub mutations
on any repository the token can reach (`gh api graphql`, `gh pr … -R`). It cannot run arbitrary local interpreters.
Run unattended crews only against a sandbox repo with a scoped token. A writer with edit +
`git commit` + `pytest` is arbitrary execution by construction; an unattended OpenCode role keeps
`bash: allow` and is likewise not a containment boundary. No role can read or write another
role's harness dir, or the channel root that holds `crew.state.json` — cross-role isolation is a
property of the directory set, not a convention. Workspace trust is pre-accepted by
writing `~/.claude.json` (which Claude Code itself locks against no one — a racing write can be
lost; accepted) under `flock` on a sidecar lock with an atomic replace. *Why:* eleven real sandbox
runs showed every alternative — moved HOME, interactive launches, rule-based confinement — fails
in a way a human then has to notice. *Cost:* the boundary is only as good as the directory set
and the token's scope; both are operator choices recorded here.

**29. `crew new` refuses under `CREW_NO_NESTED=1`.** A headless probe with the
operator's real environment once spawned a nested crew on the live zellij server (issue #6).
`session.new` now refuses at the top when `CREW_NO_NESTED=1` is set, before any filesystem or
zellij work; headless tests set it via the `no_nested_crews` fixture. It is a mistake barrier
for a misled agent, not a containment boundary: a hostile role with shell can unset it
(`env -u CREW_NO_NESTED …`) — the probe brief, the brief-keyed allow list and the teardown
fixtures are the layers that close the incident. *Cost:* an operator could
set the variable by accident — the error names it.

**30. Context is disposable — clear at boundaries, compact on threshold.** A
role's context window is working memory, not an archive: the orchestrator
clears a role at task boundaries (`crew checkpoint <role>` — `/clear` for
claude, `/new` for opencode, a headless reset marker for the `-p`/`run`
kinds), and the watcher compacts (`/compact`) an idle role whose measured
tokens cross `checkpoint_at` (default 200 000, per-role override in
`crew.toml`). Measurement reads each harness's own usage record — Claude's
transcript for the tab's pinned `--session-id` under `~/.claude/projects`
(sum of `input_tokens + cache_creation + cache_read` on the last assistant
record; the last turn's output is not summed, so a role can read up to one
max-output under its true window), OpenCode's last completed assistant row
in its sqlite — and the zellij environment is scrubbed of the launching
Claude session's identity
(`CLAUDECODE`, `CLAUDE_PID`, and the session/messaging members of
`CLAUDE_CODE_*`; provider configuration such as `CLAUDE_CODE_USE_BEDROCK`
is not identity and stays), because inherited identity
makes every role tab a child session that writes no transcript of its own
(measured on a live crew). Ambiguous measurements are `None`, never a stale
sibling's number. `/clear` (and `/new`) rotate the session file, so a clear
checkpoint re-pins the measurement by launch time — the tab's `session_id`
is dropped and `launched` becomes the checkpoint timestamp; the pin is not
permanent across a clear, and discovery then reads exactly the post-clear
session. *Why:* a 200k window silently degrades a role long before
it fills; the live crew showed a role reading another session's 338 k
transcript when identity leaked. *Cost:* a cleared role must re-orient
(catch_up + re-read the pins) — that cost is paid only at boundaries, and a
`checkpoint_at` set too low makes the watcher compact mid-flow.

**31. The models catalog declares what each provider accepts; a preset carries the raw word.**
`agentchat/crew/models.toml` (deep-merged with a user `$AGENTCHAT_HOME/models.toml`) owns, per
model id: which harnesses can run it, the provider id, the required env vars, the expiry date, the
OpenCode provider fragment, and **`efforts` — the list of effort words that provider itself
accepts**. `harness.py` carries no model constants: `write_configs` builds an OpenCode role's
`provider` block only from its model's catalog entry, and Claude kinds get `--effort <word>` from
the same entry. `effort_word()` passes a word through when the model declares it and returns
`None` otherwise, so an effort a model does not accept emits no flag at all. `crew up` refuses to
start when a role's model needs an env var that is not set (`missing_env`), and `crew status` warns
when `expires` is past — keys are read from the environment, never written to disk.

*Superseded:* effort used to be a LEVEL (low/medium/high) and each model entry carried a table
mapping that level onto the provider's vocabulary (`{ low = "low", medium = "high", high = "max" }`
for DeepSeek). That map is gone. It could not survive presets: `deepseek-flash-high` and
`deepseek-flash-max` are two curated setups whose whole difference is the word, and both would have
resolved to `max` through the old table, silently. The level vocabulary (`EFFORTS`) and
`[defaults] effort` went in the same commit as the presets that replaced them, because the two
schemes cannot coexist for even one commit without one of them lying.

The GLM measurement stands and is now attached to the preset rather than the role: `or-glm-flash-low`
pins GLM 5.3 Flash at `low` because GLM's reasoning is mandatory and `medium` starves the code out
of its output cap (measured, docs/harness-setup.md). No role defaults to GLM any more — `docs`
moved to `or-deepseek-flash-high` — so that finding stays true without having to be worked around.

*Why:* model choice was hardcoded (`GLM_PROVIDER`) and switching to DeepSeek meant editing Python;
expiry must be visible before a crew's tabs go dark. And a level map is a lie as soon as two
setups differ only by the provider's own word. *Cost:* the catalog is a third TOML layer beside
`roles.toml` and `crew.toml`; a free-text model outside it gets no provider block and no env check;
and a model that declares no `efforts` silently sends no effort flag, which is the right default
but is invisible unless you read the entry.

**32. `crew.toml` is written only by `crew approve` / `crew new` — never by hand.** The proposal
travels as a `crew` attachment posted by `crew propose` (trusted only from the `orchestrator` bus
name), the human edits and Confirms it on the board (or the operator runs `--auto-approve`), and
the confirmation posts as `stakeholder` (trusted only from that name) via the token-gated POST or
the operator's own `crew approve`. Both `crew approve` and the board POST refuse an approval whose
`supersedes` is not the newest `proposed` crew message on the channel — same rule and wording in
all three places — so a stale confirmation cannot land an older crew over a newer proposal.
Approvals and proposals are keyed to the bus name, not to a person: the MCP server binds the name,
so an MCP-restricted role cannot forge either, but a role with a shell can
(`agentchat post --agent stakeholder`) — the same residual Decision 28 accepts. *Why:* the
orchestrator hand-writing `crew.toml` was an unlogged, unvalidated write of the control plane;
the board route made a forged approval one POST away from flipping the writer bit. *Cost:* the
`stakeholder`/`orchestrator` names are now security-relevant and must not be reused for
convenience; amending a crew means a re-propose, not an edit.

**33. The issue is the plan — no approval artifact gates a dispatch.** `dispatch-issue` no longer
refuses on a missing `plan-approved` label or a missing `<!-- plan-run -->` comment. The
orchestrator reads the issue body and its comments (newest wins where they disagree), and that is
the brief; a `Footprint:` line is read when present and derived (and declared in the proposal)
when it is not. The remaining guards are unchanged and are all about *contention*, not readiness:
`needs-spec` / `needs-operator`, the `dispatched` lock, an open PR, open sub-issues. Session end
now only removes the assignee — there is no label left to consume, so the assignee is the sole
mid-build marker. *Why:* the two checks encoded how-we-work's planning ritual into the dispatcher,
and every issue written outside that ritual — most of them — was refused as "not build-ready" for
a plan that was sitting in the body all along. *Cost:* an underspecified issue now reaches a crew
instead of bouncing at the gate; the orchestrator's own read of the plan is the only filter, and a
sweep for unstarted work has one marker (the assignee) instead of two.

**34. The zellij session name is stamped, the channel name is not.** `crew new` opens
`<channel>-<MMDD-HHMM>` (a `-2`, `-3` … counter breaks a same-minute tie, and 100 collisions is a
hard exit) and records it in `crew.state.json`; every later command drives the session from that
record, and `crew new` prints the `zellij attach` line. The channel keeps `<repo>-<issue>` — it is
the bus, the board's identity, and what `--channel` addresses. *Why:* zellij keeps an EXITED
session listed for `attach` to resurrect, so its name stays taken long after the crew behind it is
gone; `crew new` refused ("a zellij session named X already exists") and a restart on the same
issue was impossible without deleting the predecessor by hand. *Cost:* the attach name is no
longer guessable from the issue number — read it from `crew new`, `crew status` or
`crew.state.json`. Dead sessions also accumulate on the zellij server instead of being
overwritten; `crew down` deletes the one it owns, and the rest are the operator's to prune.

The name must also fit a budget, which is the sharp edge of this decision: zellij binds its IPC
socket at `$TMPDIR/zellij-<uid>/contract_version_1/<session>` and a unix socket path caps at 103
bytes. macOS `$TMPDIR` is ~49 bytes, leaving ~24 characters for the whole name — `example-
projects-1-0910-2041` is 28 and zellij refuses to start ("the IPC socket path is too long"). So
`session_name_budget()` computes the room from the environment the session will actually run under
(not `os.environ` — a test's isolated env moves `TMPDIR`), and the order of sacrifice is: the
stamp's date first (`0910-2041` → `2041`), the channel's head last, with a stderr line whenever a
name is shortened. Trimming takes the head because the tail carries the issue number, which is
what tells two crews on one repo apart. A `$TMPDIR` with no room at all is a hard exit, not a
truncation to nonsense.

*Test hazard this uncovered:* `isolated_env` moves TMPDIR (sockets) but leaves HOME real, so
`~/.cache/zellij` is shared — `zellij list-sessions` on a sandbox server also lists the operator's
resurrectable sessions. A teardown that deletes the whole listing destroys their resurrect data.
`ZellijSandbox.own_sessions()` filters by the sandbox's own name prefix, and
`tests/test_conftest_fixtures.py` holds it there.

**35. A preset is the unit of role setup, not harness + model + effort.** A role picks ONE id from
`agentchat/crew/presets.toml` (deep-merged with `$AGENTCHAT_HOME/presets.toml`, exactly as the
models catalog is), and that id carries the harness, the model and the provider's own effort word
together. The board's crew card shows one `setup` dropdown per role instead of three controls and
an `other…` escape hatch. Preset ids are stable slugs, never ordinals — `order` is display order
only, so inserting a preset cannot silently re-point an existing role. `load_catalog` expands a
role's preset AFTER the defaults merge and the preset BEATS any sibling `harness`/`model`/`effort`
key, because a half-overridden preset is an incoherent role that would only fail later at the
model/harness compat check. `crew.toml` and CLI overrides still win, since they merge after
expansion — `crew new --orchestrator-model X` keeps working.

*Why:* the three keys were independently choosable and most combinations were wrong; a curated list
is a shorter thing to get right, and it is the only shape in which the raw provider effort word can
travel without a mapping layer. *Cost:* a combination nobody curated is a `presets.toml` edit
rather than two clicks on the card. Two presets can still resolve to the same MODEL
(`opus-medium` and `opus-high`), so "the builder and a reviewer are never the same model" is now
*easier* to break by accident and is called out in the dispatch-issue skill as a rule the validator
does not check.

**36. A crew attachment carries a preset id, not a setup.** A role in the `crew` attachment is
`{name, preset, writer, skills, why}`. Harness, model and effort are gone from the wire: the
preset supplies all three, and `crew_config_from` re-resolves the id against the catalog at
APPROVE time, not propose time. Four validation rules — harness membership, model charset, effort
membership, model↔harness compatibility — collapse into one membership test against the catalog's
preset ids, in Python and again in the page's mirror of it.

*Why:* those four rules existed twice, in two languages, and had to agree; a bus message is hostile
input, and the smallest thing it can say is the safest thing for it to say. The
printable-single-line guard on a model id moved with the value it protects — a model id now arrives
from a TOML file rather than from the bus, so `CrewConfig.validate` holds that guard on the way
into `crew.toml` instead of `validate_crew_attachment` holding it on the way off the wire.
*Cost:* an attachment is no longer self-describing — reading an old pinned proposal tells you
`opus-high`, not which model that was on the day it was posted, and re-approving it resolves
against today's catalog. That is deliberate (the catalog may have changed for a reason), but it
means a proposal is not an archive.

**37. The board renders block markdown only where a human asked for it.** `md(text, opts)` gains
headings, tables, ordered lists, blockquotes, horizontal rules and mermaid diagrams behind
`opts.blocks`, and message text does not pass that flag. *Why:* agents write `---` separators and
`# ` shell comments and quote CLI output with `>` constantly, and the bus is append-only — turning
blocks on for message text would retroactively restyle every message ever posted. Inline emphasis
(`*italic*`, `~~strike~~`) IS on for messages: low-noise, and already common in what agents write.
A file preview opts in; a mermaid fence in a message renders only after a tap, so an agent posting
a diagram cannot pull a 990 KB bundle onto someone's phone. *Cost:* two rendering modes to keep in
step, which is why `tests/test_board_md.py` runs the real `md()` under node over a golden corpus in
both modes rather than asserting string needles at it.

**38. The orchestrator is never cleared, and no role checkpoints itself.** `crew checkpoint
orchestrator --mode clear` is refused with no override; `crew checkpoint <role>` run by that same
role (`AGENT_NAME == role`) is refused without `--force`; the watcher's threshold compaction skips
the orchestrator. The skill and the brief, which used to say "and `checkpoint orchestrator` for
yourself", now say the opposite. *Why:* the orchestrator's context is the run's state — every other
role is disposable between tasks, this one is not. And a checkpoint is delivered by typing `/clear`
into the role's pane and typing a re-orient line two seconds later; a role running that against
itself is typing into a pane that is mid-turn, the lossy case Decision 25 accepted for nudges. When
`/clear` lands and the re-orient does not, the orchestrator sits at a blank prompt that nobody will
ever @mention — which is what happened, seven self-clears into a four-hour run. The watcher had the
same blind spot from the other side: it skips roles in `nudged`, and the nudger is never in
`nudged`, so the orchestrator always looked idle. *Cost:* the orchestrator's context only shrinks
when the operator runs `--mode compact` against it, so it has to stay small by discipline — pipe
verification through `tail` and `wc -l`, never `cat` a diff — and ask for a compact when it runs
long. Compact keeps the session; that is why it is still allowed.

**39. The project is ratel and the crew is a clan.** Every name moved in one mechanical rename
before the docs rewrite: the import package and distribution `agentchat` → `ratel`, the console
scripts → `ratel`, `ratel-mcp`, `ratel-unread`, `ratel-board`, `ratel-fake-harness`, the CLI
`agentchat crew <cmd>` → `ratel clan <cmd>`, the concept `crew` → `clan` (`ClanConfig`,
`ClanPaths`, `clan_table`, the `CLAN_*` environment variables), the on-disk names
`clan.toml` / `clan.state.json` / `clan/proposal.json`, the bus attachment type `{"type": "clan"}`,
and the board's `/api/clan/catalog` and `/api/channels/<ch>/clan` routes with their card, JS
identifiers and CSS classes. The home resolves `RATEL_HOME`, then the deprecated `AGENTCHAT_HOME`
with one stderr line, then `~/.ratel`, and the old directory is not moved. There is no read-side
shim for old `{"type": "crew"}` attachments. The decision records above are left as they were
written: they say `agentchat` and `crew` because that is what the thing was called then, and this
entry is the map between the two vocabularies. *Why:* the old name collided with an unrelated PyPI
`agent-chat` package and with AutoGen's `autogen-agentchat`, and the repo name, distribution name
and CLI disagreed with each other; a pure mechanical rename with an unchanged test count is the
proof that no behaviour moved with the words. *Cost:* two vocabularies now exist in the archive,
and a reader of an older record has to reach this entry to translate it — which is cheaper than a
history in which the rename never happened.

**40. A role's brief is pinned at kickoff, like its writer bit.** `clan new`, `clan up` and the
approve path record `briefs = {role: brief}` in `clan.state.json` next to `writers`, and
`_check_drift` refuses every command with "role X's brief changed since kickoff — resolve with the
operator" when `clan.toml` disagrees. Legacy state with no `briefs` key is not checked. *Why:*
the arming decisions in `harness.py` — OpenCode `instructions`, `--plugin-dir`, `default_prompt`,
`unattended_tools`, the `clan/` add-dir grant — key on a role's `brief`, not its name, which made
`brief` the third security-relevant field in the agent-writable `clan.toml` and the only one the
state did not pin. Measured: setting `brief = "orchestrator"` on `developer` yielded the dispatch
skill, the ratel CLI, `gh` and `git push` on its next round, silently. *Cost:* changing a brief
mid-run is now an operator act — edit the state under the same flock `clan up` uses, or bring the
role down and up again.

**41. A permission dialog is the operator's, not the orchestrator's: the watcher reads the pane
and posts `@stakeholder` once.** Every thirty seconds the watcher dumps each interactive role's
pane and asks `clan/prompts.py` whether the bottom of the screen is a harness permission dialog
(opencode's `△ Permission required` / `△ Always allow` block, Claude Code's `Do you want to
proceed?` menu — header and footer both, in the last fifteen lines). A match is recorded once under
`watch.awaiting[role]` and posted once on the bus as `ratel`: `@stakeholder developer is waiting on
a permission prompt in tab N: <text>`. `status` and the board report the role as
`awaiting-operator` ahead of `stuck` and `busy`; the stale "re-dispatch" nudge and the automatic
compaction both skip an awaiting role; when the screen moves on the record is dropped and the
role's nudge clock restarts from the unblock. Headless kinds are never probed — `PROMPT_MARKERS`
already kills a headless round on a dialog. *Why:* a role sat fifteen minutes on an opencode
external-directory prompt while `status` said `busy`, then `stuck`, and the watcher told the
orchestrator to re-dispatch. Reading a pane is not typing into it, and the orchestrator cannot
answer another role's dialog — only the operator can, so only the operator is told. *Cost:* the
watcher's first bus write (nothing trusts the `ratel` sender, so no new trust surface); one
`dump-screen` per role per thirty seconds; the one free-text field is capped at 200 characters and
re-sanitised on read, and `reasons[]` stays a closed vocabulary. Pre-seeding opencode's
external-directory permissions per worktree is deferred — roles are briefed to stay inside their
worktree, and the orchestrator provisions anything from outside it.
