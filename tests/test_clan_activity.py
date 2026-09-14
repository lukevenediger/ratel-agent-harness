"""The activity model behind the live team map.

Every test uses a temp home and a fake harness. `activity()` raises ClanError on
a drift and never sys.exit()s — see test_clan_cli for that boundary.
"""
import json
import os
import re

import pytest

from ratel.bus import Bus, now_iso
from ratel.clan import config as C
from ratel.clan import session as S


def _seed(home, ch="c", roles=None, tabs=None, watch=None, contexts=None,
          rounds=None, round_pid=None, models=None, checkpoint_at=200000, prose=None):
    """A clan.toml plus a chosen slice of clan.state.json and harness dirs."""
    roles = roles if roles is not None else {"developer": ("fake", True)}
    b = Bus(home, ch)
    cd = home / "channels" / ch
    d = cd / "clan"
    d.mkdir(parents=True, exist_ok=True)
    toml = f'channel = "{ch}"\nissue = 42\nrepo = "o/r"\ncheckout = "/tmp/r"\n'
    for name, (harness, writer) in roles.items():
        model = (models or {}).get(name, "fake")
        toml += (f'\n[roles.{name}]\nharness = "{harness}"\nmodel = "{model}"\n'
                 f'writer = {"true" if writer else "false"}\ncheckpoint_at = {checkpoint_at}\n')
        for key, value in (prose or {}).get(name, {}).items():
            toml += f'{key} = "{value}"\n'
    (d / "clan.toml").write_text(toml)
    state = {}
    if tabs is not None:
        state["tabs"] = tabs
    if watch is not None:
        state["watch"] = watch
    C.update_state(C.ClanPaths(home, ch), lambda s: s.update(state))
    for role, tokens in (contexts or {}).items():
        hd = cd / "harness" / role
        hd.mkdir(parents=True, exist_ok=True)
        (hd / "context.txt").write_text(str(tokens))
    for role, records in (rounds or {}).items():
        hd = cd / "harness" / role
        hd.mkdir(parents=True, exist_ok=True)
        (hd / "rounds.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    for role, pid in (round_pid or {}).items():
        hd = cd / "harness" / role
        hd.mkdir(parents=True, exist_ok=True)
        (hd / "round.pid").write_text(json.dumps({"pid": pid, "pgid": pid, "started": ""}))
    return C.ClanPaths(home, ch), b


def _row(model, role="developer"):
    return next(r for r in model["roles"] if r["role"] == role)


def test_activity_row_shape_is_exact(home):
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}})
    row = _row(S.activity(paths))
    assert set(row) == {
        "role", "state", "confidence", "since", "reasons", "up", "headless",
        "harness", "model", "writer", "effort", "model_expired", "branch",
        "worktree", "tab_id", "pane_id", "nudged_at", "nudged_thread", "nudged_by",
        "escalated", "pending", "context_tokens", "checkpoint_at",
        "last_checkpoint", "last_nudge", "busy", "round", "awaiting"}
    assert row["state"] == "idle" and row["confidence"] == "weak"
    assert row["reasons"] == [S.R_QUIET.format(seconds=S.QUIET_WINDOW_S)]
    assert row["up"] is True and row["round"] is None and row["busy"] is False


def test_without_a_tab_the_role_is_gone(home):
    paths, _ = _seed(home)                       # no tabs recorded
    row = _row(S.activity(paths))
    assert row["state"] == "gone" and row["up"] is False
    assert row["reasons"] == [S.R_NO_TAB]


def test_a_non_dict_tab_is_ignored(home):
    """`clan.state.json` is agent-written: a truthy non-dict tab value must
    read as no tab, not crash the model (and the /clan route with it)."""
    paths, _ = _seed(home, tabs={"developer": "not-a-dict"})
    row = _row(S.activity(paths))
    assert row["state"] == "gone" and row["up"] is False


def test_a_malformed_clan_toml_raises_clan_error(home):
    """The board's `/clan` route catches only ClanError; the cfg loader's own
    failures must be wrapped, not escape as TOMLDecodeError/KeyError."""
    paths, _ = _seed(home)
    paths.clan_toml.write_text("this is not [ valid toml")
    with pytest.raises(S.ClanError, match="clan.toml"):
        S.activity(paths)


def test_a_broken_roles_toml_names_the_catalog_not_clan_toml(home):
    """`load_catalog` parses the user's roles.toml too; a failure there must not
    be reported as a broken clan.toml, which would send the operator to the
    wrong file."""
    paths, _ = _seed(home)
    (home / "roles.toml").write_text("this is not [ valid toml")
    with pytest.raises(S.ClanError, match="roles.toml"):
        S.activity(paths)


def test_a_role_name_is_length_bounded(home):
    """A 5 kB role name is mentionable by charset but makes the map unreadable;
    the same drop-and-warn path applies when it is too long."""
    long = "a" * 5000
    paths, _ = _seed(home, roles={long: ("fake", True)})
    model = S.activity(paths)
    assert model["roles"] == []
    assert any("non-mentionable" in w for w in model["warnings"])


def test_a_non_string_expiry_does_not_expire_or_crash(home):
    """A hand-edited `expires = [1]` is not an expiry: it must not raise from
    `fromisoformat` and must not mark the model expired."""
    paths, _ = _seed(home)
    (home / "models.toml").write_text('[models."fake"]\nexpires = [1]\n')
    model = S.activity(paths)
    assert [r["role"] for r in model["roles"]] == ["developer"]
    assert not any("expired" in w for w in model["warnings"])


def test_a_structurally_broken_models_toml_raises_clan_error(home):
    """Bad TOML or a missing [models] table is wrapped like the role catalog, so
    the board route answers 200 instead of the handler thread dying."""
    paths, _ = _seed(home)
    (home / "models.toml").write_text("this is not [ valid toml")
    with pytest.raises(S.ClanError, match="models.toml"):
        S.activity(paths)


def test_activity_without_a_bus_file_still_returns_roles(home):
    """A read-only `Bus` does not create bus.jsonl; a clan with no messages must
    still serve its roles rather than raise FileNotFoundError (which the board
    route does not catch)."""
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}})
    paths.channel_dir.joinpath("channel.sqlite3").unlink()
    model = S.activity(paths)
    assert [r["role"] for r in model["roles"]] == ["developer"]


def test_activity_reads_the_channels_own_bus_not_clan_tomls_channel(home):
    """The channel directory is authoritative: a clan.toml whose `channel` names
    another channel must not make this one read that channel's bus."""
    paths, b = _seed(home, ch="here", tabs={"developer": {"tab_id": 2, "pane_id": 7}})
    paths.clan_toml.write_text(
        paths.clan_toml.read_text().replace('channel = "here"', 'channel = "elsewhere"'))
    b.post("developer", "a post in the real directory")
    assert _row(S.activity(paths))["state"] == "online"


def test_over_the_checkpoint_is_context_full(home):
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     contexts={"developer": 250000})
    row = _row(S.activity(paths))
    assert row["state"] == "context-full" and row["confidence"] == "measured"
    assert row["reasons"] == [S.R_CONTEXT.format(tokens=250000, limit=200000)]


def test_a_delivered_nudge_is_busy(home):
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     watch={"nudged": {"developer": {"id": "01ABC", "ts": 1.0,
                                                     "at": now_iso(), "escalated": False}}})
    row = _row(S.activity(paths))
    assert row["state"] == "busy" and row["confidence"] == "inferred"
    assert row["nudged_at"] and row["nudged_thread"] == "01ABC" and row["nudged_by"] is None


def test_an_undelivered_mention_is_queued(home):
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     watch={"pending": {"developer": {"n": 2, "senders": ["orchestrator"],
                                                      "thread": None,
                                                      "since": now_iso()}}})
    row = _row(S.activity(paths))
    assert row["state"] == "queued" and row["confidence"] == "inferred"
    assert row["pending"] is True
    assert row["reasons"] == [S.R_PENDING.format(count=2)]


def test_escalated_silence_is_stuck(home):
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     watch={"nudged": {"developer": {"id": "", "ts": 1.0, "at": now_iso(),
                                                     "escalated": True}}})
    row = _row(S.activity(paths))
    assert row["state"] == "stuck" and row["confidence"] == "inferred"
    assert row["escalated"] is True and row["reasons"][0].startswith("silent for")


def awaiting(**over):
    """A fresh watcher record: `at` is taken now, per test, so the age the
    model reports is zero minutes however long the run has been going."""
    return {"at": now_iso(), "ts": 1.0, "kind": "permission", "family": "opencode",
            "tab_id": 2, "text": "△ Permission required · Access external directory /x",
            "escalated": True, **over}


def test_an_awaiting_record_is_awaiting_operator(home):
    rec = awaiting()
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     watch={"awaiting": {"developer": rec}})
    row = _row(S.activity(paths))
    assert row["state"] == "awaiting-operator" and row["confidence"] == "measured"
    assert row["reasons"] == [S.R_AWAITING.format(minutes=0)]
    assert row["since"] == rec["at"]
    assert row["awaiting"] == {"at": rec["at"], "text": rec["text"]}
    assert row["busy"] is False


def test_awaiting_outranks_busy_and_stuck(home):
    """A dialog explains the silence: the role is not stuck, it is waiting on
    the operator — even with an escalated nudge on record."""
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     watch={"awaiting": {"developer": awaiting()},
                            "nudged": {"developer": {"id": "", "ts": 1.0, "at": now_iso(),
                                                     "escalated": True}}})
    row = _row(S.activity(paths))
    assert row["state"] == "awaiting-operator"
    assert row["busy"] is True                     # still owes a reply once unblocked
    assert row["reasons"][0].startswith("a permission prompt")


def test_a_gone_role_is_gone_even_with_an_awaiting_record(home):
    paths, _ = _seed(home, watch={"awaiting": {"developer": awaiting()}})
    assert _row(S.activity(paths))["state"] == "gone"


@pytest.mark.parametrize("rec", [
    "a string", 7, ["list"], {"kind": "other", "at": "x", "text": "y"}, {"at": "x"}])
def test_a_malformed_awaiting_record_is_ignored(home, rec):
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     watch={"awaiting": {"developer": rec}})
    row = _row(S.activity(paths))
    assert row["state"] != "awaiting-operator" and row["awaiting"] is None


def test_awaiting_text_is_capped_and_control_free_on_the_way_out(home):
    """The record is agent-writable state: re-sanitise on read, never trust
    what the watcher wrote."""
    rec = awaiting(text="a\x1bb\x00c" + "x" * 400, at=12)
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     watch={"awaiting": {"developer": rec}})
    row = _row(S.activity(paths))
    assert row["state"] == "awaiting-operator"
    assert row["awaiting"]["text"].startswith("abcxxx") and len(row["awaiting"]["text"]) == 200
    assert row["awaiting"]["at"] is None and row["reasons"] == [S.R_AWAITING_UNKNOWN]


def test_a_legacy_nudge_without_at_is_unknown_age(home):
    """W1 parked this for W4b: an entry with no `at` predates the wall clock, so
    its age is unknown — never "nudged 0 min ago", which reads as the freshest
    possible state."""
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     watch={"nudged": {"developer": {"id": "01ABC", "ts": 1.0}}})
    row = _row(S.activity(paths))
    assert row["state"] == "busy" and row["nudged_at"] is None
    assert row["reasons"] == [S.R_NUDGED_UNKNOWN]


def test_a_legacy_escalated_nudge_without_at_is_unknown_age(home):
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     watch={"nudged": {"developer": {"id": "", "ts": 1.0, "escalated": True}}})
    row = _row(S.activity(paths))
    assert row["state"] == "stuck" and row["reasons"] == [S.R_ESCALATED_UNKNOWN]


def test_a_timed_out_round_is_stuck(home):
    paths, _ = _seed(home, roles={"worker": ("opencode-run", True)},
                     tabs={"worker": {"tab_id": 3, "pane_id": 8, "harness": "opencode-run"}},
                     rounds={"worker": [{"round": 4, "returncode": "timeout",
                                         "ts": "2026-09-11T10:00:00Z",
                                         "output": "SECRET-OUTPUT", "prompt": "SECRET-PROMPT"}]})
    row = _row(S.activity(paths), "worker")
    assert row["state"] == "stuck" and row["confidence"] == "measured"
    assert "timed out" in row["reasons"][0]
    assert row["round"] == {"n": 4, "running": False, "outcome": "timeout",
                            "last_ts": "2026-09-11T10:00:00Z", "started_s": None}


def test_a_running_round_is_busy(home):
    paths, _ = _seed(home, roles={"worker": ("opencode-run", True)},
                     tabs={"worker": {"tab_id": 3, "pane_id": 8, "harness": "opencode-run"}},
                     rounds={"worker": [{"round": 4, "returncode": 0,
                                         "ts": "2026-09-11T10:00:00Z"}]},
                     round_pid={"worker": os.getpid()})
    row = _row(S.activity(paths), "worker")
    assert row["state"] == "busy" and row["confidence"] == "measured"
    assert row["round"]["running"] is True and row["round"]["n"] == 4
    assert any(r.startswith("headless round 4 running") for r in row["reasons"])


def test_a_recent_post_is_online(home):
    paths, b = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}})
    b.post("developer", "just posted")
    row = _row(S.activity(paths))
    assert row["state"] == "online" and row["confidence"] == "inferred"


def test_a_tab_is_never_offline(home):
    """The whole point: a running process is never called offline. Every state
    a role with a tab can reach excludes `offline`."""
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}})
    assert _row(S.activity(paths))["state"] != "offline"


def test_the_quiet_interactive_message_is_exact(home):
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}})
    assert _row(S.activity(paths))["reasons"] == [
        "no activity measured in the last 90s — the harness may be waiting at its prompt"]


def test_reasons_are_a_closed_numeric_vocabulary(home):
    """Every reason that can be produced renders one of the closed templates,
    with only digits in the placeholders — never free text from disk."""
    def pattern(t):
        return r"\d+".join(re.escape(p) for p in re.split(r"\{[a-z]+\}", t))

    pats = [pattern(t) for t in S.REASONS]
    seen = []

    def check(paths, role="developer"):
        seen.extend(_row(S.activity(paths), role)["reasons"])

    check(_seed(home / "a")[0])
    check(_seed(home / "b",
                tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                watch={"nudged": {"developer": {"at": now_iso(), "ts": 1.0,
                                                "escalated": True}}})[0])
    check(_seed(home / "c", tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                contexts={"developer": 250000})[0])
    check(_seed(home / "d", tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                watch={"pending": {"developer": {"n": 1, "senders": [],
                                                 "since": now_iso()}}})[0])
    check(_seed(home / "e", tabs={"developer": {"tab_id": 2, "pane_id": 7}})[0])
    check(_seed(home / "f", tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                watch={"awaiting": {"developer": awaiting()}})[0])
    check(_seed(home / "g", tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                watch={"awaiting": {"developer": awaiting(at=None)}})[0])
    assert seen, "no reasons produced"
    for reason in seen:
        assert not re.search(r"\{[a-z]+\}", reason), reason
        assert any(re.fullmatch(p, reason) for p in pats), reason


def test_round_output_and_prompt_never_leave_the_model(home):
    paths, b = _seed(
        home, roles={"worker": ("opencode-run", True)},
        tabs={"worker": {"tab_id": 3, "pane_id": 8, "harness": "opencode-run"}},
        rounds={"worker": [{"round": 1, "returncode": 0, "ts": "2026-09-11T10:00:00Z",
                            "output": "MARKER-OUTPUT-XYZZY",
                            "prompt": "MARKER-PROMPT-XYZZY"}]})
    blob = json.dumps(S.activity(paths))
    assert "MARKER-OUTPUT-XYZZY" not in blob and "MARKER-PROMPT-XYZZY" not in blob
    assert _row(S.activity(paths), "worker")["round"]["outcome"] == "ok"


def test_a_token_rise_marks_the_role_busy(home):
    """The process-local sampler: an interactive role whose context grew since
    the last sample is emitting — the only honest liveness signal it has."""
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     contexts={"developer": 100})
    samples = {}
    assert _row(S.activity(paths, samples=samples))["state"] == "idle"
    (paths.harness_dir("developer") / "context.txt").write_text("220")
    row = _row(S.activity(paths, samples=samples))
    assert row["state"] == "busy" and row["confidence"] == "measured"
    assert S.R_EMITTING in row["reasons"]


def test_a_non_mentionable_role_name_is_dropped(home):
    paths, _ = _seed(home, roles={"good": ("fake", True)})
    ct = paths.clan_toml
    ct.write_text(ct.read_text() + '\n[roles."bad name"]\nharness = "fake"\n'
                                     'model = "fake"\nwriter = false\n')
    model = S.activity(paths)
    assert [r["role"] for r in model["roles"]] == ["good"]
    assert any("non-mentionable" in w for w in model["warnings"])


def test_an_unknown_harness_drops_the_role(home):
    paths, _ = _seed(home, roles={"developer": ("martian", True)})
    model = S.activity(paths)
    assert model["roles"] == [] and any("harness" in w for w in model["warnings"])


def test_a_malformed_model_id_drops_the_role(home):
    paths, _ = _seed(home, models={"developer": "<bad>"})
    model = S.activity(paths)
    assert model["roles"] == [] and any("model id" in w for w in model["warnings"])


def test_a_bad_branch_is_dropped_and_numbers_are_coerced(home):
    paths, _ = _seed(home, tabs={"developer": {"tab_id": "3", "pane_id": "9",
                                               "branch": "bad branch"}})
    row = _row(S.activity(paths))
    assert row["branch"] is None
    assert row["tab_id"] == 3 and row["pane_id"] == 9 and isinstance(row["tab_id"], int)


def test_a_non_numeric_checkpoint_at_is_not_nan(home):
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     contexts={"developer": 250000}, checkpoint_at='"200k"')
    row = _row(S.activity(paths))
    assert row["checkpoint_at"] is None
    assert row["state"] != "context-full"          # no threshold to cross


def test_clan_prose_never_reaches_the_model(home):
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     prose={"developer": {"brief": "SECRET-BRIEF", "why": "SECRET-WHY",
                                          "playbook": "SECRET-PLAYBOOK"}})
    assert "SECRET-" not in json.dumps(S.activity(paths))


def test_activity_reads_never_write_a_channel_dir(home):
    paths, _ = _seed(home, tabs={"developer": {"tab_id": 2, "pane_id": 7}},
                     watch={"nudged": {"developer": {"at": now_iso(), "ts": 1.0}}},
                     contexts={"developer": 100})
    state_p = paths.channel_dir / "channel.sqlite3"
    before = (state_p.read_bytes(), state_p.stat().st_mtime_ns)
    S.activity(paths, samples={})
    assert (state_p.read_bytes(), state_p.stat().st_mtime_ns) == before
