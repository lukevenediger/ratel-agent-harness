"""Real-browser acceptance tests; install Playwright for Node and Chromium to run."""
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ratel.board import make_server
from ratel.bus import Bus
from ratel.ulid import ulid


def _missing_runtime(reason):
    if os.environ.get('RATEL_REQUIRE_BROWSER') == '1':
        pytest.fail(reason)
    pytest.skip(reason)


def test_board_browser_navigation_and_races(home):
    if not shutil.which('node'):
        _missing_runtime('Node is required for browser tests')
    available = subprocess.run(['node', '-e', "require('playwright')"], capture_output=True,
                               cwd=Path(__file__).parent / 'browser')
    if available.returncode:
        _missing_runtime('Install Node playwright and Chromium; expose it via NODE_PATH')
    bus = Bus(home, 'alpha')
    parent = bus.post('orchestrator', 'Needle @stakeholder', pin=True)
    bus.post('reviewer', 'An old reply', parent=parent['id'])
    for i in range(119):
        bus.post('developer', f'Checkpoint {i}')
    bus.post('developer', '<img src=x onerror=alert(1)>')
    Bus(home, 'beta').post('developer', 'Beta only')
    proposals = Bus(home, 'gamma')
    proposals.post('orchestrator', 'Pinned older proposal', pin=True, attachments=[
        {'type': 'clan', 'status': 'proposed', 'issue': 1, 'roles': [
            {'name': 'developer', 'preset': 'or-glm-flash-low', 'writer': True, 'skills': [], 'why': 'writer'}]}])
    for i in range(101):
        proposals.post('developer', f'Later update {i}')
    server = make_server(home, '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(['node', str(Path(__file__).parent / 'browser' / 'board.cjs'),
                                 f'http://127.0.0.1:{server.server_address[1]}',
                                 json.dumps({'parent': parent['id'], 'stale': ulid(), 'live': ulid(),
                                             'home': str(home), 'python': sys.executable,
                                             'token': server.RequestHandlerClass.token})],
                                capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + '\n' + result.stderr
    finally:
        server.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)



def test_required_browser_runtime_cannot_silently_skip(monkeypatch):
    monkeypatch.setenv('RATEL_REQUIRE_BROWSER', '1')
    with pytest.raises(pytest.fail.Exception, match='runtime missing'):
        _missing_runtime('runtime missing')
