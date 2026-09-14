"""Per-role context measurement: claude transcripts, opencode sqlite, fake files."""
import json
import sqlite3
import time

from ratel.clan.context import context_tokens


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def assistant(usage, ts=None):
    rec = {"type": "assistant", "message": {"usage": usage}}
    if ts is not None:
        rec["timestamp"] = ts
    return rec


def claude_dir(tmp_path, name="proj"):
    """(session cwd, the transcript dir claude derives from it)."""
    cwd = str(tmp_path / name)
    return cwd, tmp_path / ".claude" / "projects" / cwd.replace("/", "-")


def test_claude_reads_the_newest_matching_transcript(tmp_path):
    cwd, projects = claude_dir(tmp_path)
    old = projects / "old.jsonl"
    write_jsonl(old, [{"type": "user", "cwd": cwd}, assistant({"input_tokens": 1}, ts="2026-09-08T06:00:00Z")])
    new = projects / "new.jsonl"
    write_jsonl(new, [{"type": "user", "cwd": cwd},
                      assistant({"input_tokens": 100, "cache_creation_input_tokens": 200,
                                 "cache_read_input_tokens": 300}, ts="2026-09-08T10:01:00Z")])
    old.touch()
    time.sleep(0.02)
    new.touch()
    assert context_tokens({"harness": "claude", "cwd": cwd,
                           "launched": "2026-09-08T07:00:00Z"},
                          home=tmp_path) == 600   # the pre-launch old file is filtered


def test_claude_finds_the_post_clear_session_after_a_repin(tmp_path):
    """A clear checkpoint sets `launched` to the checkpoint ts and drops the
    pinned session id: the pre-clear file (records before the new launched)
    drops out, and the post-clear session is the one candidate."""
    cwd, projects = claude_dir(tmp_path)
    pre = projects / "36824f30-1b2d-4f3e-9a4b-5c6d7e8f9a0b.jsonl"
    write_jsonl(pre, [{"type": "user", "cwd": cwd},
                      assistant({"input_tokens": 72583}, ts="2026-09-08T15:20:00Z")])
    post = projects / "ad012f5c-1b2d-4f3e-9a4b-5c6d7e8f9a0c.jsonl"
    write_jsonl(post, [{"type": "user", "cwd": cwd},
                       assistant({"input_tokens": 12}, ts="2026-09-08T15:25:00Z")])
    assert context_tokens({"harness": "claude", "cwd": cwd,
                           "launched": "2026-09-08T15:22:00Z"},
                          home=tmp_path) == 12


def test_claude_accepts_a_leading_custom_title_record(tmp_path):
    """2.1.263 starts a session file with `custom-title`, `cwd: null` — the
    identity match runs on the first record that carries a cwd, not line 1."""
    cwd, projects = claude_dir(tmp_path)
    write_jsonl(projects / "s.jsonl", [
        {"type": "custom-title", "title": "x", "cwd": None},
        {"type": "user", "cwd": cwd},
        assistant({"input_tokens": 7, "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 0}, ts="2026-09-08T08:01:00Z"),
    ])
    assert context_tokens({"harness": "claude", "cwd": cwd,
                           "launched": "2026-09-08T07:00:00Z"},
                          home=tmp_path) == 7


def test_claude_uses_the_last_assistant_record(tmp_path):
    cwd, projects = claude_dir(tmp_path)
    write_jsonl(projects / "s.jsonl", [
        {"type": "user", "cwd": cwd},
        assistant({"input_tokens": 1}, ts="2026-09-08T08:00:00Z"),
        assistant({"input_tokens": 10, "cache_creation_input_tokens": 2,
                   "cache_read_input_tokens": 3}, ts="2026-09-08T08:01:00Z"),
    ])
    assert context_tokens({"harness": "claude", "cwd": cwd,
                           "launched": "2026-09-08T07:00:00Z"},
                          home=tmp_path) == 15


def test_claude_rejects_a_transcript_from_another_cwd(tmp_path):
    mine, theirs = str(tmp_path / "a"), str(tmp_path / "b")
    projects = tmp_path / ".claude" / "projects" / mine.replace("/", "-")
    write_jsonl(projects / "s.jsonl", [{"type": "user", "cwd": theirs},
                                       assistant({"input_tokens": 5})])
    assert context_tokens({"harness": "claude", "cwd": mine,
                           "launched": "2026-09-08T07:00:00Z"}, home=tmp_path) is None


def test_claude_rejects_a_transcript_older_than_launch(tmp_path):
    cwd, projects = claude_dir(tmp_path)
    write_jsonl(projects / "s.jsonl", [{"type": "user", "cwd": cwd},
                                       assistant({"input_tokens": 5}, ts="2026-09-08T10:40:00Z")])
    launched = time.time() + 3600          # transcript predates the tab
    tab = {"harness": "claude", "cwd": cwd, "launched": launched}
    assert context_tokens(tab, home=tmp_path) is None
    after = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    write_jsonl(projects / "s.jsonl", [{"type": "user", "cwd": cwd},
                                       assistant({"input_tokens": 5}, ts=after)])
    assert context_tokens({**tab, "launched": time.time() - 3600}, home=tmp_path) == 5


def test_claude_reads_the_exact_session_id_file(tmp_path):
    """A tab that knows its session_id must never fall back to a sibling."""
    cwd, projects = claude_dir(tmp_path)
    write_jsonl(projects / "mine.jsonl", [
        {"type": "user", "cwd": cwd},
        assistant({"input_tokens": 42}, ts="2026-09-08T12:00:00Z")])
    # a newer sibling from another session in the same cwd would win on mtime
    write_jsonl(projects / "sibling.jsonl", [
        {"type": "user", "cwd": cwd},
        assistant({"input_tokens": 999}, ts="2026-09-08T13:00:00Z")])
    sid = "0f8f3e0c-1b2d-4f3e-9a4b-5c6d7e8f9a0b"
    write_jsonl(projects / f"{sid}.jsonl", [
        {"type": "user", "cwd": cwd},
        assistant({"input_tokens": 42}, ts="2026-09-08T12:00:00Z")])
    tab = {"harness": "claude", "cwd": cwd, "session_id": sid}
    assert context_tokens(tab, home=tmp_path) == 42
    other = "0f8f3e0c-1b2d-4f3e-9a4b-5c6d7e8f9a0c"
    assert context_tokens({**tab, "session_id": other}, home=tmp_path) is None


def test_claude_ignores_a_stale_file_that_was_only_touched(tmp_path):
    """mtime is not identity: claude touches old files. The gate is the last
    assistant record's timestamp."""
    cwd, projects = claude_dir(tmp_path)
    write_jsonl(projects / "stale.jsonl", [
        {"type": "user", "cwd": cwd},
        assistant({"input_tokens": 320501}, ts="2026-09-08T10:40:00Z")])
    projects.joinpath("stale.jsonl").touch()      # touched now, mtime lies
    tab = {"harness": "claude", "cwd": cwd,
           "launched": "2026-09-08T12:00:00Z"}
    assert context_tokens(tab, home=tmp_path) is None
    write_jsonl(projects / "live.jsonl", [
        {"type": "user", "cwd": cwd},
        assistant({"input_tokens": 11}, ts="2026-09-08T12:05:00Z")])
    assert context_tokens(tab, home=tmp_path) == 11


def build_opencode_db(path, sessions):
    """sessions: list of (session_id, directory, time_created, [(data_dict, time_created)])."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, time_created TEXT)")
    con.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
                "time_created TEXT, data TEXT)")
    for sid, directory, tc, msgs in sessions:
        con.execute("INSERT INTO session VALUES (?, ?, ?)", (sid, directory, tc))
        for i, (data, tc) in enumerate(msgs):
            con.execute("INSERT INTO message VALUES (?, ?, ?, ?)",
                        (f"{sid}-{i}", sid, tc, json.dumps(data)))
    con.commit()
    con.close()


def test_opencode_reads_the_newest_session_and_skips_aborted_rows(tmp_path):
    cwd = str(tmp_path / "proj")
    db = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    good = {"role": "assistant", "tokens": {"input": 10, "cache": {"read": 20, "write": 30}},
            "time": {"created": "t", "completed": "t"}}
    aborted = {"role": "assistant", "tokens": {"input": 0, "cache": {"read": 0, "write": 0}},
               "error": {"name": "abort"}, "time": {"created": "t", "completed": "t"}}
    build_opencode_db(db, [
        ("s_old", cwd, "t0", [({"role": "assistant", "tokens": {"input": 1, "cache": {"read": 0, "write": 0}},
              "time": {"created": "t", "completed": "t"}}, "t0")]),
        ("s_new", cwd, "t1", [(good, "t2"), (aborted, "t3")]),
        ("s_other", str(tmp_path / "elsewhere"), "t0", [(good, "t9")]),
    ])
    assert context_tokens({"harness": "opencode", "cwd": cwd}, home=tmp_path) == 60
    assert context_tokens({"harness": "opencode-run", "cwd": cwd}, home=tmp_path) == 60


def test_opencode_skips_a_turn_still_streaming(tmp_path):
    """The newest assistant row of an in-flight turn has zero tokens and no
    time.completed — it must be skipped, not reported as 0."""
    cwd = str(tmp_path / "proj")
    db = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    streaming = {"role": "assistant", "tokens": {"input": 0, "cache": {"read": 0, "write": 0}},
                 "time": {"created": "t"}}
    done = {"role": "assistant", "tokens": {"input": 5, "cache": {"read": 6, "write": 7}},
            "time": {"created": "t", "completed": "t"}}
    build_opencode_db(db, [("s", cwd, "t0", [(streaming, "t3"), (done, "t2")])])
    assert context_tokens({"harness": "opencode", "cwd": cwd}, home=tmp_path) == 18


def test_opencode_ignores_sessions_that_started_before_launch(tmp_path):
    cwd = str(tmp_path / "proj")
    db = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    good = {"role": "assistant", "tokens": {"input": 3, "cache": {"read": 0, "write": 0}},
            "time": {"created": "t", "completed": "t"}}
    build_opencode_db(db, [("s_early", cwd, "1000", [(good, "t0")]),
                           ("s_late", cwd, "1788870000000", [(good, "t1")])])  # 12:20Z, after launch
    tab = {"harness": "opencode", "cwd": cwd, "launched": "2026-09-08T12:00:00Z"}
    assert context_tokens(tab, home=tmp_path) == 3
    assert context_tokens({**tab, "launched": "2099-01-01T00:00:00Z"}, home=tmp_path) is None


def test_fake_reads_harness_context_txt(tmp_path):
    hd = tmp_path / "harness" / "dev"
    hd.mkdir(parents=True)
    (hd / "context.txt").write_text("1234\n")
    assert context_tokens({"harness": "fake", "harness_dir": str(hd)}) == 1234
    (hd / "context.txt").write_text("not a number")
    assert context_tokens({"harness": "fake", "harness_dir": str(hd)}) is None
    assert context_tokens({"harness": "fake"}) is None


def test_unknown_or_broken_things_yield_none(tmp_path):
    assert context_tokens({"harness": "emacs", "cwd": "/x"}) is None
    assert context_tokens({"harness": "claude", "cwd": str(tmp_path / "nope")},
                          home=tmp_path) is None
    assert context_tokens({"harness": "opencode", "cwd": "/x"}, home=tmp_path) is None
    cwd, projects = claude_dir(tmp_path)
    write_jsonl(projects / "s.jsonl", [{"type": "user", "cwd": cwd}, "not a dict {"])
    assert context_tokens({"harness": "claude", "cwd": cwd}, home=tmp_path) is None


def test_claude_refuses_an_ambiguous_cwd_without_a_launch_record(tmp_path):
    """Several session files share one cwd; without session_id or launched the
    answer could be a stale sibling's — None, never a guess."""
    cwd, projects = claude_dir(tmp_path)
    write_jsonl(projects / "a.jsonl", [{"type": "user", "cwd": cwd},
                                       assistant({"input_tokens": 338058})])
    write_jsonl(projects / "b.jsonl", [{"type": "user", "cwd": cwd},
                                       assistant({"input_tokens": 5})])
    assert context_tokens({"harness": "claude", "cwd": cwd}, home=tmp_path) is None


def test_claude_session_id_must_be_a_uuid(tmp_path):
    cwd, projects = claude_dir(tmp_path)
    write_jsonl(projects / "leak.jsonl", [{"type": "user", "cwd": cwd},
                                          assistant({"input_tokens": 4242})])
    planted = tmp_path / ".claude" / "projects" / "-other-project" / "leak.jsonl"
    write_jsonl(planted, [{"type": "user", "cwd": cwd},
                          assistant({"input_tokens": 9999})])
    tab = {"harness": "claude", "cwd": cwd, "session_id": "../-other-project/leak"}
    assert context_tokens(tab, home=tmp_path) is None
    assert context_tokens({**tab, "session_id": "not a uuid"}, home=tmp_path) is None


def test_claude_refuses_ambiguity_even_with_a_launch_record(tmp_path):
    """Two transcripts postdate the launch: which one? None, never a pick."""
    cwd, projects = claude_dir(tmp_path)
    write_jsonl(projects / "a.jsonl", [{"type": "user", "cwd": cwd},
                                       assistant({"input_tokens": 11}, ts="2026-09-08T12:01:00Z")])
    write_jsonl(projects / "b.jsonl", [{"type": "user", "cwd": cwd},
                                       assistant({"input_tokens": 22}, ts="2026-09-08T12:02:00Z")])
    tab = {"harness": "claude", "cwd": cwd, "launched": "2026-09-08T12:00:00Z"}
    assert context_tokens(tab, home=tmp_path) is None
    (projects / "b.jsonl").unlink()
    assert context_tokens(tab, home=tmp_path) == 11


def test_claude_reads_are_memoised_per_file(tmp_path, monkeypatch):
    import ratel.clan.context as cx
    cwd, projects = claude_dir(tmp_path)
    write_jsonl(projects / "s.jsonl", [{"type": "user", "cwd": cwd},
                                       assistant({"input_tokens": 8}, ts="2026-09-08T12:01:00Z")])
    tab = {"harness": "claude", "cwd": cwd, "launched": "2026-09-08T12:00:00Z"}
    scans = {"n": 0}
    real = cx._claude_scan

    def counting(f):
        scans["n"] += 1
        return real(f)

    monkeypatch.setattr(cx, "_claude_scan", counting)
    assert context_tokens(tab, home=tmp_path) == 8
    assert context_tokens(tab, home=tmp_path) == 8
    assert scans["n"] == 1                      # unchanged file: one slurp, one stat
    write_jsonl(projects / "s.jsonl", [{"type": "user", "cwd": cwd},
                                       assistant({"input_tokens": 9}, ts="2026-09-08T12:02:00Z")])
    assert context_tokens(tab, home=tmp_path) == 9
    assert scans["n"] == 2                      # mtime/size changed: re-read
