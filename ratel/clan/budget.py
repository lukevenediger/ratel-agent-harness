"""Per-launch headless limits, independent of conversation checkpoints."""
import math
from dataclasses import dataclass

STOP_REASONS = {
    'max_rounds': 'maximum rounds reached',
    'max_seconds': 'maximum elapsed time reached',
    'max_failures': 'consecutive failure limit reached',
}


@dataclass(frozen=True)
class Limits:
    rounds: int = 100
    seconds: float = 28800
    failures: int = 3
    round_usd: float | None = None

    @classmethod
    def from_env(cls, env, harness):
        def number(key, default, integer=False):
            raw = env.get(key, default)
            try:
                value = int(raw) if integer else float(raw)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                raise ValueError(f'{key} must be a finite positive number') from None
            return value
        usd = number('CLAN_ROUND_BUDGET_USD', None) if 'CLAN_ROUND_BUDGET_USD' in env else None
        if usd is not None and harness != 'claude-p':
            raise ValueError('CLAN_ROUND_BUDGET_USD is supported only by claude-p')
        return cls(number('CLAN_MAX_ROUNDS', 100, True), number('CLAN_MAX_SECONDS', 28800),
                   number('CLAN_MAX_FAILURES', 3, True), usd)


class Budget:
    def __init__(self, limits, clock):
        self.limits = limits
        self.clock = clock
        self.started = clock()
        self.rounds = 0
        self.failures = 0

    @property
    def deadline(self):
        return self.started + self.limits.seconds

    def reason(self):
        if self.clock() >= self.deadline:
            return 'max_seconds'
        if self.failures >= self.limits.failures:
            return 'max_failures'
        if self.rounds >= self.limits.rounds:
            return 'max_rounds'
        return None

    def finish(self, returncode):
        self.failures = 0 if returncode == 0 else self.failures + 1

    def snapshot(self):
        return {'rounds': self.rounds, 'consecutive_failures': self.failures,
                'elapsed_seconds': max(0, self.clock() - self.started),
                'limits': {'rounds': self.limits.rounds, 'seconds': self.limits.seconds,
                           'failures': self.limits.failures, 'round_budget_usd': self.limits.round_usd}}



def stopped_reason(state, role):
    runs = state.get('runs')
    run = runs.get(role) if isinstance(runs, dict) else None
    reason = run.get('reason') if isinstance(run, dict) else None
    return reason if isinstance(reason, str) and reason in STOP_REASONS else None
