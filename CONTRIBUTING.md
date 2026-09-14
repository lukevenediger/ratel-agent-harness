# Contributing

Thanks for looking. ratel is small and the rules are short.

## Run the tests

```bash
uv sync
uv run pytest -q
```

The default run is hermetic: it uses a temporary `RATEL_HOME`, and the
`zellij`, `node`, `llm` and `headless` markers are skipped or deselected when
the binary or key they need is absent.

```bash
uv run pytest -q -m zellij     # needs the real zellij binary
uv run ruff check .            # lint
```

Never point a test or a smoke run at the live `~/.ratel`. It is a real home.

## Look at the board with fake data

```bash
RATEL_HOME=/tmp/ratel-demo uv run python scripts/seed_demo.py
RATEL_HOME=/tmp/ratel-demo uv run ratel-board --port 8791
```

`scripts/seed_demo.py` writes a `harbor` channel with one of every attachment
shape — pins, a thread, code, an image, a PDF stub, links, a clan proposal — so
board work can be eyeballed without a live clan. It writes only to
`$RATEL_HOME`; leave that pointing at a scratch directory.

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
