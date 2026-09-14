"""Offline, opt-in orphan cleanup backed by a verified complete channel archive.

No history, configuration, plans or worktrees are deleted. Callers stop all writers;
SQLite write locking and inventory checks detect ordinary concurrent changes.
"""
import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote

from .bus import Bus
from .clan.config import ClanPaths, read_state
from .paths import confined
from .storage import Store
from .ulid import is_ulid

DB_FILES = {'channel.sqlite3', 'channel.sqlite3-wal', 'channel.sqlite3-shm', 'channel.sqlite3-journal'}


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def inventory(root):
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            relative = (Path(directory) / name).relative_to(root).as_posix()
            path = confined(root, relative)
            info = path.stat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or path.name == '.env':
                raise ValueError('archive refuses special files and .env files')
            if relative not in DB_FILES:
                files[relative] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return files


def inactive(paths):
    state = read_state(paths)
    # Fail closed: even stale tab records require an explicit clan down. No
    # dependency on a successful ps/Zellij probe or an inferred presence timeout.
    if state.get('tabs') or (paths.clan_toml.exists() and state.get('maintenance_ready') is not True):
        raise ValueError('clan may be active; run clan down before archive or retention')
    for directory, _, names in os.walk(paths.channel_dir, followlinks=False):
        if 'round.pid' in names:
            confined(paths.channel_dir, str((Path(directory) / 'round.pid').relative_to(paths.channel_dir)))
            raise ValueError('round ledger remains; stop and inspect the round before maintenance')


def references(bus, files):
    refs = set()

    def collect(value):
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            value = unquote(value)
            # Retain direct refs and references in message/Markdown text. This
            # intentionally prefers false retention over deleting someone's file.
            for relative in files:
                if relative in value or Path(relative).name in value:
                    refs.add(relative)

    with bus.store.connection() as con:
        if Store.pending(con):
            raise ValueError('recover pending approval before maintenance')
        for (raw,) in con.execute('SELECT doc FROM messages'):
            collect(json.loads(raw))  # include malformed messages that readers skip
        if con.execute('PRAGMA user_version').fetchone()[0] >= 3:
            for (raw,) in con.execute('SELECT doc FROM clan_state'):
                collect(json.loads(raw))
    for relative in files:
        if relative.startswith(('plans/', 'clan/')) or relative.endswith(('rounds.jsonl', 'clan.state.json')):
            collect((bus.channel_dir / relative).read_text())
    return refs


def plan(bus, days):
    if not math.isfinite(days) or days < 0:
        raise ValueError('older-than-days must be finite and non-negative')
    paths = ClanPaths(bus.home, bus.channel)
    if bus.is_legacy or not bus.db_path.exists():
        raise ValueError('maintenance requires an existing SQLite channel')
    inactive(paths)
    files = inventory(bus.channel_dir)
    refs = references(bus, files)
    cutoff = time.time() - days * 86400
    candidates = []
    for relative, info in files.items():
        path = Path(relative)
        full_log = (len(path.parts) == 3 and path.parts[0] == 'harness'
                    and any(path.name.endswith('-' + kind + '.log') for kind in ('stdout', 'stderr'))
                    and is_ulid(path.name.split('-')[0]))
        attachment = len(path.parts) == 2 and path.parts[0] == 'files'
        if (attachment or full_log) and relative not in refs and info[3] / 1e9 < cutoff:
            candidates.append(relative)
    return files, sorted(candidates)


def backup(bus, destination, files):
    target = Path(destination).expanduser().absolute()
    # Never write an archive into any channel, including a sibling channel.
    if target.resolve().is_relative_to((bus.home / 'channels').resolve()):
        raise ValueError('archive destination must be outside the channels directory')
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    manifest = {'version': 1, 'channel': bus.channel, 'files': {}}
    try:
        with bus.store.connection() as source, closing(sqlite3.connect(target / 'channel.sqlite3')) as dest:
            source.backup(dest)
            if dest.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('archive database integrity check failed')
        (target / 'channel.sqlite3').chmod(0o600)
        manifest['files']['channel.sqlite3'] = digest(target / 'channel.sqlite3')
        for relative in files:
            source = confined(bus.channel_dir, relative)
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, output)
            output.chmod(0o600)
            copied = digest(output)
            if copied != digest(source):
                raise ValueError('channel file changed during archive; retry with writers stopped')
            manifest['files'][relative] = copied
        if inventory(bus.channel_dir) != files:
            raise ValueError('channel files changed during archive; retry with writers stopped')
        inactive(ClanPaths(bus.home, bus.channel))
        with (target / 'archive-manifest.json').open('x') as f:
            json.dump(manifest, f, indent=2)
        # Flush the backup before it can authorize deletion of an original.
        for directory, _, names in os.walk(target):
            for name in names:
                with (Path(directory) / name).open('rb') as f:
                    os.fsync(f.fileno())
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        # Persist the new directory entry before deleting any source file.
        parent_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        # This directory was exclusively created by this invocation.
        shutil.rmtree(target)
        raise
    return target


def maintain(home, channel, *, days=30, destination=None, apply=False, retain=False):
    bus = Bus(home, channel, read_only=True)
    files, candidates = plan(bus, days)
    report = {'channel': channel, 'dry_run': not apply, 'candidates': candidates if retain else [],
              'files_to_archive': len(files) + 1, 'removed': [], 'archive': None}
    if not apply:
        return report
    if not destination:
        raise ValueError('--apply requires --destination for a new backup directory')
    # Serialize against ratel message, cursor and state writes. Backup uses its
    # own read connection; backing up this write connection would wait on itself.
    with bus.store.connection(write=True):
        current, eligible = plan(bus, days)
        if current != files or eligible != candidates:
            raise ValueError('channel changed after planning; retry with writers stopped')
        archive = backup(bus, destination, files)
        report['archive'] = str(archive)
        for relative in candidates if retain else []:
            source = confined(bus.channel_dir, relative)
            if digest(source) != digest(archive / relative):
                raise ValueError('candidate changed; retained verified archive, refusing further cleanup')
            source.unlink()
            report['removed'].append(relative)
    return report
