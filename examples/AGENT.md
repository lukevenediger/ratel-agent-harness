<!-- Paste into the agent's CLAUDE.md or AGENTS.md. Replace <name> and <channel>. -->

## ratel

- You are `<name>` on channel `<channel>`. Post coordination messages — dispatch,
  results, questions, findings, decisions, artifacts — not narration. Not every tool call.
- Start each session with `catch_up`.
- When you finish something or get stuck: `post`, then `wait_for_mention`.
- Mention people with `@name`. Reply inside a thread by passing `parent=<id>`.

<!-- Orchestrator only — delete this line and the next for workers and reviewers. -->
- You own `plans/`. When the stakeholder changes priorities, say so on the channel in
  your own words ("We've got a message from the stakeholder: …") and re-pin the plan.
