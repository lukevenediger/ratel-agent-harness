import subprocess
import sys

from ratel.unread import render


def test_render_format(bus):
    top = bus.post("orchestrator", "take the auth middleware.\nTTL from env.", attachments=[{"type": "code", "file": "src/a.py", "lang": "py", "body": "x"}])
    rep = bus.post("security", "found it", parent=top["id"], attachments=[{"type": "tasks", "ref": "plans/p.md", "items": [{"done": True, "text": "a"}, {"done": False, "text": "b"}]}])
    out = render("harbor", "worker-a", [top, rep])
    lines = out.splitlines()
    assert lines[0] == "[ratel #harbor] 2 unread for worker-a"
    assert lines[1] == f"- {top['id'][:5]}… orchestrator: take the auth middleware. TTL from env. [code src/a.py]"
    assert lines[2] == f"- {rep['id'][:5]}… security (reply to {top['id'][:5]}…): found it [tasks 1/2]"


def run_cli(env):
    return subprocess.run([sys.executable, "-m", "ratel.unread"], env=env, capture_output=True, text=True)


def test_cli_unconfigured_is_silent(home):
    r = run_cli({"PATH": "/usr/bin:/bin", "RATEL_HOME": str(home)})
    assert r.returncode == 0 and r.stdout == ""


def test_cli_prints_and_advances(home, bus):
    import os
    bus.post("worker-a", "mine")
    m = bus.post("orchestrator", "@worker-a hello")
    env = {**os.environ, "RATEL_HOME": str(home), "AGENT_NAME": "worker-a", "CHANNEL": "test"}
    r = run_cli(env)
    assert "1 unread" in r.stdout and "hello" in r.stdout and "mine" not in r.stdout
    assert bus.get_cursor("worker-a") == m["id"]
    assert run_cli(env).stdout == ""
