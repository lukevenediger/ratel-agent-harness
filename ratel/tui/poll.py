"""Poll loop bodies. Each takes `post`, `cancelled` and `sleep` callables so the
bodies run in a Textual thread worker in the app and under a scripted clock in
tests. Read errors back off 1, 2, 4, 8, 10 s and the loop keeps going; the
payloads carry fixed text, never exception text."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

MESSAGE_INTERVAL = 0.5
PRESENCE_INTERVAL = 10.0
BACKOFF = (1, 2, 4, 8, 10)


@dataclass
class Batch:
    generation: int
    messages: list[dict]
    cursor: str | None


@dataclass
class PinsUpdate:
    generation: int
    pins: list[dict]
    heads: dict = field(default_factory=dict)


@dataclass
class PresenceUpdate:
    generation: int
    presence: list[dict]
    channels: list[dict]


@dataclass
class Storage:
    """`ok=False` means a read failed and the loop retries in `retry_in` seconds."""
    generation: int
    ok: bool
    retry_in: float | None = None


Post = Callable[[object], None]
Cancelled = Callable[[], bool]
Sleep = Callable[[float], None]


def pin_changed(msgs: list[dict]) -> bool:
    return any(m.get("pin") or "unpin" in m for m in msgs if isinstance(m, dict))


class _Backoff:
    def __init__(self, generation: int, post: Post, sleep: Sleep):
        self.generation, self.post, self.sleep = generation, post, sleep
        self.failures = 0

    def failed(self) -> None:
        delay = BACKOFF[min(self.failures, len(BACKOFF) - 1)]
        self.failures += 1
        self.post(Storage(self.generation, False, delay))
        self.sleep(delay)

    def succeeded(self) -> None:
        if self.failures:
            self.failures = 0
            self.post(Storage(self.generation, True))


def poll_messages(reader, channel: str, cursor: str | None, generation: int,
                  post: Post, cancelled: Cancelled, sleep: Sleep) -> None:
    """Tail `reader.since(channel, cursor)` every MESSAGE_INTERVAL; refetch pins
    only when a batch carries a truthy `pin` or an `unpin`."""
    backoff = _Backoff(generation, post, sleep)
    while not cancelled():
        try:
            msgs = reader.since(channel, cursor)
            pins = (reader.pins(channel), reader.heads(channel)) if pin_changed(msgs) else None
        except Exception:
            backoff.failed()
            continue
        backoff.succeeded()
        if msgs:
            cursor = msgs[-1]["id"]
            post(Batch(generation, msgs, cursor))
        if pins is not None:
            post(PinsUpdate(generation, *pins))
        sleep(MESSAGE_INTERVAL)


def poll_presence(reader, channel: str, generation: int,
                  post: Post, cancelled: Cancelled, sleep: Sleep) -> None:
    """Presence and the channel list every PRESENCE_INTERVAL."""
    backoff = _Backoff(generation, post, sleep)
    while not cancelled():
        try:
            update = PresenceUpdate(generation, reader.presence(channel), reader.channels())
        except Exception:
            backoff.failed()
            continue
        backoff.succeeded()
        post(update)
        sleep(PRESENCE_INTERVAL)
