---
name: dispatch-issue
description: Execute an operator-approved GitHub issue with a clan of role agents over an ratel channel. Use when told "/dispatch-issue N", "execute the plan for issue N with a clan", or when a clan brief says to follow this skill.
---

# Dispatch an issue to a clan

You are the orchestrator. A clan executes the plan; you own the plan, the clan and the gates, and
you never write the code yourself. Argument: the issue number. If it is missing, ask for it.

The issue IS the plan: its body is the brief, and its comments refine it — newest wins where they
disagree. There is no separate approved-plan artifact to look for. The channel is the audit log:
every dispatch, result and gate goes through it, so a human reading the board afterwards can see
who decided what.

## Guards

Before anything else, `gh issue view <n> --comments`, then:

- `needs-spec` or `needs-operator` → stop and resolve with the human.
- `dispatched` present → stop: an unattended run owns this issue right now. Never add that label
  yourself; it is the unattended lock.
- An open PR already references the issue → stop and point at it. Resuming or superseding is the
  human's call.
- Open sub-issues exist → refuse: a fanned-out epic is a tracker, not work. Each child gets its own
  planning session and its own clan.

## Lock

`gh issue edit <n> --add-assignee @me`. The assignee is the only mid-build marker there is — a
sweep looking for unstarted work skips assigned issues.

## Read the plan

Read the issue in full, body and comments. That is the plan. If it names a Footprint (the files
this work touches), read it — it decides whether a second writer is even possible; if it does not,
derive the footprint yourself from the issue and say so in the proposal. Validate the plan against
the current code: the session that wrote it may predate recent
merges. Record every deviation and its reason; the PR needs them.

## Propose the clan

Start from the catalog: `ratel clan catalog` — harnesses, presets, roles and models, as the
board serves them. A **preset** is the whole setup choice for a role in one id: it carries the
harness, the model and the provider's own effort word together, so the three can never disagree.
Read the ids out of the catalog's `presets` list at propose time and pick one per role; never work
from a list of preset ids remembered or copied from elsewhere, this file included — the catalog is
the only current one.

Write `proposal.json`: a `{"roles": [...]}` list, one object per role with exactly `name`,
`preset`, `writer`, `skills`, `why` — why this role is needed for THIS issue. **There is no
`harness`, `model` or `effort` in a proposal.** The preset supplies all three; those keys are
ignored if you send them, and they spend the attachment's byte budget for nothing.

Then `ratel clan propose --file proposal.json`: it validates the proposal against the catalog,
writes `clan/proposal.json` into the channel, and posts and pins the proposal on the bus itself —
you do not post the clan table by hand. Rules the validator enforces (get them right before
proposing):

- Exactly one role is the writer; it holds `issue-<n>`. Everyone else is detached at its tip.
- Every role names a preset, and it must be one the catalog currently lists.
- No role is called `orchestrator`; yours was fixed at kickoff and is never proposed.
- Roles earn their place. A role with nothing to do is noise.
- You may search the skills registry with `npx skills find <query>` and list `skills = [...]` for a
  role; they are installed into that role's worktree at `clan up`.

Two rules the validator does NOT check, so they are on you:

- The builder and a reviewer are never the same model. That is the point of the clan, and presets
  do not enforce it — two different presets can resolve to the same model. The catalog prints each
  preset's `model`; compare them before you pick.
- Per-role `checkpoint_at` is a catalog override in `$RATEL_HOME/roles.toml` (default 200000);
  it is not part of the proposal file.

## Approval gate

The human confirms the clan ON THE BOARD, not in your tab: the board page — opened once with the
token URL that `ratel board` printed — renders your newest proposal as an editable card, and
its Confirm posts `@orchestrator clan approved` with an attachment whose `supersedes` names your
proposal. The watcher nudges you when that lands; `wait_for_mention` is how you wait. Then run
`ratel clan approve` (it takes the newest approval, or the message id from the nudge): it
validates the attachment, writes `clan/clan.toml`, records the writer bits and prints the clan.
Only then `ratel clan up`.

**The clan that comes back is the clan.** The card is editable: the human may add a role, drop
one, or change a preset before pressing Confirm. What `ratel clan approve` writes into
`clan/clan.toml` is authoritative, and it may not be what you proposed — read it, and diff it
against `clan/proposal.json`. If it differs at all, rewrite and re-pin BOTH artifacts before the
first dispatch: `plans/execution.md` with its `tasks` attachment (rounds, owners and gates around
the roles that actually exist) and `plans/clan.md` (the role table and the mermaid diagram),
unpinning the previous pins each time. Planning around the clan you wanted instead of the clan you
have is the failure this gate exists to prevent.

If the operator types an amendment in your tab instead of using the board: edit `proposal.json`,
re-run `ratel clan propose --file proposal.json` — the new proposal supersedes the old one —
and wait again.

Exception: with `CLAN_UNATTENDED=1` in your environment, or a brief that says the clan is
unattended, run `ratel clan propose --file proposal.json --auto-approve` — it lands
`clan.toml` directly, no board round trip. Do not proceed on silence in an attended clan.

## Artifacts

Write, then post with `attach_file`, then pin — all three:

1. `clan/clan.toml` — the approved clan, written by `ratel clan approve` (or `ratel clan
   new` at kickoff); never hand-written, never hand-edited — amendments go through a new
   `clan propose` and a new board Confirm.
2. `plans/execution.md` — the rounds. For each: what happens, who owns it, and the gate that closes
   it (which role's `VERDICT:` line, or which check).
   The message that pins it MUST carry a `tasks` attachment — one item per plan task plus a final
   "PR opened / plan consumed" item — so the stakeholder can read projected against done against
   outstanding on the board without opening a file:

       {"type": "tasks", "ref": "plans/execution.md", "items": [
         {"done": false, "text": "Task 1 - config layer", "who": "developer"}
       ]}

   The file and the checklist are the same list and never disagree.
3. `plans/clan.md` — the role table plus a mermaid diagram of who talks to whom.

Both 2 and 3 describe the APPROVED clan. If the board's Confirm amended it, they are rewritten and
re-pinned before the first dispatch (see the approval gate).

Pins are how a role that joins late learns what is going on. Anything not pinned will be missed.

Then `ratel clan up`. It creates the worktrees, writes each harness config, opens one tab per
role and records the pane ids. It refuses (exit 1) when a role's model needs a provider env var
that is not exported (`developer: OPENROUTER_API_KEY`) — export the key and re-run, or re-propose
with a preset whose model needs keys you have.

## Rounds

- One `@role` dispatch per message, naming the thread the reply belongs in. Expect exactly one
  reply per round.
- A role's context is disposable at task boundaries: before dispatching a NEW task to a role,
  and after each round gate you close, run `ratel clan checkpoint <role>` (mode `clear`) for
  that role. Never clear a role mid-task — the watcher compacts on threshold. `CLAN_UNATTENDED`
  runs get the same through the headless reset.
- **Your own context is not disposable.** It is the run's state. Never run
  `ratel clan checkpoint orchestrator` — the tool refuses a clear on you and refuses any
  self-checkpoint, because it works by typing into your pane while you are mid-turn. Keep your
  context small instead: pipe verification through `| tail` and `| wc -l`, never `cat` a diff.
  If it still runs long, post `@stakeholder` asking for a compact and wait; the operator runs
  `ratel clan checkpoint orchestrator --mode compact` when your pane is idle.
- Verify independently before you believe a result: read the diff, run the tests, look at the
  artifacts. "Done" is a claim.
- Gate on `VERDICT:` lines. `VERDICT: SIGN-OFF` closes the round; `VERDICT: CHANGES-REQUESTED` goes
  straight back to the writer with the findings attached, and the round repeats.
- `ratel clan sync <role>` fast-forwards a reviewer's worktree to the branch tip before a
  review round.
- Every time a task lands, re-post the plan with that item ticked and re-pin it, unpinning the
  previous pin, so exactly one current checklist is on the board. This is the stakeholder's view of
  the run; a stale checklist is worse than none.
- When the stakeholder changes priorities, say so on the channel in your own words and re-pin the
  plan. Agents read pins.
- A role that has gone quiet after a nudge: check its tab (`ratel clan status --screen`),
  re-nudge (`ratel clan nudge <role>`), then re-brief or replace it.

## Session end

1. `ratel clan sync` the review roles, then sync the base branch by MERGE, not rebase:
   `git fetch origin <default>` then `git merge origin/<default>`. Before verifying, not after.
   Conflicts: resolve preserving both intents; where incompatible, take the side matching the
   plan's goal and note the trade-off. Cannot resolve without guessing → `git merge --abort`, open
   the PR anyway, comment naming each conflicting file and what judgment is missing, and add
   `needs-operator`.
2. Run every verification step the plan lists, then the repo's own checks (the `Checks:` line in
   its CLAUDE.md; failing that, the fast job in `.github/workflows/ci.yml`). Capture the real
   output as evidence. Reading a generated artifact back is evidence; an exit code is not.
3. Require `VERDICT: SIGN-OFF` from the simplifier and the security reviewer. These are acceptance
   items, not suggestions.
4. Open ONE PR on `issue-<n>`, written for someone reviewing a batch with zero session context:
   what changed, why, what to check, where the evidence is, and a "Plan deviations" section
   (write "none" if none). Never merge it.
5. Release the issue: `gh issue edit <n> --remove-assignee @me`.
6. Post and pin a summary of the run, then `ratel clan down`.

## Boundaries

- Never contact clients or client-facing channels.
- Never include a session URL in a PR, commit, comment or any other GitHub artifact.
- Never `--no-verify`. A failing hook is a failing check — fix the files.
- Never edit the code yourself. If the clan cannot do it, that is a finding, not a excuse to
  take over.
