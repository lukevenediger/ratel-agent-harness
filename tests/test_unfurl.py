import json

from ratel import unfurl as U


def fake(responses):
    calls = []
    def fetch(url, headers):
        calls.append((url, headers))
        status, body = responses[url]
        return status, json.dumps(body).encode() if not isinstance(body, bytes) else body
    fetch.calls = calls
    return fetch


def setup_function():
    U._CACHE.clear()


def test_github_pr():
    f = fake({
        "https://api.github.com/repos/o/r/pulls/142": (200, {"title": "Auth TTL", "state": "closed", "merged": True, "additions": 10, "deletions": 3, "head": {"sha": "abc"}}),
        "https://api.github.com/repos/o/r/commits/abc/check-runs": (200, {"check_runs": [{"status": "completed", "conclusion": "success"}, {"status": "in_progress", "conclusion": None}, {"status": "completed", "conclusion": "failure"}]}),
    })
    r = U.unfurl("https://github.com/o/r/pull/142", fetch=f)
    assert r == {"kind": "github_pr", "url": "https://github.com/o/r/pull/142", "title": "Auth TTL", "state": "merged", "number": 142, "repo": "o/r", "additions": 10, "deletions": 3, "checks": {"success": 1, "failure": 1, "pending": 1}}
    assert f.calls[0][1]["Accept"] == "application/vnd.github+json"


def test_github_issue_and_cache():
    f = fake({"https://api.github.com/repos/o/r/issues/7": (200, {"title": "Bug", "state": "open"})})
    r1 = U.unfurl("https://github.com/o/r/issues/7", fetch=f, now=1000)
    r2 = U.unfurl("https://github.com/o/r/issues/7", fetch=f, now=1200)
    assert r1["kind"] == "github_issue" and r1 == r2 and len(f.calls) == 1
    U.unfurl("https://github.com/o/r/issues/7", fetch=f, now=1400)
    assert len(f.calls) == 2  # expired after 300s


def test_gdoc_title():
    html = b"<html><head><title>Token TTL design - Google Docs</title></head></html>"
    f = fake({"https://docs.google.com/document/d/XYZ/edit": (200, html)})
    assert U.unfurl("https://docs.google.com/document/d/XYZ/edit", fetch=f) == {"kind": "gdoc", "url": "https://docs.google.com/document/d/XYZ/edit", "title": "Token TTL design", "last_edited": None}


def test_plain_link_and_errors_never_raise():
    def boom(url, headers): raise OSError("net down")
    assert U.unfurl("https://example.com/x", fetch=boom) == {"kind": "link", "url": "https://example.com/x", "title": None}
    assert U.unfurl("https://github.com/o/r/pull/1", fetch=boom)["kind"] == "link"   # exception path really exercised
    f = fake({"https://api.github.com/repos/o/r/pulls/1": (404, b"{}")})
    assert U.unfurl("https://github.com/o/r/pull/1", fetch=f)["kind"] == "link"


def test_fallback_cached_briefly_only():
    def boom(url, headers): raise OSError("net down")
    U.unfurl("https://github.com/o/r/pull/9", fetch=boom, now=1000)
    ok = fake({"https://api.github.com/repos/o/r/issues/9": (200, {"title": "T", "state": "open"})})
    assert U.unfurl("https://github.com/o/r/issues/9", fetch=ok, now=1010)["kind"] == "github_issue"  # different url, fresh
    f = fake({"https://api.github.com/repos/o/r/pulls/9": (200, {"title": "P", "state": "open", "merged": False, "additions": 1, "deletions": 1, "head": {"sha": "s"}}), "https://api.github.com/repos/o/r/commits/s/check-runs": (200, {"check_runs": []})})
    assert U.unfurl("https://github.com/o/r/pull/9", fetch=f, now=1020)["kind"] == "link"        # still inside 30 s fallback TTL
    assert U.unfurl("https://github.com/o/r/pull/9", fetch=f, now=1040)["kind"] == "github_pr"   # fallback expired, refetched


def test_github_regex_rejects_odd_repo_names():
    calls = []
    def spy(url, headers): calls.append(url); return 404, b"{}"
    assert U.unfurl("https://github.com/o/r?x=/pull/1", fetch=spy)["kind"] == "link"
    assert U.unfurl("https://github.com/o/../pull/1", fetch=spy)["kind"] == "link"
    assert calls == []
