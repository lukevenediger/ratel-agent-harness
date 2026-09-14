"""Server-side link unfurls with a small TTL cache. Never raises."""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable

TTL = 300
FALLBACK_TTL = 30  # failed/plain-fallback unfurls retry soon; a rate-limit 403 must not pin a card for 5 min
_CACHE: dict[str, tuple[float, dict]] = {}
# Each segment is interpolated into an api.github.com path, so a segment of "." or
# ".." would let a posted link steer the request. The lookahead rejects those; a dot
# inside a name (my.repo) is still fine.
_SEG = r"(?!\.{1,2}(?:/|$))[A-Za-z0-9_.-]+"
GH_RE = re.compile(rf"^https://github\.com/({_SEG})/({_SEG})/(pull|issues)/(\d+)(?:[/?#]|$)")
GDOC_RE = re.compile(r"^https://docs\.google\.com/(document|spreadsheets|presentation)/d/")

Fetch = Callable[[str, dict], tuple[int, bytes]]


def _http_get(url: str, headers: dict) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "ratel", **headers})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if tok := os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {tok}"
    return h


def _github(url: str, m: re.Match, fetch: Fetch) -> dict:
    owner, repo, kind, num = m.groups()
    api = f"https://api.github.com/repos/{owner}/{repo}"
    if kind == "pull":
        st, body = fetch(f"{api}/pulls/{num}", _gh_headers())
        if st != 200:
            return _plain(url)
        pr = json.loads(body)
        checks = {"success": 0, "failure": 0, "pending": 0}
        sha = (pr.get("head") or {}).get("sha")
        if sha:
            st2, body2 = fetch(f"{api}/commits/{sha}/check-runs", _gh_headers())
            if st2 == 200:
                for run in json.loads(body2).get("check_runs", []):
                    if run.get("status") != "completed":
                        checks["pending"] += 1
                    elif run.get("conclusion") in ("success", "neutral", "skipped"):
                        checks["success"] += 1
                    else:
                        checks["failure"] += 1
        state = "merged" if pr.get("merged") else pr.get("state", "open")
        return {"kind": "github_pr", "url": url, "title": pr.get("title"), "state": state, "number": int(num),
                "repo": f"{owner}/{repo}", "additions": pr.get("additions", 0), "deletions": pr.get("deletions", 0),
                "checks": checks}
    st, body = fetch(f"{api}/issues/{num}", _gh_headers())
    if st != 200:
        return _plain(url)
    issue = json.loads(body)
    return {"kind": "github_issue", "url": url, "title": issue.get("title"), "state": issue.get("state"),
            "number": int(num), "repo": f"{owner}/{repo}"}


def _gdoc(url: str, fetch: Fetch) -> dict:
    st, body = fetch(url, {})
    if st != 200:
        return _plain(url)
    m = re.search(rb"<title>(.*?)</title>", body, re.S | re.I)
    title = html.unescape(m.group(1).decode(errors="ignore")).strip() if m else None
    if title:
        title = re.sub(r"\s+-\s+Google (Docs|Sheets|Slides)$", "", title)
    return {"kind": "gdoc", "url": url, "title": title, "last_edited": None}


def _plain(url: str) -> dict:
    return {"kind": "link", "url": url, "title": None}


def unfurl(url: str, fetch: Fetch = _http_get, now: float | None = None) -> dict:
    t = time.time() if now is None else now
    hit = _CACHE.get(url)
    if hit and hit[0] > t:
        return hit[1]
    try:
        if m := GH_RE.match(url):
            result = _github(url, m, fetch)
        elif GDOC_RE.match(url):
            result = _gdoc(url, fetch)
        else:
            result = _plain(url)
    except Exception:
        result = _plain(url)
    ttl = FALLBACK_TTL if result["kind"] == "link" and (GH_RE.match(url) or GDOC_RE.match(url)) else TTL
    _CACHE[url] = (t + ttl, result)
    return result
