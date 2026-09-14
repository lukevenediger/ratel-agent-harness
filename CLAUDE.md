# ratel

Read `README.md`, then `docs/ARCHITECTURE.md` and `docs/DECISIONS.md` before changing code.

Checks: `uv run pytest -q`

Rules
- Memory is repo-bound, never machine-bound. Preferences, build state, lessons and decisions go in
  this repo — `docs/DECISIONS.md` is the only memory location. Never in a harness's memory store or
  auto-memory, and never in an untracked build log.
- Tests use a temporary `RATEL_HOME` only. `~/.ratel` is a live home; never point a test,
  script or smoke run at it.
- `main` is protected. Work on `issue-<n>` branches; one PR per issue.
- Never read, print or `git add -f` the gitignored `.env`.
- No session URLs in commits, PRs or comments.
