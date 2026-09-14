"""Minimal ULID: 48-bit ms timestamp + 80 random bits, Crockford base32.
Monotonic within a process for ids generated in the same millisecond."""
import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = threading.Lock()
_last_ts = -1
_last_rand = 0


def ulid(now_ms: int | None = None) -> str:
    global _last_ts, _last_rand
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    with _lock:
        if ts == _last_ts:
            _last_rand += 1
        else:
            _last_ts = ts
            _last_rand = int.from_bytes(os.urandom(10), "big")
        rand = _last_rand
    n = (ts << 80) | (rand & ((1 << 80) - 1))
    out = []
    for _ in range(26):
        out.append(_ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(out))


def is_ulid(s: object) -> bool:
    """True for a 26-char Crockford-base32 ULID — the only ids that may sit
    in a message's `parent`, which reaches typed keystrokes downstream."""
    return isinstance(s, str) and len(s) == 26 and all(c in _ALPHABET for c in s)
