"""Bounded diagnostic tails; full output is streamed to files, never accumulated."""
from collections import deque


class OutputTail:
    def __init__(self, lines=40, characters=65536):
        self.parts = deque()
        self.limit = characters
        self.lines = lines
        self.size = 0
        self.total = 0

    def append(self, text):
        self.total += len(text)
        text = text[-self.limit:]
        self.parts.append(text)
        self.size += len(text)
        while self.size > self.limit or len(self.parts) > self.lines:
            excess = self.size - self.limit
            first = self.parts.popleft()
            self.size -= len(first)
            if 0 < excess < len(first) and len(self.parts) < self.lines:
                rest = first[excess:]
                self.parts.appendleft(rest)
                self.size += len(rest)

    def text(self):
        return ''.join(self.parts)

    @property
    def truncated(self):
        return self.total > self.size
