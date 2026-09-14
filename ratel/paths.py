"""Validated names and non-symlink paths beneath an operator-selected home.

The home itself may be a symlink; everything inside it must be a real path.
These checks prevent accidental or planted aliases, not a sandbox against a
process with the same OS permissions concurrently replacing directories.
"""
import os
import tempfile
from pathlib import Path


def confined(root: Path, *parts: str) -> Path:
    root = root.expanduser().absolute()
    path = root
    for part in parts:
        relative = Path(part)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("path escapes its storage directory")
        for component in relative.parts:
            path = path / component
            if path.is_symlink():
                raise ValueError("symlinks are not allowed inside channel storage")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("path escapes its storage directory")
    return path


def channel_path(home: Path, channel: str, *parts: str) -> Path:
    from .schema import validate_name
    validate_name(channel, "channel")
    return confined(home, "channels", channel, *parts)


def atomic_write(path: Path, text: str):
    """Replace one regular configuration file without exposing a partial save."""
    confined(path.parent, path.name)
    fd, name = tempfile.mkstemp(prefix=".ratel-write-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        confined(path.parent, path.name)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)
