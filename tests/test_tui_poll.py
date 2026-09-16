"""poll: loop bodies over a fake reader with a scripted sleep — no Textual, no threads."""
import pytest

from ratel.tui import events, poll
from ratel.tui.poll import Batch, PinsUpdate, PresenceUpdate, Storage


def m(i, **extra):
    return {"id": f"01M{i:023d}", "ts": "2026-09-16T10:00:00.000Z", "from": "a", "text": "t", "parent": None,
            "mentions": [], "attachments": [], "pin": False, **extra}


class FakeReader:
    def __init__(self, since=(), presence=()):
        self.since_script = list(since)
        self.presence_script = list(presence)
        self.since_calls: list[str | None] = []
        self.pins_calls = 0
        self.channel_calls = 0

    def _next(self, script):
        step = script.pop(0) if script else []
        if isinstance(step, Exception):
            raise step
        return step

    def since(self, channel, cursor):
        self.since_calls.append(cursor)
        return self._next(self.since_script)

    def pins(self, channel):
        self.pins_calls += 1
        return [m(99)]

    def heads(self, channel):
        return {"proposed": None, "approved": None}

    def presence(self, channel):
        return self._next(self.presence_script)

    def channels(self):
        self.channel_calls += 1
        return [{"name": channel_name} for channel_name in ("a", "b")]


class Script:
    """Records sleeps and cancels after `stop` of them, so every loop terminates."""
    def __init__(self, stop):
        self.sleeps: list[float] = []
        self.posted: list = []
        self.stop = stop

    def sleep(self, s):
        self.sleeps.append(s)

    def cancelled(self):
        return len(self.sleeps) >= self.stop

    def post(self, event):
        self.posted.append(event)


def run_messages(reader, stop, cursor=None, generation=7):
    s = Script(stop)
    poll.poll_messages(reader, "ch", cursor, generation, s.post, s.cancelled, s.sleep)
    return s


def test_batches_carry_the_generation_and_advance_the_cursor():
    r = FakeReader(since=[[m(1), m(2)], [], [m(3)]])
    s = run_messages(r, stop=3, cursor="01M0", generation=7)
    batches = [e for e in s.posted if isinstance(e, Batch)]
    assert [b.generation for b in batches] == [7, 7]
    assert [[x["id"] for x in b.messages] for b in batches] == [[m(1)["id"], m(2)["id"]], [m(3)["id"]]]
    assert [b.cursor for b in batches] == [m(2)["id"], m(3)["id"]]
    assert r.since_calls == ["01M0", m(2)["id"], m(2)["id"]]
    assert s.sleeps == [poll.MESSAGE_INTERVAL] * 3


def test_pins_are_refetched_only_when_a_batch_pins_or_unpins():
    r = FakeReader(since=[[m(1)], [m(2, pin=True)], [m(3, unpin="01M0")], [m(4, pin="01M0")]])
    s = run_messages(r, stop=4)
    assert r.pins_calls == 3
    kinds = [type(e).__name__ for e in s.posted]
    assert kinds == ["Batch", "Batch", "PinsUpdate", "Batch", "PinsUpdate", "Batch", "PinsUpdate"]
    update = next(e for e in s.posted if isinstance(e, PinsUpdate))
    assert update.generation == 7 and update.pins == [m(99)] and update.heads == {"proposed": None, "approved": None}


def test_read_errors_back_off_and_the_loop_recovers():
    boom = [OSError("disk"), ValueError("schema"), OSError(), OSError(), OSError(), OSError(), OSError()]
    r = FakeReader(since=boom + [[m(1)]])
    s = run_messages(r, stop=9)
    assert s.sleeps == [1, 2, 4, 8, 10, 10, 10, poll.MESSAGE_INTERVAL, poll.MESSAGE_INTERVAL]
    storage = [e for e in s.posted if isinstance(e, Storage)]
    assert [(e.ok, e.retry_in) for e in storage] == [(False, 1), (False, 2), (False, 4), (False, 8), (False, 10),
                                                      (False, 10), (False, 10), (True, None)]
    assert all(e.generation == 7 for e in storage)
    assert isinstance(s.posted[-1], Batch)
    assert not any("disk" in str(vars(e)) for e in s.posted)   # fixed text, never exception text


def test_a_failing_pin_refetch_counts_as_a_read_error_and_the_batch_is_re_read():
    r = FakeReader(since=[[m(1, pin=True)], [m(1, pin=True), m(2)]])   # the cursor did not advance
    pins, errors = r.pins, [OSError("locked")]
    r.pins = lambda channel: (_ for _ in ()).throw(errors.pop()) if errors else pins(channel)
    s = run_messages(r, stop=2)
    kinds = [type(e).__name__ for e in s.posted]
    assert kinds == ["Storage", "Storage", "Batch", "PinsUpdate"] and s.sleeps == [1, poll.MESSAGE_INTERVAL]
    assert r.since_calls == [None, None] and [x["id"] for x in s.posted[2].messages] == [m(1)["id"], m(2)["id"]]


def test_cancellation_stops_the_loop_before_the_next_read():
    r = FakeReader(since=[[m(1)]] * 10)
    s = run_messages(r, stop=2)
    assert len(r.since_calls) == 2 and len(s.posted) == 2


def test_cancelled_before_the_first_read_posts_nothing():
    r = FakeReader(since=[[m(1)]])
    s = run_messages(r, stop=0)
    assert r.since_calls == [] and s.posted == []


def test_presence_loop_posts_presence_and_channels_every_ten_seconds():
    r = FakeReader(presence=[[{"agent": "a", "online": True}], OSError(), []])
    s = Script(3)
    poll.poll_presence(r, "ch", 3, s.post, s.cancelled, s.sleep)
    assert s.sleeps == [poll.PRESENCE_INTERVAL, 1, poll.PRESENCE_INTERVAL]
    updates = [e for e in s.posted if isinstance(e, PresenceUpdate)]
    assert [u.generation for u in updates] == [3, 3]
    assert updates[0].presence == [{"agent": "a", "online": True}] and updates[0].channels == [{"name": "a"}, {"name": "b"}]
    assert [type(e).__name__ for e in s.posted] == ["PresenceUpdate", "Storage", "Storage", "PresenceUpdate"]


def test_pin_changed_detects_truthy_pin_or_unpin_key():
    assert not poll.pin_changed([m(1), m(2, pin=False)])
    assert poll.pin_changed([m(1, pin=True)]) and poll.pin_changed([m(1, pin="01M0")]) and poll.pin_changed([m(1, unpin="x")])


@pytest.mark.parametrize("cls, payload", [
    (events.MessagesArrived, Batch(4, [m(1)], m(1)["id"])),
    (events.PinsChanged, PinsUpdate(4, [], {})),
    (events.PresenceChanged, PresenceUpdate(4, [], [])),
    (events.StorageChanged, Storage(4, False, 2)),
])
def test_textual_events_wrap_poll_payloads_and_expose_the_generation(cls, payload):
    event = cls(payload)
    assert event.generation == 4 and event.payload is payload
    assert events.wrap(payload).__class__ is cls
