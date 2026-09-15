import os
import threading
import time

import pytest

from ratel.clan.budget import Budget, Limits
from ratel.clan.loop import NudgeLoop


def test_budget_rounds_failures_and_clock_are_independent():
    now = [0]
    budget = Budget(Limits(rounds=3, seconds=10, failures=2), lambda: now[0])
    budget.rounds += 1
    budget.finish(1)
    assert budget.reason() is None
    budget.finish(0)
    assert budget.failures == 0
    budget.finish('timeout')
    budget.finish('error')
    assert budget.reason() == 'max_failures'
    budget.finish(0)
    budget.rounds = 3
    assert budget.reason() == 'max_rounds'
    now[0] = 10
    assert budget.reason() == 'max_seconds'


@pytest.mark.parametrize('key,value', [('CLAN_MAX_ROUNDS', '0'), ('CLAN_MAX_ROUNDS', '2.5'),
    ('CLAN_MAX_SECONDS', 'nan'), ('CLAN_MAX_SECONDS', 'inf'), ('CLAN_MAX_FAILURES', '-1'),
    ('CLAN_ROUND_BUDGET_USD', '0')])
def test_invalid_limits_refused(key, value):
    with pytest.raises(ValueError, match=key):
        Limits.from_env({key: value}, 'claude-p')


def test_spend_limit_is_never_silently_ignored():
    assert Limits.from_env({'CLAN_ROUND_BUDGET_USD': '1.25'}, 'claude-p').round_usd == 1.25
    with pytest.raises(ValueError, match='only by claude-p'):
        Limits.from_env({'CLAN_ROUND_BUDGET_USD': '1'}, 'opencode-run')


def test_idle_nudge_input_stops_at_deadline():
    read, write = os.pipe()
    ran = []
    # A real blocking pipe reproduces the idle terminal, not just StringIO EOF.
    with os.fdopen(read) as stream:
        loop = NudgeLoop(ran.append, stdin=stream, deadline=time.monotonic() + 0.1)
        worker = threading.Thread(target=loop.run)
        worker.start()
        worker.join(2)
        os.close(write)
        assert not worker.is_alive() and ran == []


@pytest.mark.parametrize('runs', [[], 'bad', {'developer': []}, {'developer': {'reason': []}},
                                 {'developer': {'reason': 'invented'}}])
def test_untrusted_stop_metadata_is_ignored(runs):
    from ratel.clan.budget import stopped_reason
    assert stopped_reason({'runs': runs}, 'developer') is None


def test_deadline_reader_propagates_input_failure():
    class BrokenInput:
        def __iter__(self):
            raise OSError('input unavailable')
    with pytest.raises(OSError, match='input unavailable'):
        NudgeLoop(lambda line: None, stdin=BrokenInput(), deadline=time.monotonic() + 1).run()
