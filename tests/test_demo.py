import json

import pytest

from ratel.bus import Bus
from ratel.cli import main
from ratel.demo import seed


def test_demo_has_pins_threads_and_existing_attachments(tmp_path):
    report = seed(tmp_path / 'demo')
    bus = Bus(report['home'], report['channel'], read_only=True)
    messages = bus.read_all()
    assert report['messages'] == len(messages) == 6
    assert bus.pins() and any(m['parent'] == report['thread'] for m in messages)
    for message in messages:
        for att in message['attachments']:
            if att['type'] == 'file':
                assert att['ref'].startswith('files/')
            if 'ref' in att:
                assert (bus.channel_dir / att['ref']).is_file()
    assert not (bus.channel_dir / 'clan' / 'clan.toml').exists()


def test_demo_never_overwrites_existing_home(tmp_path):
    marker = tmp_path / 'keep'
    marker.write_text('keep')
    with pytest.raises(FileExistsError):
        seed(tmp_path)
    assert marker.read_text() == 'keep' and not (tmp_path / 'channels').exists()


def test_demo_requires_explicit_home_even_with_environment(home):
    with pytest.raises(SystemExit, match='requires --home'):
        main(['demo'])
    assert not (home / 'channels').exists()


def test_demo_cli_reports_channel(tmp_path, capsys):
    main(['demo', '--home', str(tmp_path / 'demo'), '--channel', 'example'])
    assert json.loads(capsys.readouterr().out)['channel'] == 'example'


def test_demo_validates_before_creating_home(tmp_path):
    with pytest.raises(ValueError):
        seed(tmp_path / 'demo', '../escape')
    assert not (tmp_path / 'demo').exists()
