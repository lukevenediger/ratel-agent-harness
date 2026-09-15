# Contributing

Thanks for looking. ratel is small and the rules are short.

## Run the tests

```bash
uv sync --frozen
uv run pytest -q
```

Tests use temporary homes. Live provider tests are opt-in; optional runtime tests may skip
when their prerequisites are missing. Never point a test or smoke run at the live `~/.ratel`.

```bash
uv run --frozen ruff check .
uv run --frozen python scripts/verify-wheel.py
```

Install Node 22+ and the locked browser dependencies to include browser checks:

```bash
npm ci --ignore-scripts --prefix tests/browser
npx --prefix tests/browser --no-install playwright install chromium
RATEL_REQUIRE_BROWSER=1 uv run --frozen pytest -q
```

The required-browser flag makes missing prerequisites an error. CI runs Linux Python 3.12/3.13
and macOS Python 3.14, plus lint and an installed-wheel smoke test. `CI required` aggregates them.

### Terminal integration tests

Install Zellij 0.44.1 (the CI-pinned release) for `uv run --frozen pytest -q -m zellij`. Install HerdR 0.9.0+ on PATH, or set
`HERDR_TEST_BINARY=/absolute/path/to/herdr`, to include real isolated HerdR tests in the normal
suite. Use `uv run` so child panes can find the scripted harness executable. These tests create
temporary homes and servers; they do not use the operator's live sessions or model turns.
See [backend verification](docs/cli-contract.md#terminal-backends) for optional native smoke tests.

## Look at the board with fake data

Choose a new, nonexistent home directory:

```bash
uv run ratel demo --home /tmp/ratel-demo
uv run ratel-board --home /tmp/ratel-demo --port 8791
```

Open the `harbor-demo` channel. It includes pins, threads, Markdown and code attachments and
requires no credentials or running agents. The demo refuses an existing home; use another
path to repeat it. When changing user-facing behavior, update the
[user guide](docs/USER-GUIDE.md) and relevant reference documentation.

## One PR per issue

`main` is protected. Do your work on an `issue-<n>` branch, one PR per issue,
and keep the PR description focused on that issue. A change with no test that
would catch its absence is not done.

## Decisions go in `docs/DECISIONS.md`

Memory is repo-bound, never machine-bound. Preferences, build state, lessons
and decisions belong in this repo, and `docs/DECISIONS.md` is the only memory
location — not a harness's memory store, not an untracked build log. Add a
numbered record when you make a decision worth keeping, and do not rewrite the
older records: they are the archive.

## The board renders agent-authored content

The board's reads are unauthenticated and it renders whatever agents post.
Treat **every message field as hostile input**: no string from the bus is
trusted, text goes through the page's `esc()` path first, and URLs go through
`safeUrl()`. If you add a rendering path, add it to the escape-first model and
cover it with a test. See `docs/ARCHITECTURE.md` § Security boundary.

## Commit style

Plain conventional-commit subjects (`fix(clan): …`, `docs: …`), one logical
change per commit where you can. Do not commit secrets; `.env` is gitignored
on purpose.
