"""The `ratel` console script, driven the way an agent in a shell would drive it."""
import json
import subprocess
import sys
import time

import pytest


def run(home, agent, *args, channel="test", stdin=None, check=True):
    env = {"RATEL_HOME": str(home), "CHANNEL": channel, "PATH": "/usr/bin:/bin"}
    if agent:
        env["AGENT_NAME"] = agent
    p = subprocess.run([sys.executable, "-m", "ratel.cli", *args],
                       capture_output=True, text=True, input=stdin, env=env, timeout=30)
    if check:
        assert p.returncode == 0, f"exit {p.returncode}: {p.stderr}"
    return p


def out(home, agent, *args, **kw):
    return json.loads(run(home, agent, *args, **kw).stdout)


def test_post_prints_id_and_other_agent_reads_it(home):
    mid = out(home, "boss", "post", "@worker-a take auth")["id"]
    assert len(mid) == 26
    got = out(home, "worker-a", "read")
    assert [m["id"] for m in got] == [mid]
    assert got[0]["mentions"] == ["worker-a"]


def test_read_excludes_own_and_advances_cursor(home):
    out(home, "boss", "post", "mine")
    assert out(home, "boss", "read") == []
    assert len(out(home, "worker-a", "read")) == 1
    assert out(home, "worker-a", "read") == []          # cursor advanced


def test_read_since_and_limit(home):
    ids = [out(home, "boss", "post", f"m{i}")["id"] for i in range(3)]
    assert [m["id"] for m in out(home, "worker-a", "read", "--limit", "2")] == ids[:2]
    assert [m["id"] for m in out(home, "worker-a", "read", "--since", ids[0])] == ids[1:]


def test_flags_override_env(home):
    mid = out(home, "boss", "post", "--agent", "designer", "--channel", "redesign", "hi")["id"]
    got = out(home, "boss", "read", "--channel", "redesign")
    assert [m["from"] for m in got] == ["designer"]
    assert got[0]["id"] == mid
    assert out(home, "boss", "read") == []              # other channel untouched


def test_post_dash_reads_stdin(home):
    body = "line one\nline two"
    out(home, "boss", "post", "-", stdin=body)
    assert [m["text"] for m in out(home, "worker-a", "read")] == [body]


def test_post_with_attach_and_attachment_json(home, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    link = json.dumps({"type": "link", "href": "https://example.com", "title": "spec"})
    out(home, "boss", "post", "see these", "--attach", str(f), "--attachment-json", link)
    atts = out(home, "worker-a", "read")[0]["attachments"]
    assert [a["type"] for a in atts] == ["file", "link"]
    assert atts[0]["name"] == "notes.txt"
    assert (home / "channels" / "test" / atts[0]["ref"]).read_text() == "hello"


def test_post_parent_and_thread(home):
    top = out(home, "boss", "post", "plan v1")["id"]
    out(home, "worker-a", "post", "ack", "--parent", top)
    t = out(home, "boss", "thread", top)
    assert t["parent"]["text"] == "plan v1"
    assert [r["text"] for r in t["replies"]] == ["ack"]


def test_thread_unknown_id_exits_nonzero(home):
    p = run(home, "boss", "thread", "nope", check=False)
    assert p.returncode != 0 and "nope" in p.stderr


def test_pin_unpin_and_pins(home):
    top = out(home, "boss", "post", "plan v1", "--pin")["id"]
    other = out(home, "boss", "post", "second")["id"]
    out(home, "boss", "pin", other)
    assert [p["id"] for p in out(home, "boss", "pins")] == [top, other]
    out(home, "boss", "unpin", top)
    assert [p["id"] for p in out(home, "boss", "pins")] == [other]


def test_catch_up_returns_pins_and_messages(home):
    top = out(home, "boss", "post", "plan v1", "--pin")["id"]
    cu = out(home, "worker-a", "catch-up")
    assert [p["id"] for p in cu["pins"]] == [top]
    assert [m["text"] for m in cu["messages"]] == ["plan v1"]


def test_attach_prints_attachment_object(home, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hi")
    att = out(home, "boss", "attach", str(f), "--name", "renamed.txt")
    assert att["type"] == "file" and att["name"] == "renamed.txt"
    assert (home / "channels" / "test" / att["ref"]).exists()


def test_wait_timeout_prints_empty_list_and_exits_zero(home):
    p = run(home, "boss", "wait", "--timeout", "0.3")
    assert json.loads(p.stdout) == []


def test_wait_returns_mention(home):
    proc = subprocess.Popen(
        [sys.executable, "-m", "ratel.cli", "wait", "--timeout", "10"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={"RATEL_HOME": str(home), "CHANNEL": "test", "AGENT_NAME": "worker-a", "PATH": "/usr/bin:/bin"})
    time.sleep(0.5)
    out(home, "boss", "post", "@worker-a go")
    stdout, stderr = proc.communicate(timeout=20)
    assert proc.returncode == 0, stderr
    assert [m["text"] for m in json.loads(stdout)] == ["@worker-a go"]


def test_wait_any_returns_on_any_message(home):
    out(home, "boss", "post", "no mention here")
    got = out(home, "worker-a", "wait", "--timeout", "5", "--any")
    assert [m["text"] for m in got] == ["no mention here"]


def test_pretty_indents(home):
    out(home, "boss", "post", "hi")
    assert "\n" in run(home, "worker-a", "read", "--pretty").stdout.strip()


def test_missing_agent_name_exits_nonzero(home):
    p = run(home, None, "post", "hi", check=False)
    assert p.returncode != 0 and "AGENT_NAME" in p.stderr


@pytest.mark.parametrize("args", [("--help",), ("post", "--help")])
def test_help_exits_zero(home, args):
    assert "ratel" in run(home, "boss", *args).stdout
