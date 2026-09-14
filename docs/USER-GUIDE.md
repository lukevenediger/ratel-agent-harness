# ratel clan — quick and dirty user guide

How to go from "I have an issue" to "a clan of agents built it and opened a PR", on Linux or macOS.
Reference detail lives in [harness-setup.md](harness-setup.md), [cli-contract.md](cli-contract.md),
[ARCHITECTURE.md](ARCHITECTURE.md) and [DECISIONS.md](DECISIONS.md). This page is the short path.

## 0. One-time setup

```bash
cd <path-to-your-clone>
uv sync
uv tool install --editable . --force      # puts ratel, ratel-board, ratel-mcp… on PATH

# Zellij 0.44+ — install it however your platform prefers:
#   macOS:       brew install zellij
#   via Rust:    cargo install zellij
#   distro pkg:  apt install zellij   /   dnf install zellij   /   pacman -S zellij
#   release:     https://zellij.dev/documentation/installation

gh auth status                             # the clan uses your gh login for issues, labels, PRs
```

Provider keys live in `<path-to-your-clone>/.env` (gitignored, never printed). Copy
`.env.example` and fill it in, then export them in the shell that will launch a clan — tabs inherit
that shell's environment:

```bash
set -a; . <path-to-your-clone>/.env; set +a
export DEEPSEEK_API_KEY                             # developer, on DeepSeek direct
export OPENROUTER_API_KEY="$OPENROUTER_API_TOKEN"   # docs + any OpenRouter preset
```

**The names on the left are the only ones that count.** `clan up` refuses to start a role whose
model needs a key that is not exported, and it checks the exact variable the models catalog names
— `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`. A `.env` that spells one of them differently
(`OPENROUTER_APO_TOKEN` has happened) fails every OpenRouter role at that check with
`developer: OPENROUTER_API_KEY`, which looks like a missing key rather than a typo three files
away. Read the required names out of `ratel clan catalog` (`models[].env`) rather than
trusting the `.env`.

Board, once, outside any zellij session (serves every channel):

```bash
ratel-board --host <your-tailnet-ip> --port 8787   # tailnet address; never `tailscale funnel`
```

`tailscale ip -4` prints your address. Open <http://<your-tailnet-ip>:8787/>. Phone works.
Pick a channel from the left rail.

## 1. Write the issue (GitHub is the source of truth)

**The issue is the plan.** No approval label, no `<!-- plan-run -->` comment — the orchestrator reads
the body and the comments (newest wins where they disagree) and works from that. Write enough for a
clan to build from: what to change, the checks to run, and ideally a `Footprint:` line naming the
files it touches (it decides whether a second writer is possible; without one the orchestrator
derives it and says so in the proposal).

The issue must still carry none of `needs-spec`, `needs-operator`, `dispatched`, have no open PR and
no open sub-issues — an epic with children is a tracker, not work.

Want a fuller plan written for you: open `claude` in the repo and run `/plan-issue <n>` (how-we-work
skill). Optional — a hand-written issue dispatches exactly the same.

## 2. Start a clan

```bash
ratel clan new <path-to-your-clone> 15       # <checkout> <issue>; prints the attach command
zellij attach harbor-15-0910-1423       # channel is <repo>-<issue>; the session adds a stamp
```

The channel is `harbor-15` and stays that — it is what `--channel` and the board use. The
**zellij session** is the channel plus the start time, because zellij keeps exited sessions listed
for `attach` to resurrect and a restart must not collide with its predecessor. Take the name from
`clan new`, from `clan status`, or from `clan.state.json`.

A zellij socket path caps at 103 bytes and macOS `$TMPDIR` eats ~49 of them, so a long name gets
shortened — the stamp drops its date first (`-0910-1423` → `-1423`), and only then does the
channel lose characters off its front. `clan new` says so on stderr when it happens.

`clan new` creates the channel, a detached zellij session with tabs `orchestrator` (Claude, Fable
latest, already running `/clan:dispatch-issue 15`), `watch` (the nudge dispatcher) and `bus` (raw log),
and prints the attach command. Board: `http://<your-tailnet-ip>:8787/#harbor-15`.

Options: `--orchestrator-model M`, `--orchestrator-harness claude|opencode` (the GLM-as-orchestrator
experiment), `--unattended` (catalog defaults, headless roles, no approval gate — Tier 2 only, see #8).

Tip: pin a **stakeholder note** on the channel before attaching if the orchestrator needs context the
issue does not carry (`AGENT_NAME=stakeholder CHANNEL=harbor-15 ratel post --pin "…"`).

## 3. Approve the clan

The orchestrator reads the plan, checks the guards, locks the issue (assigns you), and posts a
**clan proposal**: role, setup, why. It stops and waits.

The proposal is a card on the board. Open the board once with the token URL `ratel-board`
prints; Confirm posts the approved clan back on the channel and the orchestrator picks it up.
On the card you can:

- **change a role's setup** — one dropdown per row, listing curated presets like
  `1) opencode · DeepSeek Flash · DeepSeek · high`. A preset is the harness, the model and
  the provider's own effort word in one choice, so you cannot assemble a combination that does not
  work. Nothing else picks a model.
- **move the writer** — one radio, and exactly one role must hold it.
- **add a role** — the dropdown offers the catalogued roles this clan is missing; the box beside
  it takes a name of your own. Eight roles maximum, and the control disables at the cap.
- **remove a role** with the `×`. The proposal is a suggestion: if you want only a security
  reviewer, delete the rest. Removing the row that holds the writer leaves the clan without one,
  which shows as an error and disables Confirm until you give it to somebody.
- **edit skills and the role's name**; `why` is the orchestrator's argument for the role and is
  read-only.

The orchestrator plans around the clan that comes back, not the one it proposed — if you amended
the card, it rewrites and re-pins the execution plan and the clan diagram before dispatching.

Catalog defaults (`ratel clan catalog`): developer = `deepseek-flash-high` (the only writer);
reviewer and security-reviewer = `opus-high`; simplifier = `opus-medium`; docs =
`or-deepseek-flash-high`; orchestrator = `fable-high`, fixed at kickoff. Override per run on the
card, or permanently in `~/.ratel/roles.toml` (a role's `preset` key) and
`~/.ratel/presets.toml` (a preset of your own, deep-merged over the packaged list).

If you want a combination nobody curated, add it to `~/.ratel/presets.toml` — an id, an
`order`, a `label`, and the harness/model/effort it stands for — and it appears in the dropdown.

Then the orchestrator runs `clan up`: one git worktree per role under `~/<repo>-wt/<issue>-<role>`
(the writer holds branch `issue-<n>`, others are detached at its tip), one config dir per role under the
channel, one tab per role named after the role. First time in a new worktree, Claude shows a trust
dialog in the tab: accept it.

## 4. While it runs

You can watch, or walk away.

- **Board**: pinned plan + checklist with the progress bar in the header; every dispatch, result and
  `VERDICT:` line; evidence files attached. Per-role context meter comes from `clan status`.
- **History and search**: the board starts with the latest 100 messages. **Load older messages**
  adds earlier history without losing your place. Search message text across the whole channel;
  the agent selector filters mentions, and **Operator messages** selects posts by or mentioning
  `@stakeholder`. Filters combine. Replies remain visible and link to their parent thread.
- **Connection**: **Live** means the event stream is connected. A lost connection reconnects
  automatically; use **Retry** for an immediate attempt or a failed page/thread load.
- **Links**: copy **Channel link** or **Thread link** to share or bookmark the current view and
  filters. Browser Back/Forward restores it. These links do not contain the approval token.
- **Threads**: click a message or focus it and press Enter to open the thread on the right. Drag its left edge to
  make it wider — the width is remembered in that browser, and the message column reflows to
  match. Keyboard: tab to the edge and use the arrow keys (left widens), Home/End for the
  extremes, Enter to reset. On a phone the thread is a full-screen sheet instead.
- **Reading an attached document**: a `.md` or a `.log` attachment has a **preview** button beside
  its link. Markdown opens in a panel with real headings, tables, task lists and — for a
  ```` ```mermaid ```` fence — the rendered diagram, so the orchestrator's `plans/clan.md` reads as
  a diagram rather than as source. A `.log` opens as plain text, never interpreted. A diagram that
  will not parse keeps its source on screen with the parse error under it, so nothing is ever
  hidden by a rendering failure. A mermaid fence posted in a MESSAGE shows a `render diagram`
  button instead of rendering by itself — the renderer is ~1 MB and is not downloaded until
  something on screen actually needs it.
- **Tabs**: `zellij attach <the session clan new printed>`, `Ctrl+t` then arrows or the tab name to move. You can type
  into any role's tab; it is just its harness. Detach with `Ctrl+o d`.
- **Nudges**: a role hears `@role` because the watcher types one line into its pane. If a nudge landed
  mid-turn and was swallowed, re-send it: `ratel clan nudge developer --channel harbor-15`.
- **Status**: `ratel clan status --channel harbor-15 --pretty` (add `--screen` for the last 20
  lines of each pane). Shows worktree, branch, dirty, presence, `context_tokens`, last nudge,
  and a state per role — `awaiting-operator` means a permission dialog is open in that tab.
- **Context**: roles are cleared at task boundaries by the orchestrator and compacted automatically when
  idle above 200k tokens (`checkpoint_at`, per role). By hand: `ratel clan checkpoint developer
  --channel harbor-15` (`--mode compact` to keep the thread).
- **Change of priorities**: tell the orchestrator in its tab. It re-pins the plan in its own words.

Rounds: orchestrator dispatches one role per message in a thread, verifies the result itself, gates on
`VERDICT: SIGN-OFF` from reviewer, simplifier and security-reviewer; `CHANGES-REQUESTED` loops back to
the writer.

## 5. Session end

The orchestrator: syncs the base branch by merge, runs the plan's verification plus the repo checks,
requires the three sign-offs, opens **one PR** on `issue-<n>` with a "Plan deviations" section, removes
the assignee, pins a run summary, then `clan down`. It never merges.

You: review the PR, merge (squash), then clean up:

```bash
gh pr merge <pr> -R <owner>/<repo> --squash --delete-branch
ratel clan down --channel harbor-15 --prune-worktrees   # if the clan did not prune
git -C <path-to-your-clone> fetch -p && git -C <path-to-your-clone> reset --hard origin/main
uv tool install --editable <path-to-your-clone> --force          # if ratel itself changed
```

## 6. Another repo

Same commands; nothing is per-repo. `~/.ratel` is shared, the board shows every channel.

```bash
git clone git@github.com:<owner>/harbor.git ~/harbor
ratel clan new ~/harbor 42        # then attach to the session it prints
```

Plain ratel without a clan (one session on a channel): copy `examples/mcp.json` to the repo's
`.mcp.json` and `examples/claude-settings.json` to `.claude/settings.json`, set `AGENT_NAME`/`CHANNEL`.
From a shell: `AGENT_NAME=operator CHANNEL=harbor ratel post "hello"`.

## 7. When something is off

| Symptom | Do |
|---|---|
| Role sits idle after a dispatch | `clan status --screen`; re-nudge; check a trust or permission dialog in its tab |
| `awaiting-operator` on the board, or an `@stakeholder … permission prompt in tab N` post | attach, go to that tab, answer the dialog; the role resumes on its own |
| Forgot the attach name | `ratel clan status --channel <channel>` prints `session` |
| `clan up` refuses: branch checked out elsewhere | this checkout holds `issue-<n>`; `git checkout main` (or a `-host` branch) and retry |
| Orchestrator refuses to dispatch | `needs-spec`/`needs-operator`/`dispatched` label, an open PR, or open sub-issues; see §1 |
| Stray zellij session from a test | `zellij delete-session --force <name>`; tests must not touch the live server (#6) |
| Board shows nothing for the channel | board reads `~/.ratel`; `RATEL_HOME` on the board and on `clan new` must match |
| Channels missing after upgrading from `agent-chat` | the home is now `~/.ratel`; `AGENTCHAT_HOME` is a deprecated fallback with a warning, or `mv ~/.agentchat ~/.ratel` |
| Model key missing | export it in the shell **before** `clan new`; tabs inherit that environment |

Tests: `uv run pytest -q` (unit), `uv run pytest -q -m zellij` (real isolated zellij round),
`uv run pytest -q -m headless` (real `claude -p` / `opencode run`, opt-in),
`CLAN_SANDBOX_REPO=<owner>/clan-sandbox uv run pytest -q -m llm tests/e2e` (Tier 2, needs
`CLAN_SANDBOX_TOKEN`, see #8).
