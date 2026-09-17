import itertools
import os
import shutil
import signal
import subprocess
import tempfile
import warnings
from pathlib import Path

import pytest

from ratel.bus import Bus
from ratel.clan.config import load_models
from ratel.clan.zellij import Zellij, isolated_env

_counter = itertools.count()


def pytest_configure(config):
    config.addinivalue_line("markers", "zellij: needs the real zellij binary")
    config.addinivalue_line("markers", "llm: spends real model tokens; opt in with -m llm")
    config.addinivalue_line("markers", "node: needs the node binary")


def pytest_collection_modifyitems(config, items):
    for binary in ("zellij", "node"):
        if shutil.which(binary):
            continue
        skip = pytest.mark.skip(reason=f"{binary} binary not installed")
        for item in items:
            if binary in item.keywords:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def claude_config(tmp_path, monkeypatch):
    """No test ever reads or writes the live ~/.claude.json: mark_trusted() and
    any claude binary a test runs land in this tmp config dir instead."""
    d = tmp_path / "claude-config"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def catalog_api_keys(monkeypatch):
    """`clan up` refuses to start a role whose model needs an unset API key, so
    a suite that reads the operator's environment passes or fails by whatever
    they happened to export. Fill in a placeholder for every key the packaged
    models catalog names, without overwriting a real one: Tier 2 (`-m llm`)
    spends real tokens and needs the operator's actual keys."""
    for entry in load_models(None).values():
        for var in entry.get("env", []):
            if not os.environ.get(var):
                monkeypatch.setenv(var, f"test-placeholder-{var.lower()}")


def strip_ambient_agent_name(monkeypatch):
    """A clan role's harness exports AGENT_NAME (`launch()` sets it), and the
    suite runs from inside those roles. The checkpoint self-guard keys off it
    (Decision 38), so an inherited identity fails every test that checkpoints
    that same role. Tests that need an identity set their own."""
    monkeypatch.delenv("AGENT_NAME", raising=False)


@pytest.fixture(autouse=True)
def no_ambient_agent_name(monkeypatch):
    strip_ambient_agent_name(monkeypatch)


@pytest.fixture(autouse=True)
def harness_binaries_on_path(monkeypatch):
    """Answer for the four external binaries the product checks at first use.

    This suite runs on machines (and CI) without `claude`, `opencode`, `npx`
    or `zellij` installed. The presence check has its own tests that force a
    miss; everywhere else the check must pass so the code under test keeps
    going. Real subprocess calls (the zellij integration tests) still resolve
    their binary through PATH, untouched by `shutil.which`."""
    real = shutil.which
    known = {name: f"/fake/bin/{name}" for name in ("zellij", "claude", "opencode", "npx")}
    monkeypatch.setattr(shutil, "which",
                        lambda name, *a, **k: known.get(name, real(name, *a, **k)))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("RATEL_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def no_nested_clans(monkeypatch):
    """Headless probe runs must never create a real nested clan. NOT autouse:
    Tier 2 (`-m llm`) calls `clan new` from the test process itself."""
    monkeypatch.setenv("CLAN_NO_NESTED", "1")


@pytest.fixture
def bus(home):
    return Bus(home, "test")


def live_zellij_sessions() -> list[str] | None:
    """What the user's real server sees — must be untouched by any test.
    None when the listing itself fails: a caller must never treat a failed
    listing as an empty server (every live session would look 'stray')."""
    env = {k: v for k, v in os.environ.items() if k != "ZELLIJ"}
    p = subprocess.run(["zellij", "list-sessions", "--no-formatting", "--short"],
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        return None
    return sorted(p.stdout.split())


class ZellijSandbox:
    """A zellij server of our own: its own TMPDIR (sockets) and config.

    The socket dir must live under a SHORT path: zellij builds
    $TMPDIR/zellij-<uid>/contract_version_1/<session> and a unix socket path is
    capped at 103 bytes, which pytest's own tmp_path blows through on its own.
    HOME stays real so nested clans see real credentials (see
    ratel.clan.zellij.isolated_env).
    """

    def __init__(self):
        self.session = f"clan-test-{os.getpid()}-{next(_counter)}"
        self.tmp = Path(tempfile.mkdtemp(prefix="zj", dir="/tmp"))
        self.env = isolated_env(self.tmp)

    def zellij(self, session=None):
        return Zellij(session or self.session, env=self.env)

    def own_sessions(self) -> list[str]:
        """The session names this sandbox is entitled to delete: its own name
        and anything `clan new` stamped from it (`<name>-<MMDD-HHMM>`).

        The listing is NOT the sandbox's alone. HOME stays real (roles need
        real credentials), so ~/.cache/zellij is shared and `list-sessions`
        includes the operator's exited-but-resurrectable sessions. Deleting
        what this sandbox did not create destroys their resurrect data.
        """
        p = subprocess.run(["zellij", "list-sessions", "--no-formatting", "--short"],
                           env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            return [self.session]
        return sorted(n for n in p.stdout.split()
                      if n == self.session or n.startswith(self.session + "-"))

    def live_sessions(self):
        return live_zellij_sessions()


@pytest.fixture
def zellij_env():
    box = ZellijSandbox()
    try:
        yield box
    finally:
        # `clan new` stamps the session name with a timestamp, so the name the
        # test asked for is not the name that ran — delete every session the
        # sandbox itself could have created, by prefix. NEVER the whole listing:
        # HOME stays real, so ~/.cache/zellij is shared and `list-sessions`
        # reports the operator's own resurrectable sessions alongside ours.
        for name in box.own_sessions():
            subprocess.run(["zellij", "delete-session", "--force", name],
                           env=box.env, capture_output=True, stdin=subprocess.DEVNULL)
        shutil.rmtree(box.tmp, ignore_errors=True)


def no_stray_sessions_impl(armed: set[str]):
    """A test that runs real zellij sessions must leave none of ITS OWN behind.
    Only sessions the test armed via `arm(*names)` are strays: any session that
    appeared and is not in the armed set belongs to someone else on this
    machine (concurrent clans are real) — warn, never touch it."""
    def arm(*names):
        armed.update(names)

    if not shutil.which("zellij"):
        yield arm
        return
    before = live_zellij_sessions()
    yield arm
    after = live_zellij_sessions()
    if before is None or after is None:
        # never read a failed listing as an empty server: every live session
        # would look stray, and the test's hygiene is then unverifiable
        pytest.fail("could not verify the stray-session check: "
                    "`zellij list-sessions` failed")
    new = set(after) - set(before)
    mine = new & armed
    for name in sorted(mine):
        subprocess.run(["zellij", "delete-session", "--force", name],
                       capture_output=True, stdin=subprocess.DEVNULL)
    others = new - armed
    if others:
        warnings.warn(f"unrelated zellij session(s) appeared during the test: "
                      f"{sorted(others)}")
    if mine:
        pytest.fail(f"stray zellij session(s) left by the test: {sorted(mine)}")


@pytest.fixture
def no_stray_sessions():
    """Yield `arm(*names)` — the session names this test is entitled to create."""
    yield from no_stray_sessions_impl(set())


def reaped_subprocesses_impl():
    """Run real subprocesses in their own process group and killpg anything
    still alive when the test ends — a headless agent must not outlive its test.
    Accepted trade (security review of issue #6): the pgid is recorded at spawn
    and killed unconditionally at teardown, even when `communicate()` already
    reaped a leader that exited normally — a group whose leader is gone may
    hold orphans, and in that window a recycled pid is a (vanishingly unlikely)
    bystander kill."""
    pgids: list[int] = []

    def run(argv, **kw):
        timeout = kw.pop("timeout", None)
        if kw.pop("capture_output", False):
            kw["stdout"] = kw["stderr"] = subprocess.PIPE
        p = subprocess.Popen(argv, start_new_session=True, **kw)
        pgids.append(p.pid)                     # start_new_session: pgid == pid
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            p.communicate()
            raise
        return subprocess.CompletedProcess(argv, p.returncode, out, err)

    yield run
    for pgid in pgids:
        try:                                    # the group, not just the child: an
            os.killpg(pgid, signal.SIGKILL)     # exited sh can leave orphans behind
        except (ProcessLookupError, PermissionError):
            pass


@pytest.fixture
def reaped_subprocesses():
    yield from reaped_subprocesses_impl()


# -- ratel-tui (Textual) ----------------------------------------------------

def run_app(app, script, size=(80, 24)):
    """Drive a Textual app headlessly: `script(pilot)` is a coroutine run inside
    `App.run_test`, from a plain sync test via asyncio.run (the repo's pattern
    for async code, see test_mcp_server.py — no pytest plugin)."""
    import asyncio

    async def go():
        async with app.run_test(size=size) as pilot:
            return await script(pilot)
    return asyncio.run(go())


def screen_text(app) -> str:
    """The screen as plain text (what a screenshot would show, minus colour)."""
    import io

    from rich.console import Console

    width, height = app.size
    console = Console(width=width, height=height, file=io.StringIO(), force_terminal=True,
                      color_system="truecolor", record=True, legacy_windows=False, safe_box=False)
    console.print(app.screen._compositor.render_update(full=True, screen_stack=app._background_screens))
    return console.export_text()


@pytest.fixture
def demo_home(tmp_path, monkeypatch):
    """A seeded `harbor-demo` home (ratel.demo.seed) that is also RATEL_HOME for the test."""
    from ratel.demo import seed

    home = Path(seed(tmp_path / "demo")["home"])
    monkeypatch.setenv("RATEL_HOME", str(home))
    return home
