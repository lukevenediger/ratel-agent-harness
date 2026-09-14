"""Read-only board: JSON API + SSE + static page + channel files. stdlib only.

The one write route is the clan-approval POST (`/api/channels/<ch>/post`),
gated by a bearer token in `<home>/board.token`; everything else stays GET.
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import stat
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .bus import Bus, default_home, now_iso
from .clan.config import CATALOG_ERRORS, ClanConfig, ClanPaths, load_catalog, read_state
from .clan.config import catalog as clan_catalog
from .clan.proposal import validate_clan_attachment
from .clan.session import ClanError, activity
from .ulid import is_ulid
from .unfurl import unfurl

STATIC = Path(__file__).parent / "static"
CHANNEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# `repo` lands in the sidebar from the agent-writable clan.toml. A conservative
# owner/name slug only; anything else is dropped, never escaped-and-shown.
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_repo(value: object) -> str | None:
    return value if isinstance(value, str) and REPO_RE.fullmatch(value) else None


# Vendored assets, served at /static/. The requested name is only ever used as a
# DICT KEY — never a path join, never a resolve-and-check-parents like _file
# does — so traversal is not a question that can be asked: "/static/../x" splits
# into three parts and never reaches here, ".." is not a key, and board.html is
# deliberately absent because the page is served at "/" and must not be
# reachable twice. Provenance for each blob: ratel/static/VENDOR.md.
STATIC_ASSETS = {"mermaid.min.js": "text/javascript; charset=utf-8"}
_GZIP_CACHE: dict[tuple, bytes] = {}


def gzipped(path: Path) -> bytes:
    """The asset compressed once per process, keyed by mtime and size.

    DERIVED, never committed. A checked-in .gz drifts the moment someone
    updates the .js and forgets to regenerate it, and the server then answers
    one URL two ways: stale compressed bytes to every client that accepts
    gzip, fresh bytes to everyone else — and each path looks fine on its own.
    """
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _GZIP_CACHE.get(key)
    if hit is None:
        hit = _GZIP_CACHE[key] = gzip.compress(path.read_bytes(), 9)
    return hit
MAX_POST_BODY = 8192
MAX_POST_TEXT = 1000


def board_token(home: Path) -> str:
    """The board's write token. First start creates `<home>/board.token`
    (0600, O_EXCL so a concurrent server cannot race a second file into
    existence); every start refuses a file whose group/other bits are set —
    it gates writes to every channel this home serves. An empty or
    whitespace-only file is refused on both paths: `compare_digest("", "")`
    is True, so an empty token would open the write route to everyone."""
    p = Path(home) / "board.token"
    p.parent.mkdir(parents=True, exist_ok=True)   # a fresh machine has no home yet
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if stat.S_ISLNK(os.lstat(p).st_mode):
            sys.exit(f"ratel board: {p} is a symlink — remove it and restart, "
                     "the token file is never read through a link")
        return _checked_token(p)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(32))
        f.flush()
        os.fsync(f.fileno())      # a crash here must never leave an empty 0600 token
    return _checked_token(p)


def _checked_token(p: Path, mode: int | None = None) -> str:
    # O_NOFOLLOW: the lstat→read window must not be swappable for a symlink.
    # O_NONBLOCK: a FIFO must not block the open/read before the type check.
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd).st_mode
        if not stat.S_ISREG(st):
            sys.exit(f"ratel board: {p} is not a regular file — remove it and restart")
        if mode is None:
            mode = st & 0o777
        if mode & 0o077:
            sys.exit(f"ratel board: {p} is readable beyond its owner "
                     f"({oct(mode)}) — chmod 600 first, then restart")
        token = os.read(fd, 4096).decode().strip()
    finally:
        os.close(fd)
    if not token:
        sys.exit(f"ratel board: {p} is empty — delete it and restart "
                 "so a fresh token is generated")
    return token


def open_once_line(host: str, port: int, token: str) -> str:
    """The one-time bootstrap URL the board.html Confirm flow reads by hand."""
    return f"open once: http://{host}:{port}/?token={token}"

# files/ is named by agents (attach_file's `name`, and the spilled-code basename in
# Bus._fit), and it is served from the board's own origin. Anything outside this set
# is handed back as an inert download so a message can't plant active content — an
# .html or .svg served inline could read the whole channel through the JSON API.
# SVG is deliberately absent: it executes script.
INLINE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "application/pdf", "text/plain", "text/markdown",
})


class _LockBusy(Exception):
    """Another writer holds `clan.state.json`'s lock past the retry window."""


def _read_state_nb(path: Path, attempts: int = 3, delay_s: float = 0.02) -> dict:
    """A shared, NON-blocking read of an agent-adjacent JSON file.

    `clan watch` and `clan up` take `LOCK_EX` over read-modify-write cycles, so
    a plain read can catch the file mid-truncate. Take `LOCK_SH | LOCK_NB` with a
    short retry; never `LOCK_EX`, never write. `_LockBusy` lets the caller serve
    its last good snapshot instead of pinning a handler thread on the writer.
    """
    if not path.exists():
        return {}
    with open(path, "r") as f:
        got = False
        for i in range(attempts):
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if i + 1 < attempts:
                    time.sleep(delay_s)
        if not got:
            raise _LockBusy()
        try:
            f.seek(0)
            raw = f.read()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    try:
        doc = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _clan_signature(home: Path, ch: str) -> str:
    """A change signature over `clan.state.json` (mtime, size) and each
    `harness/<role>` directory mtime (it moves when `round.pid` is created or
    unlinked). Stats only — no contents, no parser — so the SSE loop stays
    cheap enough to wake every 0.5s."""
    base = home / "channels" / ch
    parts: list[str] = []
    state = base / "clan.state.json"
    try:
        st = state.stat()
        parts.append(f"state:{st.st_mtime_ns}:{st.st_size}")
    except OSError:
        parts.append("state:-")
    harness = base / "harness"
    try:
        dirs = sorted(d for d in harness.iterdir() if d.is_dir())
    except OSError:
        dirs = []
    for d in dirs:
        try:
            parts.append(f"{d.name}:{d.stat().st_mtime_ns}")
        except OSError:
            parts.append(f"{d.name}:-")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def list_channels(home: Path) -> list[str]:
    root = home / "channels"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if ((p / "channel.sqlite3").is_file() or (p / "bus.jsonl").is_file()))


class BoardHandler(BaseHTTPRequestHandler):
    home: Path  # set by make_server
    token: str  # set by make_server
    protocol_version = "HTTP/1.1"
    timeout = 15  # a stalled body read must not pin a handler thread forever

    def log_message(self, fmt, *args):  # quiet
        pass

    # -- helpers --
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode(), "application/json", {"Cache-Control": "no-store"})

    def _error(self, status, msg):
        self._json({"error": msg}, status)

    def _bus(self, ch: str) -> Bus | None:
        if not CHANNEL_RE.match(ch) or ch not in list_channels(self.home):
            return None
        return Bus(self.home, ch, read_only=True)

    # -- routing --
    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        q = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        parts = [p for p in path.split("/") if p]
        try:
            if path == "/":
                return self._send(200, (STATIC / "board.html").read_bytes(), "text/html; charset=utf-8",
                                  {"Cache-Control": "no-store",
                                   # the page holds the write token in localStorage: frame-busters
                                   # and a tight CSP are defence in depth against a future injection
                                   "Content-Security-Policy":
                                       "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                       "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                                       "img-src 'self' data:; frame-ancestors 'none'; "
                                       "base-uri 'none'; form-action 'self'",
                                   "X-Frame-Options": "DENY",
                                   "Referrer-Policy": "no-referrer",
                                   "X-Content-Type-Options": "nosniff"})
            if len(parts) == 2 and parts[0] == "static":
                return self._static(parts[1])
            if parts == ["api", "channels"]:
                return self._json({"channels": self._channel_summaries()})
            if parts == ["api", "clan", "catalog"]:
                try:
                    return self._json(clan_catalog(self.home))
                except CATALOG_ERRORS:
                    # models.toml / presets.toml / roles.toml are operator-
                    # writable; a half-saved one must answer, not kill the
                    # handler thread. Fixed text: the exception names the file
                    # and the value, and this route is unauthenticated.
                    return self._error(500, "catalog unreadable")
            if len(parts) >= 4 and parts[:2] == ["api", "channels"]:
                if not CHANNEL_RE.match(parts[2]):
                    return self._error(400, "bad channel")
                bus = self._bus(parts[2])
                if bus is None:
                    return self._error(404, "no such channel")
                if parts[3] == "messages" and len(parts) == 4:
                    raw_limit = q.get("limit", "")
                    limit = int(raw_limit) if raw_limit.isdigit() and int(raw_limit) > 0 else None
                    return self._json({"messages": bus.read_since(q.get("since"), limit), "pins": bus.pins()})
                if parts[3] == "thread" and len(parts) == 5:
                    t = bus.read_thread(parts[4])
                    return self._json(t) if t else self._error(404, "no such message")
                if parts[3] == "events" and len(parts) == 4:
                    return self._sse(bus, self.headers.get("Last-Event-ID") or q.get("since"))
                if parts[3] == "clan" and len(parts) == 4:
                    return self._json(self._clan(parts[2]))
                if parts[3] == "unfurl" and len(parts) == 4:  # wired in Task 10
                    return self._unfurl(q.get("url", ""))
            if len(parts) == 3 and parts[0] == "files":
                return self._file(parts[1], parts[2])
            return self._error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- write route (token-gated) ---------------------------------------
    def do_POST(self):
        try:
            return self._do_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, sqlite3.Error):
            self._error(409, "channel unavailable for writes; check storage and migration status")

    def _do_post(self):
        parts = [p for p in unquote(urlparse(self.path).path).split("/") if p]
        if not (len(parts) == 4 and parts[:2] == ["api", "channels"] and parts[3] == "post"):
            # this branch never reads the body, so the connection is poisoned
            # for keep-alive: close it explicitly
            return self._send(404, b'{"error": "not found"}', "application/json",
                              {"Connection": "close"})
        auth = self.headers.get("Authorization", "")
        supplied = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        # bytes: the header is latin-1 off the wire and may be non-ASCII, and
        # compare_digest on str raises on that instead of answering 401
        if not hmac.compare_digest(supplied.encode("latin-1", "ignore"),
                                   self.token.encode()):
            return self._send(401, b'{"error": "unauthorized"}', "application/json",
                              {"WWW-Authenticate": "Bearer", "Connection": "close"})
        if self.headers.get("Transfer-Encoding"):
            return self._send(411, b'{"error": "length required"}', "application/json",
                              {"Connection": "close"})
        raw_len = self.headers.get("Content-Length")
        try:
            n = int(raw_len) if raw_len is not None else 0
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_POST_BODY:
            return self._send(413, b'{"error": "payload too large"}', "application/json",
                              {"Connection": "close"})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._send(415, b'{"error": "unsupported media type"}', "application/json",
                              {"Connection": "close"})
        body = self.rfile.read(n)            # exactly n: HTTP/1.1 keeps the connection
        try:
            doc = json.loads(body)
        except json.JSONDecodeError:
            return self._error(400, "body must be JSON")
        if not isinstance(doc, dict):
            return self._error(400, "body must be a JSON object")
        ch = parts[2]
        if not CHANNEL_RE.match(ch) or ch not in list_channels(self.home):
            return self._error(404, "no such channel")
        text = doc.get("text")
        if not isinstance(text, str) or len(text.encode()) > MAX_POST_TEXT:
            return self._error(422, "text: must be a string of at most "
                                    f"{MAX_POST_TEXT} bytes")
        parent = doc.get("parent")
        if parent is not None and not is_ulid(parent):
            return self._error(422, "parent: must be a ULID")
        atts = doc.get("attachments")
        if not isinstance(atts, list) or len(atts) != 1:
            return self._error(422, "attachments: exactly one required")
        att = atts[0]
        if not isinstance(att, dict) or att.get("type") != "clan":
            return self._error(422, "attachments: must be type clan")
        if att.get("status") != "approved":
            return self._error(422, "attachments: status must be approved")
        try:
            validate_clan_attachment(att, clan_catalog(self.home))
        except ValueError as e:
            return self._error(422, str(e))
        # the page is not the guard: an approval must supersede the newest
        # proposal on this channel, or a second tab (or curl) lands a stale
        # clan.toml — same rule and wording as `clan approve`. Proposals
        # are trusted only from the orchestrator (it runs `clan propose`);
        # a role cannot kill the operator's Confirm with a rogue one.
        bus = Bus(self.home, ch)
        proposed = [m["id"] for m in bus.read_all()
                    for a in m.get("attachments") or []
                    if m["from"] == "orchestrator"
                    and isinstance(a, dict) and a.get("type") == "clan"
                    and a.get("status") == "proposed"]
        if not proposed:
            return self._error(422, "no proposed clan proposal on the bus — "
                                    "an approval must supersede the newest proposal")
        newest = proposed[-1]
        if att.get("supersedes") != newest:
            return self._error(422, f"attachment supersedes {att.get('supersedes')!r} "
                                    f"but the newest proposed clan message is {newest!r} — "
                                    "approve the newest proposal")
        msg = bus.post("stakeholder", text, parent=parent, attachments=[att])
        return self._json({"id": msg["id"]}, 201)

    def _channel_summaries(self) -> list[dict]:
        """Every sidebar row, most recently active first. The key is the last
        message's ULID, so a plain string compare is chronological; channels
        with no messages (empty `last_id`) sort last."""
        rows = [self._channel_summary(c) for c in list_channels(self.home)]
        rows.sort(key=lambda r: r["last_id"] or "", reverse=True)
        return rows

    def _clan_config(self, ch: str) -> ClanConfig | None:
        """The one reader of a channel's `clan.toml`, shared by the sidebar
        summary and the per-role meter. None for a channel with no clan or a
        broken file — one malformed clan must not empty the whole sidebar."""
        paths = ClanPaths(self.home, ch)
        try:
            return ClanConfig.from_dict(
                tomllib.loads(paths.clan_toml.read_text()), load_catalog(self.home))
        except (OSError, tomllib.TOMLDecodeError, ValueError, KeyError, TypeError):
            return None

    def _channel_summary(self, ch: str) -> dict:
        """One sidebar row. `repo`, `checkout` and `issue` come from the
        agent-writable `clan.toml`/`clan.state.json`; those paths are already
        exposed to anyone who can reach the board, the same boundary that
        serves the bus. `repo` is dropped unless it is an owner/name slug, and
        `checkout` is only ever rendered as text. A channel with no clan (or a
        broken one) keeps its row with null metadata."""
        bus = Bus(self.home, ch, read_only=True)   # a GET must not touch the bus mtime
        msgs = bus.read_all()                 # one pass: the count and the ordering key
        # read_all's contract gives every message a dict and a str id, so the
        # last line is safe to index and last_id is a str; `ts` is not part of
        # that contract, so it keeps its own check.
        last = msgs[-1] if msgs else {}
        last_id = last.get("id")
        last_ts = last.get("ts") if isinstance(last.get("ts"), str) else None
        cfg = self._clan_config(ch)
        if cfg is None:
            repo = issue = checkout = created = None
        else:
            state = read_state(ClanPaths(self.home, ch))
            if not isinstance(state, dict):
                state = {}
            repo, issue = _safe_repo(cfg.repo), cfg.issue
            checkout = state.get("checkout") or cfg.checkout
            created = state.get("created")
        return {"name": ch, "agents": bus.presence(), "count": len(msgs),
                "last_id": last_id, "last_ts": last_ts,
                "repo": repo, "issue": issue, "checkout": checkout, "created": created}

    def _clan(self, ch: str) -> dict:
        """Per-role context meters. Read-only: reads never write — the file is
        shared with `clan watch` and `clan up`, which hold an flock over
        read-modify-write cycles. `activity()` raises `ClanError` on a missing
        clan or a clan.toml/clan.state.json drift; this answers 200 instead of
        letting a `sys.exit`-shaped path kill the handler thread."""
        samples = self.server.samples.setdefault(ch, {})
        state, stale = self._state_snapshot(ch)
        try:
            return activity(ClanPaths(self.home, ch), samples=samples,
                            state=state, stale=stale)
        except ClanError:
            return {"roles": []}                  # no clan here (yet), or a drifted one

    def _state_snapshot(self, ch: str) -> tuple[dict, bool]:
        """A non-blocking shared read of `clan.state.json`, falling back to the
        last snapshot served for this channel with `stale=True` when a writer
        holds the lock. Reads never write."""
        store = self.server.snapshots
        try:
            state = _read_state_nb(ClanPaths(self.home, ch).state_json)
        except _LockBusy:
            return store.get(ch, {}), True
        store[ch] = state
        return state, False

    def _accepts_gzip(self) -> bool:
        """Whether the client actually asked for gzip. `gzip;q=0` is a refusal,
        not an offer, so the token alone is not enough."""
        for part in self.headers.get("Accept-Encoding", "").split(","):
            tok = [t.strip().lower() for t in part.split(";")]
            if tok[0] not in ("gzip", "*"):
                continue
            q = next((t[2:] for t in tok[1:] if t.startswith("q=")), "1")
            try:
                return float(q) > 0
            except ValueError:
                return False
        return False

    def _static(self, name: str):
        """A vendored asset. SEPARATE from _file on purpose: /files/ serves
        attacker-named content and carries its own sandboxing CSP; one handler
        doing both would have to reason about two threat models at once."""
        ctype = STATIC_ASSETS.get(name)
        if ctype is None:
            return self._error(404, "no such asset")
        headers = {
            # honest only because the page asks for ?v=<version>: a new version
            # is a new URL, so nothing has to expire
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
            "Vary": "Accept-Encoding",
        }
        path = STATIC / name
        if self._accepts_gzip():
            headers["Content-Encoding"] = "gzip"   # _send does no compression
            return self._send(200, gzipped(path), ctype, headers)
        return self._send(200, path.read_bytes(), ctype, headers)

    def _file(self, ch: str, name: str):
        if not CHANNEL_RE.match(ch):
            return self._error(400, "bad channel")
        files_dir = (self.home / "channels" / ch / "files").resolve()
        target = (files_dir / name).resolve()
        if files_dir not in target.parents:
            return self._error(403, "forbidden")
        if not target.is_file():
            return self._error(404, "no such file")
        guessed = mimetypes.guess_type(name)[0] or "application/octet-stream"
        headers = {
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        if guessed in INLINE_TYPES:
            ctype = guessed
        else:
            ctype = "application/octet-stream"
            safe = target.name.replace('"', "").replace("\\", "")
            headers["Content-Disposition"] = f'attachment; filename="{safe}"'
        self._send(200, target.read_bytes(), ctype, headers)

    def _unfurl(self, url: str):
        if not url.startswith(("http://", "https://")):
            return self._error(400, "url required")
        return self._json(unfurl(url))

    def _sse(self, bus: Bus, since: str | None = None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def emit(event: str, data) -> None:
            event_id = f"id: {data['id']}\n" if event == "message" else ""
            self.wfile.write(f"event: {event}\n{event_id}data: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        cursor = bus.tip() if since is None else since
        emit("hello", {"presence": bus.presence()})
        last_presence = last_ping = time.monotonic()
        clan_sig = _clan_signature(self.home, bus.channel)   # emit on change only
        while not self.server.stop.is_set():
            sig = _clan_signature(self.home, bus.channel)
            if sig != clan_sig:
                clan_sig = sig
                # a signature, not the payload: building the model here would run
                # the expensive work once per client and turn an exception into a
                # dropped stream. The client refetches, coalesced.
                emit("clan", {"sig": sig, "at": now_iso()})
            for msg in bus.read_since(cursor, limit=200):
                emit("message", msg)
                cursor = msg["id"]
            now = time.monotonic()
            if now - last_presence >= 10:
                emit("presence", {"presence": bus.presence()})
                last_presence = now
            if now - last_ping >= 15:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                last_ping = now
            time.sleep(0.5)


class BoardServer(ThreadingHTTPServer):
    """A client that walks away is not an error.

    The board holds long SSE connections open to phones and laptops on a
    tailnet; every one of them eventually drops. That raises inside
    socketserver's own read loop, before any handler runs, and the default
    `handle_error` prints a full traceback per drop — which buries the
    tracebacks that mean something.
    """

    def handle_error(self, request, client_address):
        if isinstance(sys.exception(), (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def make_server(home: Path, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    token = board_token(home)
    handler = type("Handler", (BoardHandler,), {"home": Path(home), "token": token})
    srv = BoardServer((host, port), handler)
    srv.daemon_threads = True
    srv.stop = threading.Event()
    srv.token = token
    # per-channel process-local context-token samples for the activity model:
    # the interactive liveness signal. Lives only here, never on disk.
    srv.samples = {}
    # last good clan.state.json per channel, served with stale=True on lock contention
    srv.snapshots = {}
    return srv


def main() -> None:
    ap = argparse.ArgumentParser(description="ratel board (read-only)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--home", default=str(default_home()))
    a = ap.parse_args()
    srv = make_server(Path(a.home).expanduser(), a.host, a.port)
    print(f"ratel board on http://{a.host}:{srv.server_address[1]}  home={a.home}")
    print(open_once_line(a.host, srv.server_address[1], srv.token))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
