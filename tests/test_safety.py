"""Storage boundary and corruption regressions; every path is temporary."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ratel.bus import Bus, now_iso
from ratel.clan.config import ClanPaths
from ratel.storage import Store
from ratel.ulid import ulid


@pytest.mark.parametrize('part', ['channels', 'channels/test', 'channels/test/files',
                                  'channels/test/plans', 'channels/test/cursors'])
def test_storage_directory_symlinks_are_refused(tmp_path, part):
    home = tmp_path / 'home'
    outside = tmp_path / 'outside'
    outside.mkdir()
    alias = home / part
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        Bus(home, 'test')
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('name', ['channel.sqlite3', 'channel.sqlite3-wal',
                                  'channel.sqlite3-shm', 'channel.sqlite3-journal', 'bus.jsonl'])
def test_existing_bus_rechecks_file_aliases(bus, tmp_path, name):
    target = tmp_path / 'untouched'
    target.write_text('keep')
    alias = bus.channel_dir / name
    alias.unlink(missing_ok=True)
    alias.symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        bus.post('agent', 'blocked')
    assert target.read_text() == 'keep'


@pytest.mark.parametrize('name', ['..', '.', '../outside', '/absolute', 'bad/name', 'bad\nname'])
def test_clan_paths_reject_names(home, name):
    with pytest.raises(ValueError):
        ClanPaths(home, name)
    with pytest.raises(ValueError):
        ClanPaths(home, 'test').harness_dir(name)


@pytest.mark.parametrize('attachment', [
    {'type': 'tasks', 'items': ['not an object']},
    {'type': 'tasks', 'items': [{'text': 'task', 'done': 'yes'}]},
    {'type': 'clan', 'roles': [{'name': '../outside'}]},
    {'type': 'clan', 'roles': [{'name': 'dev', 'writer': 1}]},
    {'type': 'code', 'body': []}, {'type': 'file', 'ref': 4},
    {'type': 'link', 'url': []}, {'type': 'unknown'},
])
def test_malformed_attachments_never_land(bus, attachment):
    with pytest.raises(ValueError):
        bus.post('agent', 'bad', attachments=[attachment])
    assert bus.read_all() == []


def test_corrupt_rows_do_not_hide_later_messages_and_cursors_repair(bus):
    first = bus.post('agent', 'first')
    bad = {**first, 'id': ulid(), 'attachments': [{'type': 'tasks', 'items': ['bad']}]}
    with bus.store.connection(write=True) as con:
        seq = con.execute('INSERT INTO messages(id,parent,doc) VALUES(?,NULL,?)',
                          (bad['id'], json.dumps(bad))).lastrowid
        con.execute('INSERT INTO pins VALUES(?,?)', (bad['id'], seq))
        con.execute('INSERT INTO cursors VALUES(?,?,?)', ('reader', 'broken', now_iso()))
        con.execute('INSERT INTO cursors VALUES(?,?,?)', ('../outside', 0, now_iso()))
    last = bus.post('agent', 'last')
    assert bus.read_since(first['id'], limit=1) == [last]
    assert bus.read_thread(bad['id']) is None
    assert bus.pins() == []
    assert bus.diagnostics() == {'malformed_messages': 1, 'malformed_cursors': 2}
    assert bus.consume('reader') == [first, last]
    assert bus.get_cursor('reader') == last['id']
    assert bus.diagnostics()['malformed_cursors'] == 1


def test_version_one_is_upgraded_without_losing_messages(bus):
    msg = bus.post('agent', 'retained')
    with bus.store.connection(write=True) as con:
        con.execute('DROP TABLE approval_applications')
        con.execute('PRAGMA user_version=1')
    assert Bus(bus.home, bus.channel, read_only=True).read_all() == [msg]
    upgraded = Bus(bus.home, bus.channel)
    assert upgraded.read_all() == [msg]
    with upgraded.store.connection() as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == 3
        assert Store.pending(con) is None


def proposal(bus):
    return bus.post('orchestrator', 'proposal', attachments=[
        {'type': 'clan', 'status': 'proposed', 'issue': 1, 'roles': []}])


def test_approval_check_and_insert_share_a_transaction(bus, monkeypatch):
    original = proposal(bus)
    entered, attempted = Event(), Event()
    original_check = Store.check_proposal

    def check(con, expected):
        original_check(con, expected)
        entered.set()
        assert attempted.wait(5)

    monkeypatch.setattr(Store, 'check_proposal', staticmethod(check))
    att = {'type': 'clan', 'status': 'approved', 'issue': 1, 'roles': [], 'supersedes': original['id']}

    def propose_later():
        assert entered.wait(5)
        attempted.set()
        return proposal(bus)

    with ThreadPoolExecutor(2) as pool:
        later = pool.submit(propose_later)
        approved = bus.post('stakeholder', 'approved', attachments=[att], expected_proposal=original['id'])
        newest = later.result(timeout=5)
    assert [m['id'] for m in bus.read_all()] == [original['id'], approved['id'], newest['id']]
    with pytest.raises(ValueError, match='newest'):
        bus.post('stakeholder', 'stale', attachments=[att], expected_proposal=original['id'])


def test_same_approval_retries_are_idempotent_and_conflicting_edits_refused(bus):
    proposed = proposal(bus)
    att = {'type': 'clan', 'status': 'approved', 'issue': 1, 'roles': [], 'supersedes': proposed['id']}
    first = bus.post('stakeholder', 'approved', attachments=[att], expected_proposal=proposed['id'])
    assert bus.post('stakeholder', 'retry', attachments=[att], expected_proposal=proposed['id']) == first
    with pytest.raises(ValueError, match='already approved'):
        bus.post('stakeholder', 'different', attachments=[{**att, 'roles': [{'name': 'dev'}]}],
                 expected_proposal=proposed['id'])
    assert len(bus.read_all()) == 2


def test_diagnostics_cli_is_read_only(home, capsys):
    from ratel.cli import main
    main(['diagnostics', '--home', str(home), '--channel', 'absent'])
    assert json.loads(capsys.readouterr().out) == {'malformed_messages': 0, 'malformed_cursors': 0}
    assert not (home / 'channels').exists()


@pytest.mark.parametrize('value', [-1, 0, True, '2'])
def test_invalid_read_limits_are_rejected(bus, value):
    with pytest.raises(ValueError, match='limit'):
        bus.read_since(None, value)


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), True])
def test_invalid_wait_timeouts_are_rejected(bus, value):
    with pytest.raises(ValueError, match='timeout'):
        bus.wait_for_new('agent', timeout_s=value)
