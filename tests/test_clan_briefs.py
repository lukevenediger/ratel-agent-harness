"""The packaged briefs and the orchestrator's plugin skill."""
import json
import tomllib
from pathlib import Path

import pytest

from ratel.clan import config as C

PKG = Path(C.__file__).parent
BRIEFS = PKG / "briefs"
PLUGIN = PKG / "plugin"
FIELDS = dict(channel="harbor-42", issue=42, repo="acme/widget", role="developer",
              branch="issue-42", worktree="/tmp/harbor-wt/42-developer",
              clan_table="| role | harness |\n| developer | opencode |")


def roles():
    return sorted(C.load_catalog(None))


def test_every_catalog_role_has_a_brief():
    assert {p.stem for p in BRIEFS.glob("*.md")} >= set(roles())


def test_every_catalog_role_points_at_a_brief_that_exists():
    for name, spec in C.load_catalog(None).items():
        assert (BRIEFS / f"{spec['brief']}.md").exists(), name


@pytest.mark.parametrize("role", roles())
def test_brief_formats_with_every_field(role):
    text = (BRIEFS / f"{role}.md").read_text().format(**FIELDS)
    for field in FIELDS:                                # no placeholder survives formatting
        assert "{" + field + "}" not in text
    assert FIELDS["channel"] in text and str(FIELDS["issue"]) in text
    assert len(text.encode()) < 64_000                  # --append-system-prompt is an argv element


@pytest.mark.parametrize("role", roles())
def test_brief_states_the_identity_and_the_comms_protocol(role):
    text = (BRIEFS / f"{role}.md").read_text()
    assert "You are `{role}` on ratel channel `{channel}`" in text
    assert "catch_up" in text
    assert "attach_file" in text                        # pass its return value unchanged
    assert "parent" in text                             # thread replies


@pytest.mark.parametrize("role", ["reviewer", "security-reviewer", "simplifier"])
def test_review_roles_are_told_to_emit_a_verdict_line(role):
    text = (BRIEFS / f"{role}.md").read_text()
    assert "VERDICT: SIGN-OFF" in text and "VERDICT: CHANGES-REQUESTED" in text
    assert "severity" in text.lower() and "evidence" in text.lower()


def test_developer_brief_covers_tdd_and_the_nudge_contract():
    text = (BRIEFS / "developer.md").read_text()
    assert "wait_for_mention" in text                    # explicitly told not to, after a nudge
    assert "{branch}" in text and "{worktree}" in text


def test_orchestrator_brief_owns_the_plans_and_gates():
    text = (BRIEFS / "orchestrator.md").read_text()
    assert "plans/" in text and "VERDICT" in text and "clan.toml" in text


def test_plugin_manifest_namespaces_the_skill_as_clan():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "clan"                    # gives /clan:dispatch-issue
    assert manifest["skills"] == "./skills/"


def test_dispatch_issue_skill_front_matter():
    text = (PLUGIN / "skills" / "dispatch-issue" / "SKILL.md").read_text()
    head, _, body = text.partition("\n---\n")
    assert head.startswith("---\n")
    front = head.removeprefix("---\n")
    assert "name: dispatch-issue" in front
    assert "description:" in front and "issue" in front
    for section in ("## Guards", "## Lock", "## Read the plan", "## Propose the clan",
                    "## Approval gate", "## Artifacts", "## Rounds", "## Session end", "## Boundaries"):
        assert section in body, section


def test_dispatch_issue_skill_carries_the_build_issue_contract():
    body = (PLUGIN / "skills" / "dispatch-issue" / "SKILL.md").read_text()
    for needle in ("needs-operator", "add-assignee",
                   "ratel clan up", "ratel clan down", "Plan deviations",
                   "VERDICT", "CLAN_UNATTENDED", "session URL"):
        assert needle in body, needle
    assert "dispatched" in body                          # never sets the unattended lock itself
    # The issue is the plan: no separate approved-plan artifact to gate on.
    for gone in ("plan-approved", "plan-run", "build-ready"):
        assert gone not in body, gone


def test_repo_root_skills_symlink_points_at_the_plugin_skills():
    link = Path(C.__file__).parents[2] / "skills"
    assert link.is_symlink() and link.resolve() == (PLUGIN / "skills").resolve()


def test_roles_catalog_and_briefs_stay_in_step():
    cat = tomllib.loads((PKG / "roles.toml").read_text())["roles"]
    briefs = {p.stem for p in BRIEFS.glob("*.md")}
    # `probe` is the one packaged brief no catalog role uses: headless smoke
    # clans set it per-role so no skill load path arms.
    assert {v["brief"] for v in cat.values()} | {"probe"} == briefs


def test_orchestrator_pins_a_tasks_checklist_for_the_stakeholder():
    """The board renders a `tasks` attachment as a checklist; that is the stakeholder's view."""
    text = (BRIEFS / "orchestrator.md").read_text().format(**FIELDS)
    assert '"type": "tasks"' in text and "plans/execution.md" in text
    assert "re-pin" in text and "unpinning" in text


@pytest.mark.parametrize("role", ["developer", "reviewer", "security-reviewer", "simplifier", "docs"])
def test_only_the_orchestrator_pins_the_plan(role):
    assert "Never post your own checklist" in (BRIEFS / f"{role}.md").read_text()


def test_docs_brief_does_not_promise_tooling_that_does_not_exist():
    """`session.up` gives every non-writer a DETACHED worktree, so the docs role
    has no branch of its own — the old `issue-{issue}-docs` branch was created by
    no code path. `docs/lessons/` is not a directory this repo has either."""
    text = (BRIEFS / "docs.md").read_text()
    assert "issue-{issue}-docs" not in text
    assert "docs/lessons/" not in text
    assert "detached" in text                        # says what actually happens


def test_skill_requires_the_tasks_attachment_on_the_pinned_plan():
    body = (PLUGIN / "skills" / "dispatch-issue" / "SKILL.md").read_text()
    assert '"type": "tasks"' in body
    assert "PR opened / plan consumed" in body
    assert "re-pin" in body


def test_the_checkpoint_boundary_rule_is_in_the_orchestrator_texts():
    brief = (BRIEFS / "orchestrator.md").read_text()
    skill = (PLUGIN / "skills" / "dispatch-issue" / "SKILL.md").read_text()
    for text in (brief, skill):
        assert "clan checkpoint <role>" in text
        assert "checkpoint_at" in text or "clan checkpoint orchestrator" in text
    assert "Never clear a role mid-task" in brief
    assert "Never clear a role mid-task" in skill
    assert "checkpoint_at" in skill                     # the roles.toml override, not a proposal field
    assert "default 200000" in skill


def test_the_proposal_schema_in_the_skill_is_the_preset_one():
    """The orchestrator writes proposal.json from this file, so a stale schema
    here produces proposals that fail validation on arrival."""
    body = (PLUGIN / "skills" / "dispatch-issue" / "SKILL.md").read_text()
    assert "`preset`" in body
    assert "There is no\n`harness`, `model` or `effort` in a proposal" in body
    # the nine ids live in presets.toml and the catalog, never inline here
    for pid in C.load_presets(None):
        assert pid not in body, pid


def test_the_proposal_schema_in_the_orchestrator_brief_is_the_preset_one():
    text = (BRIEFS / "orchestrator.md").read_text()
    assert "`preset`" in text and "ratel clan catalog" in text
    for pid in C.load_presets(None):
        assert pid not in text, pid


def test_an_amended_clan_makes_the_orchestrator_re_pin_both_plans():
    """The human edits the card, so the approved clan may not be the proposed
    one; both pinned artifacts describe the clan that exists."""
    skill = (PLUGIN / "skills" / "dispatch-issue" / "SKILL.md").read_text()
    brief = (BRIEFS / "orchestrator.md").read_text()
    for text in (skill, brief):
        assert "plans/execution.md" in text and "plans/clan.md" in text
        assert "unpinning the previous pins" in text
        assert "clan that comes back" in text.lower() or "clan you have" in text
        assert "diff it" in text          # against what was proposed, not assume


def test_dispatch_issue_skill_proposes_and_approves_via_the_cli():
    body = (PLUGIN / "skills" / "dispatch-issue" / "SKILL.md").read_text()
    for needle in ("ratel clan catalog", "ratel clan propose --file", "proposal.json",
                   "ratel clan approve", "--auto-approve", "token",
                   "supersedes", "ratel board"):
        assert needle in body, needle
    assert "ratel clan roles" not in body          # replaced by `clan catalog`


def test_dispatch_issue_skill_says_the_human_confirms_on_the_board():
    body = (PLUGIN / "skills" / "dispatch-issue" / "SKILL.md").read_text()
    assert "board" in body and "token URL" in body
    assert "@orchestrator clan approved" in body       # the nudge the Confirm posts


def test_orchestrator_brief_owns_plans_only_clan_toml_is_generated():
    text = (BRIEFS / "orchestrator.md").read_text()
    assert "clan.toml" in text                         # still named (needle above)
    assert "ratel clan approve" in text
    assert "You own `plans/` and `clan/clan.toml`" not in text


def test_skill_artifacts_say_clan_toml_is_written_by_approve():
    body = (PLUGIN / "skills" / "dispatch-issue" / "SKILL.md").read_text()
    assert "written by `ratel clan approve`" in body
    assert "never hand-written" in body.lower()


SKILL = Path(C.__file__).parent / "plugin" / "skills" / "dispatch-issue" / "SKILL.md"


def test_orchestrator_is_never_told_to_checkpoint_itself():
    """A self-checkpoint types /clear into a pane that is mid-turn (Decision 38).
    Neither the brief nor the skill may instruct it; both must say why not."""
    brief = (Path(C.__file__).parent / "briefs" / "orchestrator.md").read_text()
    skill = SKILL.read_text()
    for text in (brief, skill):
        assert "for yourself" not in text
        assert "checkpoint orchestrator" in text          # named, so it is not rediscovered
        assert "never cleared" in text or "not disposable" in text
    assert "checkpoint <role>" in brief and "checkpoint <role>" in skill   # others still are
