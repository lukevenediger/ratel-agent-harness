import re

from ratel.ulid import ulid

CROCKFORD = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def test_shape():
    u = ulid()
    assert CROCKFORD.match(u), u


def test_sorts_by_time():
    a = ulid(now_ms=1_000)
    b = ulid(now_ms=2_000)
    assert a < b


def test_monotonic_same_ms():
    ids = [ulid(now_ms=5_000) for _ in range(100)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 100
