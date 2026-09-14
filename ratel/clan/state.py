"""Versioned machine-owned state, transactional in the channel database.

Legacy JSON is read-only. Stop clan processes before explicitly importing it.
Unknown fields are retained so additions do not discard older runtime metadata.
"""
from __future__ import annotations

import fcntl
import json
from typing import TypedDict

from ..storage import Store

STATE_VERSION = 1


class ClanState(TypedDict, total=False):
    maintenance_ready: bool
    runs: dict[str, dict]
    session: str
    checkout: str
    tabs: dict[str, dict]
    watch: dict
    writers: dict[str, bool]
    briefs: dict[str, str]
    env: dict[str, str]
    checkpoints: list[dict]


def store_for(paths):
    return Store(paths.channel_dir / 'channel.sqlite3', root=paths.home)


def validate(doc):
    if not isinstance(doc, dict):
        raise ValueError('clan state must be an object')
    for key in ('tabs', 'watch', 'writers', 'briefs', 'env', 'runs'):
        if key in doc and not isinstance(doc[key], dict):
            raise ValueError(f'clan state {key} must be an object')
    for key in ('session', 'checkout'):
        if key in doc and not isinstance(doc[key], str):
            raise ValueError(f'clan state {key} must be a string')
    # Also reject non-finite numbers and unserializable objects before a commit.
    json.dumps(doc, allow_nan=False)
    return doc


def stored(con):
    if con.execute('PRAGMA user_version').fetchone()[0] < 3:
        return None
    row = con.execute('SELECT version,doc FROM clan_state WHERE singleton=1').fetchone()
    if row is None:
        return None
    if row[0] != STATE_VERSION:
        raise ValueError('unsupported clan state version')
    return validate(json.loads(row[1]))


def legacy(paths, strict=False):
    path = paths.state_json
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            return validate(json.loads(f.read() or '{}'))
    except (ValueError, TypeError):
        if strict:
            raise ValueError('legacy clan state is malformed; repair it before migration') from None
        return {}


def read(paths) -> ClanState:
    store = store_for(paths)
    if store.path.exists():
        with store.connection() as con:
            doc = stored(con)
            if doc is not None:
                return doc
    return legacy(paths)


def revision(paths):
    store = store_for(paths)
    if not store.path.exists():
        return None
    with store.connection() as con:
        if con.execute('PRAGMA user_version').fetchone()[0] < 3:
            return None
        row = con.execute('SELECT revision FROM clan_state WHERE singleton=1').fetchone()
        return row[0] if row else None


def save(con, doc):
    validate(doc)
    con.execute('INSERT INTO clan_state(singleton,version,revision,doc) VALUES(1,?,1,?) '
                'ON CONFLICT(singleton) DO UPDATE SET version=excluded.version, '
                'revision=clan_state.revision+1, doc=excluded.doc',
                (STATE_VERSION, json.dumps(doc, allow_nan=False)))


def update(paths, mutate, *, recovery=None, con=None):
    if con is None:
        from ..bus import Bus
        paths.ensure()
        store = Bus(paths.home, paths.channel).store
        with store.connection(write=True) as connection:
            return update(paths, mutate, recovery=recovery, con=connection)
    doc = stored(con)
    if doc is None:
        if paths.state_json.exists():
            raise ValueError('legacy clan state is read-only; stop the clan and run ratel migrate-state')
        doc = dict(recovery or {})
    mutate(doc)
    save(con, doc)
    return doc


def migrate(paths):
    from ..bus import Bus
    store = Bus(paths.home, paths.channel).store
    with store.connection(write=True) as con:
        if stored(con) is not None:
            raise ValueError('clan state already exists in SQLite')
        if not paths.state_json.is_file():
            raise ValueError('no legacy clan state to migrate')
        doc = legacy(paths, strict=True)
        save(con, doc)
    return {'version': STATE_VERSION, 'original_retained': str(paths.state_json)}
