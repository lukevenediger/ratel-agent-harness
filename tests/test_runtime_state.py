import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from ratel.bus import Bus
from ratel.clan.config import ClanPaths, read_state, update_state
from ratel.clan.state import migrate, revision
from ratel.doctor import diagnose


def test_state_updates_are_atomic_and_rollback(home):
    paths = ClanPaths(home, 'state')
    update_state(paths, lambda s: s.update(count=0))
    def increment(_):
        update_state(paths, lambda s: s.update(count=s['count'] + 1))
    with ThreadPoolExecutor(5) as pool:
        list(pool.map(increment, range(30)))
    assert read_state(paths)['count'] == 30
    before = revision(paths)
    def fail(s):
        s['count'] = 999
        raise RuntimeError('rollback')
    with pytest.raises(RuntimeError):
        update_state(paths, fail)
    assert read_state(paths)['count'] == 30 and revision(paths) == before


def test_legacy_state_requires_explicit_migration_and_retains_original(home):
    paths = ClanPaths(home, 'legacy').ensure()
    original = '{"session":"old", "future_field":{"keep":true}}'
    paths.state_json.write_text(original)
    assert read_state(paths)['session'] == 'old'
    with pytest.raises(ValueError, match='migrate-state'):
        update_state(paths, lambda s: s.update(session='new'))
    migrate(paths)
    update_state(paths, lambda s: s.update(session='new'))
    assert read_state(paths)['session'] == 'new'
    assert read_state(paths)['future_field'] == {'keep': True}
    assert paths.state_json.read_text() == original
    with pytest.raises(ValueError, match='already exists'):
        migrate(paths)


def test_invalid_migration_is_not_published(home):
    paths = ClanPaths(home, 'legacy').ensure()
    paths.state_json.write_text('[]')
    with pytest.raises(ValueError, match='malformed'):
        migrate(paths)
    assert revision(paths) is None


def test_state_version_is_enforced(home):
    paths = ClanPaths(home, 'future')
    update_state(paths, lambda s: s.update(session='test'))
    with Bus(home, 'future').store.connection(write=True) as con:
        con.execute('UPDATE clan_state SET version=999')
    with pytest.raises(ValueError, match='version'):
        read_state(paths)


def test_doctor_is_read_only_and_redacts_exception_text(home, monkeypatch):
    missing = home / 'missing'
    assert diagnose(missing, 'absent')['ok']
    assert not missing.exists()
    paths = ClanPaths(home, 'test').ensure()
    paths.clan_toml.write_text('invalid SECRET-CREDENTIAL-VALUE')
    monkeypatch.setenv('EXAMPLE_TOKEN', 'SECRET-CREDENTIAL-VALUE')
    report = diagnose(home, 'test')
    assert not report['ok']
    assert 'SECRET-CREDENTIAL-VALUE' not in json.dumps(report)
    assert not (paths.channel_dir / 'channel.sqlite3').exists()


def test_doctor_reports_storage_corruption_counts(bus):
    assert diagnose(bus.home, bus.channel)['ok']
    with bus.store.connection(write=True) as con:
        con.execute("INSERT INTO cursors VALUES('agent', 'broken', 'invalid')")
    report = diagnose(bus.home, bus.channel)
    record = next(c for c in report['checks'] if c['check'] == 'records')
    assert record['status'] == 'warning' and record['detail']['malformed_cursors'] == 1


def test_doctor_reports_credential_presence_not_value(home, monkeypatch):
    paths = ClanPaths(home, 'test').ensure()
    paths.clan_toml.write_text('channel="test"\nissue=1\nrepo="a/b"\ncheckout="/tmp"\n'
                              '[roles.developer]\nharness="fake"\nmodel="fake"\neffort=""\nwriter=true\n')
    monkeypatch.setattr('ratel.doctor.load_models', lambda home: {'fake': {'harness': ['fake'], 'env': ['TEST_API_KEY']}})
    monkeypatch.setenv('TEST_API_KEY', 'sensitive-value-never-print')
    report = diagnose(home, 'test')
    credential = next(c for c in report['checks'] if c['check'] == 'credential:TEST_API_KEY')
    assert credential['detail'] == 'present'
    assert 'sensitive-value-never-print' not in json.dumps(report)


def test_state_shape_failure_rolls_back(home):
    paths = ClanPaths(home, 'test')
    update_state(paths, lambda s: s.update(tabs={}))
    with pytest.raises(ValueError, match='tabs'):
        update_state(paths, lambda s: s.update(tabs=[]))
    assert read_state(paths) == {'tabs': {}}


def test_message_contract_versions(bus):
    from ratel.schema import validate_message
    doc = bus.post('agent', 'hello')
    # Bus.post returns the public message, retaining its established shape.
    assert validate_message(doc) == doc
    assert validate_message({**doc, 'version': 1})['version'] == 1
    for version in (2, True, '1'):
        with pytest.raises(ValueError, match='version'):
            validate_message({**doc, 'version': version})


def test_doctor_cli_reports_error_with_json(home, capsys):
    from ratel.cli import main
    paths = ClanPaths(home, 'test').ensure()
    paths.clan_toml.write_text('invalid')
    with pytest.raises(SystemExit) as exc:
        main(['doctor', '--home', str(home), '--channel', 'test'])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)['ok'] is False
