"""`unique_session` — a zellij session name that is free AND short enough.

zellij binds its IPC socket at `$TMPDIR/zellij-<uid>/contract_version_1/<session>`
and a unix socket path caps at 103 bytes. macOS `$TMPDIR` is ~49 bytes on its
own, which leaves about 24 characters for the whole session name.
"""
import datetime

import pytest

from ratel.clan import session as S


def taken(*names):
    held = set(names)
    return lambda n: n in held


NOW = datetime.datetime(2026, 9, 10, 20, 41)


# ---- the budget --------------------------------------------------------
def test_the_budget_comes_from_tmpdir_and_the_uid(monkeypatch):
    monkeypatch.setattr(S.os, "getuid", lambda: 501)
    env = {"TMPDIR": "/var/folders/r_/tfbstn_x5338ck_wxjx926q40000gn/T/"}
    # the exact path from the failure this fixed: 79 bytes of prefix
    assert S.session_name_budget(env) == 103 - 79


def test_a_trailing_slash_on_tmpdir_is_not_counted_twice(monkeypatch):
    monkeypatch.setattr(S.os, "getuid", lambda: 501)
    assert S.session_name_budget({"TMPDIR": "/tmp/"}) == S.session_name_budget({"TMPDIR": "/tmp"})


def test_no_tmpdir_is_never_more_generous_than_tmp(monkeypatch):
    """Whatever the fallback resolves to, an unset TMPDIR must not budget for
    a shorter path than the one it would guess."""
    monkeypatch.setattr(S.os, "getuid", lambda: 501)
    assert S.session_name_budget({}) <= S.session_name_budget({"TMPDIR": "/tmp"})


# ---- the name ----------------------------------------------------------
def test_the_full_stamp_when_the_whole_name_fits():
    assert S.unique_session("harbor-42", taken(), now=NOW, budget=69) == \
        "harbor-42-0910-2041"


def test_the_stamp_loses_its_date_before_the_channel_loses_a_character():
    # "example-site-1-0910-2041" is 24; at 23 the date goes first.
    assert S.unique_session("example-site-1", taken(), now=NOW, budget=23) == \
        "example-site-1-2041"


def test_a_held_name_gets_a_counter():
    assert S.unique_session("harbor-42", taken("harbor-42-0910-2041"),
                            now=NOW, budget=69) == "harbor-42-0910-2041-2"


def test_the_counter_walks_past_every_held_name():
    held = taken("harbor-42-0910-2041", "harbor-42-0910-2041-2",
                 "harbor-42-0910-2041-3")
    assert S.unique_session("harbor-42", held, now=NOW, budget=69) == \
        "harbor-42-0910-2041-4"


def test_the_channel_is_trimmed_from_the_head_only_as_a_last_resort():
    """The tail carries the issue number — it is what tells two clans on one
    repo apart, so the head is what goes."""
    name = S.unique_session("example-site-1", taken("example-site-1-2041"),
                            now=NOW, budget=19)
    assert len(name) <= 19
    assert name.endswith("-2041-2") and name != "example-site-1-2041-2"
    assert "site-1" in name


def test_every_candidate_stays_inside_the_budget():
    for budget in range(12, 40):
        name = S.unique_session("example-site-1", taken(), now=NOW, budget=budget)
        assert len(name) <= budget, (budget, name)


def test_it_gives_up_rather_than_spinning_forever():
    with pytest.raises(SystemExit, match="session name"):
        S.unique_session("harbor-42", lambda n: True, now=NOW, budget=69)


def test_it_refuses_a_budget_too_small_to_name_anything():
    with pytest.raises(SystemExit, match="socket path"):
        S.unique_session("harbor-42", taken(), now=NOW, budget=6)


def test_an_exited_session_still_owns_its_name():
    # zellij lists EXITED sessions for `attach` to resurrect; they are taken.
    assert S.unique_session("x", taken("x-0910-2041"), now=NOW, budget=69) == "x-0910-2041-2"


# ---- the fallback when TMPDIR is not set -------------------------------
def test_an_unset_tmpdir_budgets_for_the_longest_it_could_be(monkeypatch):
    """zellij reads TMPDIR; when it is not set in OUR environment we cannot
    know what zellij will see, so budget for the worst case rather than
    predicting /tmp and overrunning a 49-byte per-user temp dir."""
    monkeypatch.setattr(S.os, "getuid", lambda: 501)
    monkeypatch.setattr(S, "_darwin_temp_dir",
                        lambda: "/var/folders/r_/tfbstn_x5338ck_wxjx926q40000gn/T/")
    assert S.session_name_budget({}) == 103 - 79


def test_a_set_tmpdir_is_taken_at_its_word(monkeypatch):
    """A short TMPDIR is not second-guessed: zellij uses what it is given."""
    monkeypatch.setattr(S.os, "getuid", lambda: 501)
    monkeypatch.setattr(S, "_darwin_temp_dir",
                        lambda: "/var/folders/r_/tfbstn_x5338ck_wxjx926q40000gn/T/")
    assert S.session_name_budget({"TMPDIR": "/tmp/zj1/"}) > S.session_name_budget({})


# ---- zellij's own arithmetic wins --------------------------------------
def test_the_budget_is_recomputed_from_zellij_s_error():
    msg = ("zellij attach --create-background x: Error: the IPC socket path is too long "
           "(104 bytes, max 103):/var/folders/r_/tfbstn_x5338ck_wxjx926q40000gn/T/"
           "zellij-1000/contract_version_1/example-site-1-0910-2041")
    assert S.budget_from_error(msg, "example-site-1-0910-2041") == 103 - 80


def test_an_unrelated_error_yields_no_budget():
    assert S.budget_from_error("Error: something else entirely", "x") is None
