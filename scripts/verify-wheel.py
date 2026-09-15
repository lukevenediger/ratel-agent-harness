"""Build and exercise an installed wheel in a disposable home outside this checkout.

Run with `uv run --frozen python scripts/verify-wheel.py`. No live harness calls.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SMOKE = r'''
import asyncio
import importlib.metadata
import json
import os
import pathlib
import sys
import threading
import urllib.request

import ratel
from ratel.board import board_page, make_server
from ratel.bus import Bus
from ratel.clan.config import load_catalog, load_models
from ratel.clan.harness import PKG, SKILL_MD
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

root = pathlib.Path(os.environ['SMOKE_CHECKOUT'])
assert not pathlib.Path(ratel.__file__).resolve().is_relative_to(root)
distribution = importlib.metadata.distribution('ratel')
assert any(str(f).endswith('/THIRD-PARTY-NOTICES.md') for f in distribution.files)
for entry in distribution.entry_points:
    assert callable(entry.load()), entry.name
home = pathlib.Path(os.environ['RATEL_HOME'])
assert load_catalog(home) and load_models(home)
assert SKILL_MD.is_file() and (PKG / 'briefs' / 'orchestrator.md').is_file()
page = board_page()
assert b'<!-- BOARD_SCRIPTS -->' not in page and b'const MAP_STATES' in page
bus = Bus(home, 'harbor-demo', read_only=True)
assert len(bus.read_all()) == 6 and bus.pins()
server = make_server(home, '127.0.0.1', 0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f'http://127.0.0.1:{server.server_address[1]}'
try:
    for path in ('/', '/static/mermaid.min.js', '/api/channels/harbor-demo/history',
                 '/files/harbor-demo/review.md', '/files/harbor-demo/demo-plan.md'):
        with urllib.request.urlopen(base + path, timeout=5) as response:
            assert response.status == 200 and response.read(), path
finally:
    server.stop.set()
    server.shutdown()
    server.server_close()
    thread.join(5)

async def mcp():
    params = StdioServerParameters(command=sys.executable, args=['-m', 'ratel.mcp_server'],
             env={**os.environ, 'AGENT_NAME': 'smoke', 'CHANNEL': 'harbor-demo'})
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool('post', {'text': 'Installed wheel MCP smoke'})
        assert not result.is_error
asyncio.run(mcp())
assert bus.read_all()[-1]['text'] == 'Installed wheel MCP smoke'
print(json.dumps({'wheel': 'ok', 'assets': 'ok', 'demo': 'ok', 'http': 'ok', 'mcp': 'ok'}))
'''


def main():
    with tempfile.TemporaryDirectory(prefix='ratel-wheel-') as directory:
        scratch = Path(directory)
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
        env.update(RATEL_HOME=str(scratch / 'demo'), CLAUDE_CONFIG_DIR=str(scratch / 'claude'),
                   SMOKE_CHECKOUT=str(ROOT))

        def run(argv, cwd=ROOT):
            subprocess.run([str(a) for a in argv], cwd=cwd, env=env, check=True, timeout=180)

        run(['uv', 'build', '--wheel', '--out-dir', scratch / 'dist'])
        wheels = list((scratch / 'dist').glob('*.whl'))
        assert len(wheels) == 1
        run(['uv', 'export', '--quiet', '--frozen', '--no-dev', '--no-emit-project',
             '--output-file', scratch / 'requirements.txt'], cwd=ROOT)
        run(['uv', 'venv', '--python', sys.executable, scratch / 'venv'])
        python = scratch / 'venv' / 'bin' / 'python'
        run(['uv', 'pip', 'install', '--python', python, '--require-hashes', '-r', scratch / 'requirements.txt'])
        run(['uv', 'pip', 'install', '--python', python, '--no-deps', wheels[0]])
        run([scratch / 'venv' / 'bin' / 'ratel', 'demo', '--home', env['RATEL_HOME']], cwd=scratch)
        run([python, '-I', '-c', SMOKE], cwd=scratch)


if __name__ == '__main__':
    main()
