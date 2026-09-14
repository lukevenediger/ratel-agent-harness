"""The clan attachment: bus inference, builder, validator, clan.toml synthesis."""
import pytest

from ratel.clan import config as C
from ratel.clan import proposal as P


def cfg(*names):
    roles = {"orchestrator": {}}
    roles.update({n: {} for n in names})
    return C.ClanConfig.from_dict(
        {"channel": "harbor-15", "issue": 15, "repo": "acme/widget",
         "checkout": "/repo", "roles": roles}, C.load_catalog(None))


ROLES = [
    {"name": "developer", "preset": "or-glm-flash-low",
     "writer": True, "skills": [], "why": "writer"},
    {"name": "reviewer", "preset": "opus-high",
     "writer": False, "skills": [], "why": "checks"},
]


def att(roles=None, **over):
    a = {"type": "clan", "status": "proposed", "issue": 15,
         "roles": ROLES if roles is None else roles}
    a.update(over)
    return a


def catalog():
    return C.catalog(None)


# ---- validator ----------------------------------------------------------
def test_rejects_wrong_type():
    with pytest.raises(ValueError, match="type"):
        P.validate_clan_attachment(att(type="tasks"), catalog())


def test_rejects_bad_status():
    with pytest.raises(ValueError, match="status"):
        P.validate_clan_attachment(att(status="draft"), catalog())


def test_rejects_non_int_issue():
    with pytest.raises(ValueError, match="issue"):
        P.validate_clan_attachment(att(issue="15"), catalog())


def test_rejects_empty_and_oversized_roles():
    with pytest.raises(ValueError, match="roles"):
        P.validate_clan_attachment(att(roles=[]), catalog())
    with pytest.raises(ValueError, match="roles"):
        P.validate_clan_attachment(att(roles=[dict(ROLES[0], name=f"r{i}") for i in range(9)]),
                                   catalog())


def _one(**over):
    return [dict(ROLES[0], **over)]


def test_rejects_unmentionable_duplicate_or_orchestrator_names():
    for name in ("has space", "dev!", "a.b"):
        with pytest.raises(ValueError, match="name"):
            P.validate_clan_attachment(att(_one(name=name)), catalog())
    with pytest.raises(ValueError, match="name"):
        P.validate_clan_attachment(att(_one(name="developer") + _one(name="developer")),
                                   catalog())
    with pytest.raises(ValueError, match="name"):
        P.validate_clan_attachment(att(_one(name="orchestrator")), catalog())


def test_rejects_a_missing_preset():
    role = {k: v for k, v in ROLES[0].items() if k != "preset"}
    with pytest.raises(ValueError, match="preset"):
        P.validate_clan_attachment(att([role]), catalog())


def test_rejects_an_unknown_preset_and_names_the_known_ids():
    with pytest.raises(ValueError, match="preset") as e:
        P.validate_clan_attachment(att(_one(preset="gpt-9-turbo")), catalog())
    assert "opus-high" in str(e.value) and "or-glm-flash-low" in str(e.value)


def test_rejects_a_malformed_or_oversized_preset_id():
    for bad in ("", "Opus-High", "opus high", "-opus", "opus_high", "opus|high",
                "opus\nhigh", 7, None, "o" * 41):
        with pytest.raises(ValueError, match="preset"):
            P.validate_clan_attachment(att(_one(preset=bad)), catalog())


def test_harness_model_and_effort_are_not_the_attachment_s_business():
    """They come from the preset's row, so naming them changes nothing: a role
    cannot half-override a preset into an incoherent pair."""
    ok = P.validate_clan_attachment(
        att(_one(harness="claude", model="claude-opus-5", effort="banana")), catalog())
    assert ok["roles"][0]["preset"] == "or-glm-flash-low"


def test_rejects_non_bool_writer_and_requires_exactly_one():
    with pytest.raises(ValueError, match="writer"):
        P.validate_clan_attachment(att(_one(writer="yes")), catalog())
    with pytest.raises(ValueError, match="writer"):
        P.validate_clan_attachment(att(_one(writer=False)), catalog())
    with pytest.raises(ValueError, match="writer"):
        P.validate_clan_attachment(att(_one() + [dict(ROLES[1], name="second",
                                                      writer=True)]), catalog())


def test_rejects_bad_skills_and_why():
    with pytest.raises(ValueError, match="skills"):
        P.validate_clan_attachment(att(_one(skills=["s" * 41])), catalog())
    with pytest.raises(ValueError, match="skills"):
        P.validate_clan_attachment(att(_one(skills=[f"s{i}" for i in range(9)])), catalog())
    with pytest.raises(ValueError, match="why"):
        P.validate_clan_attachment(att(_one(why="w" * 161)), catalog())


def test_rejects_a_non_ulid_supersedes():
    with pytest.raises(ValueError, match="supersedes"):
        P.validate_clan_attachment(att(supersedes="nope"), catalog())


def _fat_roles():
    """8 roles that pass every per-role rule, at the free-text caps. A preset id
    is short and picked from the catalog, so the length lives in name + why."""
    return [dict(ROLES[0], name=f"role{i}" + "n" * 80, why="w" * 160, writer=False)
            for i in range(8)]


def test_rejects_over_the_byte_cap():
    roles = _fat_roles()
    roles[0]["writer"] = True
    with pytest.raises(ValueError, match="bytes"):
        P.validate_clan_attachment(att(roles=roles), catalog())


# ---- clan_config_from ---------------------------------------------------
def test_clan_config_from_keeps_base_and_round_trips_through_toml(home):
    base = cfg("orchestrator_only")  # orchestrator role comes from base
    base.roles = {"orchestrator": C.RoleSpec(
        name="orchestrator", harness="claude", model="claude-fable-5-1", effort="high",
        brief="orchestrator")}
    a = att()
    got = P.clan_config_from(a, base, home)
    assert (got.channel, got.issue, got.repo, got.checkout) == \
        (base.channel, base.issue, base.repo, base.checkout)
    assert got.roles["orchestrator"] is base.roles["orchestrator"]
    # briefs and checkpoint_at come from the role catalog
    assert got.roles["developer"].brief == "developer"
    assert got.roles["developer"].checkpoint_at == 200000
    assert got.roles["reviewer"].brief == "reviewer"
    C.dump_toml(got.to_dict())
    text = C.dump_toml(got.to_dict())
    reread = C.ClanConfig.from_dict(__import__("tomllib").loads(text), C.load_catalog(home))
    assert reread.to_dict() == got.to_dict()


def test_clan_config_from_resolves_the_preset_into_harness_model_and_effort(home):
    base = cfg()
    base.roles = {"orchestrator": C.RoleSpec(
        name="orchestrator", harness="claude", model="claude-fable-5-1", effort="high",
        brief="orchestrator")}
    got = P.clan_config_from(att(_one(preset="deepseek-flash-max")), base, home)
    dev = got.roles["developer"]
    assert (dev.harness, dev.model, dev.effort) == \
        ("opencode", "deepseek/deepseek-flash", "max")


def test_clan_config_from_refuses_a_preset_that_vanished_between_propose_and_approve(home):
    """The preset is re-read at approve time, so a catalog edited in between is
    a clear error, not a role that quietly keeps the proposal's old model."""
    (home / "presets.toml").write_text(
        '[presets.house-style]\norder = 1\nharness = "claude"\n'
        'model = "claude-sonnet-5"\neffort = "high"\nlabel = "house"\n')
    a = att(_one(preset="house-style"))
    base = cfg()
    base.roles = {"orchestrator": C.RoleSpec(
        name="orchestrator", harness="claude", model="claude-fable-5-1", effort="high",
        brief="orchestrator")}
    assert P.clan_config_from(a, base, home).roles["developer"].model == "claude-sonnet-5"
    (home / "presets.toml").unlink()
    with pytest.raises(ValueError, match="house-style"):
        P.clan_config_from(a, base, home)


def test_clan_config_from_unknown_role_names_itself_as_brief(home):
    base = cfg()
    base.roles = {"orchestrator": C.RoleSpec(
        name="orchestrator", harness="claude", model="claude-fable-5-1", brief="orchestrator",
        writer=True)}
    got = P.clan_config_from(att(_one(name="qa", preset="opus-high", writer=False)), base, home)
    assert got.roles["qa"].brief == "qa"


def test_clan_config_from_drops_roles_the_amend_omits(home):
    base = C.ClanConfig.from_dict(
        {"channel": "harbor-15", "issue": 15, "repo": "acme/widget",
         "checkout": "/repo",
         "roles": {"orchestrator": {}, "reviewer": {}, "docs": {}}},
        C.load_catalog(home))
    a = att()                                   # developer + reviewer only
    got = P.clan_config_from(a, base, home)
    assert set(got.roles) == {"orchestrator", "developer", "reviewer"}
    assert "docs" not in got.roles              # an amend can drop a role


def test_clan_config_from_applies_user_defaults_to_non_catalog_roles(home):
    (home / "roles.toml").write_text("[defaults]\ncheckpoint_at = 50000\n")
    base = C.ClanConfig.from_dict(
        {"channel": "c", "issue": 15, "repo": "o/r", "checkout": "/repo",
         "roles": {"orchestrator": {}}}, C.load_catalog(home))
    got = P.clan_config_from(att(_one(name="qa", preset="opus-high")), base, home)
    assert got.roles["qa"].checkpoint_at == 50000   # the operator's [defaults]
    assert got.roles["qa"].brief == "qa"


def test_clan_config_from_takes_home_none():
    base = C.ClanConfig.from_dict(
        {"channel": "c", "issue": 15, "repo": "o/r", "checkout": "/repo",
         "roles": {"orchestrator": {}}}, C.load_catalog(None))
    got = P.clan_config_from(att(), base, None)      # regression: raised TypeError at e56420a
    assert set(got.roles) == {"orchestrator", "developer", "reviewer"}


def test_rejects_skills_that_are_not_specs():
    with pytest.raises(ValueError, match="skills"):
        P.validate_clan_attachment(att(_one(skills=["-rf"])), catalog())
    with pytest.raises(ValueError, match="skills"):
        P.validate_clan_attachment(att(_one(skills=["a b"])), catalog())
    assert P.validate_clan_attachment(att(_one(skills=["tdd", "owner/repo@name"])),
                                      catalog())


def test_skill_spec_documented_shapes_pass_and_hostile_ones_fail():
    for spec in ("tdd", "superpowers:tdd", "owner/repo@name",
                 "@anthropic/skill", "https://github.com/owner/repo"):
        assert P.validate_clan_attachment(att(_one(skills=[spec])), catalog())
    assert P.validate_clan_attachment(att(_one(skills=["owner/repo.name"])), catalog())
    for spec in ("--registry=http://evil", "a b", "$(id)", "../../etc/passwd",
                 "a/../../etc/passwd", "a/.."):
        with pytest.raises(ValueError, match="skills"):
            P.validate_clan_attachment(att(_one(skills=[spec])), catalog())


def test_byte_cap_is_exact_on_the_compact_form():
    import json as j

    def size(x):
        return len(j.dumps(x, separators=(",", ":")))

    roles = _fat_roles()
    roles[0]["writer"] = True
    over = size(att(roles=roles)) - 2600
    assert over > 0
    left = over                       # shave exactly `over` bytes off the whys
    for r in roles:
        cut = min(left, len(r["why"]))
        r["why"] = "w" * (len(r["why"]) - cut)
        left -= cut
        if not left:
            break
    assert not left, "fixture too small to reach the limit"
    exact = att(roles=roles)
    assert size(exact) == 2600
    assert P.validate_clan_attachment(exact, catalog())
    one_over = att(roles=[dict(r) for r in roles])
    one_over["roles"][0]["why"] += "w"
    with pytest.raises(ValueError, match="bytes"):
        P.validate_clan_attachment(one_over, catalog())


def test_every_packaged_preset_is_proposable():
    for pid in C.load_presets(None):
        assert P.validate_clan_attachment(att(_one(preset=pid)), catalog())
