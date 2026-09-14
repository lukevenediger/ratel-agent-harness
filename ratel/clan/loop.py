"""One nudge line in, one turn out.

A nudge is typed into an agent's terminal, so from the process's side it is
just a line on stdin. The fake harness and the headless kinds both take that
shape, which is why this is its own tiny module rather than part of either.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Callable, TextIO


class NudgeLoop:
    def __init__(self, runner: Callable[[str], object], stdin: TextIO | None = None,
                 log: Path | str | None = None, stderr: TextIO | None = None):
        self.runner = runner
        self.stdin = stdin if stdin is not None else sys.stdin
        self.log = Path(log) if log else None
        self.stderr = stderr if stderr is not None else sys.stderr

    def run(self) -> int:
        for raw in self.stdin:
            line = raw.strip()
            if not line:
                continue
            if self.log:
                with open(self.log, "a") as f:
                    f.write(line + "\n")
            try:
                self.runner(line)
            except Exception:
                # A bad round is not a dead agent: report it and wait for the next nudge.
                traceback.print_exc(file=self.stderr)
        return 0
