import json

import pytest

from ratel.bus import Bus
from ratel.ulid import ulid


@pytest.mark.parametrize('legacy', [False, True])
def test_history_pages_search_and_pins_use_append_order(home, legacy):
    bus = Bus(home, 'test')
    first = bus.post('agent', 'Needle @stakeholder', pin=True)
    reply = bus.post('other', 'reply needle', parent=first['id'])
    last = bus.post('stakeholder', 'newest')
    if legacy:
        messages = bus.read_all()
        bus.db_path.unlink()
        bus.bus_path.write_text(''.join(json.dumps(m) + '\n' for m in messages))
        bus = Bus(home, 'test', read_only=True)
    page = bus.history(limit=2)
    assert page['messages'] == [reply, last]
    assert page['tip'] == last['id'] and page['next_before'] == reply['id']
    bus_page = bus.history(before=reply['id'], limit=2)
    assert bus_page['messages'] == [first] and bus_page['next_before'] is None
    assert bus.history(query='NEEDLE')['messages'] == [first, reply]
    assert bus.history(operator=True)['messages'] == [first, last]
    assert bus.history(mention='stakeholder')['messages'] == [first]
    assert bus.history(query='not found')['tip'] == last['id']
    assert bus.pins() == [first]
    assert bus.read_thread(first['id'])['replies'] == [reply]


def test_history_reversed_ids_and_appends_between_pages(bus, monkeypatch):
    ids = [ulid() for _ in range(5)][::-1]
    monkeypatch.setattr('ratel.bus.ulid', lambda: ids.pop(0))
    msgs = [bus.post('agent', str(i)) for i in range(4)]
    page = bus.history(limit=2)
    newest = bus.post('agent', 'arrived later')
    earlier = bus.history(before=page['next_before'], limit=2)
    assert earlier['messages'] + page['messages'] == msgs
    assert bus.read_since(page['tip']) == [newest]


@pytest.mark.parametrize('args', [{'limit': 0}, {'limit': 201}, {'before': 'bad'},
                                  {'before': '0' * 26}, {'query': 'x' * 201}])
def test_history_rejects_invalid_or_missing_cursors(bus, args):
    with pytest.raises(ValueError):
        bus.history(**args)


def test_history_skips_nested_corruption_and_keeps_page_full(bus):
    good = bus.post('agent', 'valid')
    bad = {**good, 'id': ulid(), 'attachments': ['invalid']}
    with bus.store.connection(write=True) as con:
        con.execute('INSERT INTO messages(id,parent,doc) VALUES(?,NULL,?)', (bad['id'], json.dumps(bad)))
    assert bus.history(limit=1)['messages'] == [good]
    assert bus.history(limit=1)['tip'] == good['id']
    assert bus.clan_heads() == {'proposed': None, 'approved': None}


def test_heads_are_independent_of_loaded_page(bus):
    proposed = bus.post('orchestrator', 'proposal', attachments=[{'type': 'clan', 'roles': [], 'status': 'proposed'}])
    bus.post('agent', 'recent')
    assert bus.clan_heads() == {'proposed': proposed['id'], 'approved': None}
