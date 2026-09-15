"""One nudge line in, one turn out.

A nudge is typed into an agent's terminal, so from the process's side it is
just a line on stdin. The fake harness and the headless kinds both take that
shape, which is why this is its own tiny module rather than part of either.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, TextIO


class NudgeLoop:
    def __init__(self, runner: Callable[[str], object], stdin: TextIO | None = None,
                 log: Path | str | None = None, stderr: TextIO | None = None,
                 deadline: float | None = None, fail_fast: bool = False):
        self.deadline = deadline
        self.fail_fast = fail_fast
        self.runner = runner
        self.stdin = stdin if stdin is not None else sys.stdin
        self.log = Path(log) if log else None
        self.stderr = stderr if stderr is not None else sys.stderr

    def _lines(self):
        if self.deadline is None:
            yield from self.stdin
            return
        # A daemon reader permits deadline expiry while a pane is idle, including
        # buffered pipes. The bounded queue prevents reading an unlimited backlog.
        pending = queue.Queue(maxsize=1)
        stopped = threading.Event()
        end = object()

        def read():
            completion = end
            try:
                for line in self.stdin:
                    while not stopped.is_set():
                        try:
                            pending.put(line, timeout=0.1)
                            break
                        except queue.Full:
                            pass
                    if stopped.is_set():
                        return
            except Exception as exc:
                completion = exc
            finally:
                while not stopped.is_set():
                    try:
                        pending.put(completion, timeout=0.1)
                        break
                    except queue.Full:
                        pass

        threading.Thread(target=read, daemon=True).start()
        try:
            while (remaining := self.deadline - time.monotonic()) > 0:
                try:
                    line = pending.get(timeout=min(remaining, 60))
                except queue.Empty:
                    continue
                if line is end:
                    return
                if isinstance(line, Exception):
                    raise line
                yield line
        finally:
            stopped.set()

    def run(self) -> int:
        for raw in self._lines():
            line = raw.strip()
            if not line:
                continue
            if self.log:
                with open(self.log, "a") as f:
                    f.write(line + "\n")
            try:
                if self.runner(line) is False:
                    break
            except Exception:
                if self.fail_fast:
                    raise
                # A bad round is not a dead agent: report it and wait for the next nudge.
                traceback.print_exc(file=self.stderr)
        return 0
