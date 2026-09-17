"""Textual messages the poll workers post to the app. Each carries the
generation of the channel/thread selection it belongs to; handlers drop
anything from an older generation."""
from __future__ import annotations

from textual.message import Message

from .poll import Batch, PinsUpdate, PresenceUpdate, Storage


class PollEvent(Message):
    def __init__(self, payload) -> None:
        super().__init__()
        self.payload = payload

    @property
    def generation(self) -> int:
        return self.payload.generation


class MessagesArrived(PollEvent):
    payload: Batch


class PinsChanged(PollEvent):
    payload: PinsUpdate


class PresenceChanged(PollEvent):
    payload: PresenceUpdate


class StorageChanged(PollEvent):
    payload: Storage


class ThreadLoaded(Message):
    """A thread fetched by the thread worker; `thread_generation` guards against a
    later selection overtaking an earlier fetch."""
    def __init__(self, generation: int, thread_generation: int, thread: dict | None) -> None:
        super().__init__()
        self.generation, self.thread_generation, self.thread = generation, thread_generation, thread


_WRAP = {Batch: MessagesArrived, PinsUpdate: PinsChanged, PresenceUpdate: PresenceChanged, Storage: StorageChanged}


def wrap(payload) -> PollEvent:
    """The Textual message for a poll payload — the `post` callable the app hands the loops."""
    return _WRAP[type(payload)](payload)
