<!-- issue
Add a hello endpoint to the sandbox package:

- `sandbox/hello.py` with a `hello()` function that returns the string `"hello"`
- `tests/test_hello.py` covering it

Checks: `python -m pytest -q`. One commit on `issue-<n>`.

## Plan

1. Task 1 — hello module and test (developer): create `sandbox/hello.py` with `hello() -> "hello"`
   and `tests/test_hello.py` asserting it, run the checks, push to `issue-<n>`.
2. Task 2 — review round (reviewer): read the diff against the base commit, run the checks,
   reply `VERDICT: SIGN-OFF` or `VERDICT: CHANGES-REQUESTED`.
3. Task 3 — sign-off (simplifier, security-reviewer): whole-diff sign-off, `VERDICT:` lines.
4. Task 4 — close (orchestrator): open ONE PR from `issue-<n>` to `main` (never merge it), then
   release the issue: `gh issue edit <n> --remove-assignee @me`, post and pin a summary,
   `ratel clan down`.

## Clan

Catalog defaults, unattended mapping applied (`claude` → `claude-p`, `opencode` → `opencode-run`):
`orchestrator` claude-p, `developer` opencode-run (the writer), `reviewer`, `simplifier`,
`security-reviewer` claude-p. Builder and reviewer are different models. All roles push with the
environment's GitHub credentials — if a push fails, report it, do not route around it.

## Checks

`python -m pytest -q`

## Rules

One commit on `issue-<n>`. Never merge the PR. No session URLs anywhere.

## Footprint

`sandbox/hello.py`, `tests/test_hello.py`
-->
