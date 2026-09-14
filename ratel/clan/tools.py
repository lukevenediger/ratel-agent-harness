"""External-binary presence checks, run at first use rather than at import."""
from __future__ import annotations

import shutil
import sys


def require_binary(binary: str, needed_by: str) -> None:
    """Exit with an actionable message when `binary` is not on PATH.

    A missing `zellij` or `claude` otherwise surfaces as a bare
    `FileNotFoundError` from inside `execvpe`, saying nothing about which tool
    to install. `needed_by` names what wants it so the message reads as an
    instruction. Callers check at the point the binary is first needed, never
    at import time.
    """
    if shutil.which(binary) is None:
        sys.exit(f"ratel: {binary!r} is not on PATH — {needed_by} needs it. "
                 f"Install {binary} and try again.")
