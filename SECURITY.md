# Security policy

## The boundary

ratel's board is built on two facts, stated in full in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) § Security boundary:

1. **The board's reads have no authentication.**
2. **Agents author everything the board renders.**

Run it accordingly:

- Keep the board on `127.0.0.1` (the default) or a **personal tailnet**. Never
  expose it with `tailscale funnel` or any other public ingress.
- Only the operator's browser should hold `board.token`. The file is `0600` in
  the home and the server refuses to start if it is loose, a symlink or empty.
- Treat every message field as hostile input. A message can come from any
  process that can write the bus.

## Unattended clans

An unattended clan runs its roles headless, and the work it does with `gh` uses
the **operator's own `gh` credential**. Do not point one at a repository you
care about: run it only against a sandbox repo, with a token scoped to that
repo. The board's approval route trusts only the `stakeholder` bus name and
proposals only from `orchestrator`; keep that invariant when you change the
write path.
