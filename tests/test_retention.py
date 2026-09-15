import json
import os
import shutil

import pytest

from ratel.bus import Bus
from ratel.clan.config import ClanPaths, update_state
from ratel.retention import digest, maintain
from ratel.ulid import ulid


def seed(bus):
    files = bus.files_dir
    names = ['linked', 'orphan', 'recent', 'plan-ref']
    for name in names:
        (files / name).write_text(name)
        if name != 'recent':
            os.utime(files / name, (1, 1))
    bus.post('agent', 'keep', attachments=[{'type': 'file', 'ref': 'files/linked'}])
    (bus.channel_dir / 'plans' / 'task.md').write_text('Read files/plan-ref')
    return files


def test_retention_dry_run_does_not_change_files(bus, tmp_path):
    files = seed(bus)
    target = tmp_path / 'archive'
    result = maintain(bus.home, bus.channel, destination=target, retain=True)
    assert result['dry_run'] and result['candidates'] == ['files/orphan']
    assert (files / 'orphan').read_text() == 'orphan' and not target.exists()


def test_backup_restores_messages_cursors_state_and_attachments(bus, tmp_path):
    files = seed(bus)
    bus.post('agent', 'pinned', pin=True)
    update_state(ClanPaths(bus.home, bus.channel), lambda s: s.update(tabs={}, session='old'))
    # Keep a WAL connection open while backing up: copying only the main DB
    # would lose committed content, whereas the backup API retains it.
    with bus.store.connection() as held:
        held.execute('SELECT COUNT(*) FROM messages').fetchone()
        bus.post('agent', 'in WAL', advance_sender=True)
        target = tmp_path / 'archive'
        report = maintain(bus.home, bus.channel, destination=target, apply=True, retain=True)
    assert report['removed'] == ['files/orphan'] and not (files / 'orphan').exists()
    manifest = json.loads((target / 'archive-manifest.json').read_text())
    assert all(digest(target / name) == sha for name, sha in manifest['files'].items())
    assert (target / 'files/orphan').read_text() == 'orphan'
    restore_home = tmp_path / 'restored'
    restore = restore_home / 'channels' / bus.channel
    restore.parent.mkdir(parents=True)
    shutil.copytree(target, restore)
    restored = Bus(restore_home, bus.channel, read_only=True)
    assert restored.read_all() == bus.read_all()
    assert restored.pins() == bus.pins()
    with restored.store.connection() as con, bus.store.connection() as original:
        assert con.execute('SELECT * FROM cursors').fetchall() == original.execute('SELECT * FROM cursors').fetchall()
        assert con.execute('SELECT * FROM clan_state').fetchall() == original.execute('SELECT * FROM clan_state').fetchall()
    assert (restore / 'files/linked').read_text() == 'linked'


@pytest.mark.parametrize('mode', ['tabs', 'clan', 'round'])
def test_active_or_uncertain_clans_are_excluded(bus, tmp_path, mode):
    paths = ClanPaths(bus.home, bus.channel).ensure()
    if mode == 'tabs':
        update_state(paths, lambda s: s.update(tabs={'developer': {'pane_id': 1}}))
    elif mode == 'clan':
        paths.clan_toml.write_text('placeholder')
    else:
        hd = paths.harness_dir('developer')
        hd.mkdir()
        (hd / 'round.pid').write_text('{}')
    target = tmp_path / 'archive'
    with pytest.raises(ValueError, match='clan|round'):
        maintain(bus.home, bus.channel, apply=True, destination=target)
    assert not target.exists()


def test_declared_down_clan_can_be_archived(bus, tmp_path):
    paths = ClanPaths(bus.home, bus.channel).ensure()
    paths.clan_toml.write_text('placeholder')
    update_state(paths, lambda s: s.update(tabs={}, maintenance_ready=True))
    assert maintain(bus.home, bus.channel)['dry_run']


def test_referenced_logs_retained_orphan_logs_eligible(bus):
    hd = ClanPaths(bus.home, bus.channel).ensure().harness_dir('developer')
    hd.mkdir()
    referenced = ulid() + '-stdout.log'
    orphan = ulid() + '-stderr.log'
    for name in [referenced, orphan]:
        (hd / name).write_text('output')
        os.utime(hd / name, (1, 1))
    (hd / 'rounds.jsonl').write_text(json.dumps({'logs': {'stdout': referenced}}) + '\n')
    report = maintain(bus.home, bus.channel, retain=True)
    assert report['candidates'] == ['harness/developer/' + orphan]


def test_existing_destination_is_never_overwritten(bus, tmp_path):
    files = seed(bus)
    target = tmp_path / 'archive'
    target.mkdir()
    (target / 'keep').write_text('keep')
    with pytest.raises(FileExistsError):
        maintain(bus.home, bus.channel, destination=target, apply=True, retain=True)
    assert (files / 'orphan').exists() and (target / 'keep').read_text() == 'keep'


def test_symlinks_and_internal_destination_refused(bus, tmp_path):
    (bus.files_dir / 'alias').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='symlink'):
        maintain(bus.home, bus.channel)
    (bus.files_dir / 'alias').unlink()
    with pytest.raises(ValueError, match='outside'):
        maintain(bus.home, bus.channel, destination=bus.channel_dir / 'archive', apply=True)


def test_changed_file_aborts_backup_before_any_removal(bus, tmp_path, monkeypatch):
    files = seed(bus)
    real_copy = shutil.copyfile
    def racing(source, target):
        result = real_copy(source, target)
        if source.name == 'orphan':
            source.write_text('new content')
        return result
    monkeypatch.setattr('ratel.retention.shutil.copyfile', racing)
    target = tmp_path / 'archive'
    with pytest.raises(ValueError, match='changed'):
        maintain(bus.home, bus.channel, destination=target, apply=True, retain=True)
    assert (files / 'orphan').read_text() == 'new content' and not target.exists()


def test_malformed_messages_still_protect_references(bus):
    path = bus.files_dir / 'keep'
    path.write_text('keep')
    os.utime(path, (1, 1))
    msg = bus.post('agent', 'files/keep')
    with bus.store.connection(write=True) as con:
        con.execute("UPDATE messages SET doc=json_set(doc,'$.ts','broken') WHERE id=?", (msg['id'],))
    assert maintain(bus.home, bus.channel, retain=True)['candidates'] == []


def test_archive_without_retain_keeps_originals(bus, tmp_path):
    files = seed(bus)
    maintain(bus.home, bus.channel, destination=tmp_path / 'archive', apply=True)
    assert (files / 'orphan').exists()


@pytest.mark.parametrize('days', [-1, float('nan'), float('inf')])
def test_invalid_retention_age_is_rejected(bus, days):
    with pytest.raises(ValueError, match='older-than-days'):
        maintain(bus.home, bus.channel, days=days)


def test_encoded_markdown_reference_is_retained(bus):
    path = bus.files_dir / 'two words.txt'
    path.write_text('keep')
    os.utime(path, (1, 1))
    bus.post('agent', '[Read](files/two%20words.txt)')
    assert maintain(bus.home, bus.channel, retain=True)['candidates'] == []


def test_special_files_are_refused_without_opening_them(bus):
    os.mkfifo(bus.files_dir / 'pipe')
    with pytest.raises(ValueError, match='special files'):
        maintain(bus.home, bus.channel)
