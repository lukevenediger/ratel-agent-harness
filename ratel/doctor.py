"""Read-only local diagnostics. Credential values and exception text never leave here."""
import os
import re
import shutil
import subprocess
from pathlib import Path

from .bus import Bus
from .clan.config import ClanConfig, ClanPaths, load_catalog, load_models, read_state
from .clan.state import revision
from .storage import Store


def diagnose(home, channel=None):
    home = Path(home).expanduser().resolve()
    checks = []

    def check(name, status, detail):
        checks.append({'check': name, 'status': status, 'detail': detail})

    check('home', 'ok' if home.is_dir() else 'warning', 'present' if home.is_dir() else 'not created')
    if channel:
        try:
            bus = Bus(home, channel, read_only=True)
            if bus.is_legacy:
                check('storage', 'warning', 'legacy JSONL: stop writers and run ratel migrate')
            elif not bus.db_path.exists():
                check('storage', 'warning', 'channel does not exist')
            else:
                with bus.store.connection() as con:
                    ok = con.execute('PRAGMA quick_check').fetchall() == [('ok',)]
                    pending = Store.pending(con) is not None
                check('storage', 'ok' if ok else 'error',
                      'SQLite integrity verified' if ok else 'integrity check failed')
                check('approval', 'warning' if pending else 'ok',
                      'rerun clan approve to recover' if pending else 'no pending application')
                counts = bus.diagnostics()
                check('records', 'warning' if any(counts.values()) else 'ok', counts)
            paths = ClanPaths(home, channel)
            if paths.clan_toml.exists():
                cfg = ClanConfig.read(paths, load_catalog(home)).validate(load_models(home))
                check('configuration', 'ok', 'clan and catalogs validated')
                state = read_state(paths)
                migrated = revision(paths) is not None
                check('state', 'ok' if migrated else 'warning',
                      'SQLite state' if migrated else 'legacy or absent state; use migrate-state for existing JSON')
                binaries = {'zellij', 'git'}
                names = set()
                models = load_models(home)
                for role in cfg.roles.values():
                    binaries.add({'claude-p': 'claude', 'opencode-run': 'opencode',
                                  'fake': 'ratel-fake-harness'}.get(role.harness, role.harness))
                    names.update(models.get(role.model, {}).get('env', []))
                for name in sorted(binaries):
                    check('binary:' + name, 'ok' if shutil.which(name) else 'warning',
                          'available' if shutil.which(name) else 'not found on PATH')
                for name in sorted(names):
                    if isinstance(name, str) and re.fullmatch(r'[A-Z_][A-Z0-9_]*', name):
                        present = bool(os.environ.get(name))
                        check('credential:' + name, 'ok' if present else 'warning', 'present' if present else 'not set')
                for role, tab in state.get('tabs', {}).items():
                    if not isinstance(tab, dict) or not tab.get('worktree'):
                        continue
                    path = Path(tab['worktree'])
                    result = subprocess.run(['git', '-C', str(path), 'status', '--porcelain'],
                                            capture_output=True, timeout=5)
                    status = 'error' if result.returncode else 'warning' if result.stdout else 'ok'
                    check('worktree:' + role, status, 'unavailable' if result.returncode else
                          'uncommitted changes' if result.stdout else 'clean')
            else:
                check('configuration', 'ok', 'channel has no clan configuration')
        except Exception:
            # Config, Git errors and paths can contain arbitrary operator text.
            check('inspection', 'error', 'unable to inspect channel; check paths, state version and configuration')
    else:
        check('channel', 'warning', 'pass --channel for storage, clan and credential checks')
    return {'ok': not any(c['status'] == 'error' for c in checks), 'checks': checks}
