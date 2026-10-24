"""HTTP server for a graphify knowledge base: REST API, live events and the admin UI.

Start it with ``graphify kb serve``.  The server uses only the standard library.

Security model:

- It binds to 127.0.0.1 by default.  A non-loopback bind needs a token.
- With a token, every ``/api`` route except ``/api/health`` needs
  ``Authorization: Bearer <token>`` or the ``graphify_kb`` cookie from
  ``POST /api/login``.
- Every write (POST, PUT, PATCH, DELETE) needs the ``X-Graphify-KB`` header or a
  bearer token, so a web page from another origin cannot send it.
- The ``Host`` header must name this server, which blocks DNS rebinding
  (Jackson et al., "Protecting Browsers from DNS Rebinding Attacks", CCS 2007).
  On a wildcard bind (0.0.0.0 or ::) the server accepts IP literals, loopback
  names, the machine name and the ``--allow-host`` names.
- The token has at least ``MIN_TOKEN_LENGTH`` characters, so guessing it is
  not practical and a correct token always works.
- The login cookie holds a random session id, never the token. The server keeps
  the sessions in memory: logout ends one, a restart ends all.
- Failed logins and wrong bearer tokens get an exponential backoff per client
  (NIST SP 800-63B, section 5.2.2). A correct credential passes during it.
- A browser write must come from the server's own origin (``Origin`` header).
- A client that stops sending a request for ``READ_TIMEOUT`` seconds loses the
  connection, so it cannot hold a thread forever.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import queue
import re
import secrets
import signal
import socket
import ssl
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from graphify.kb import MAX_ASSET_BYTES, KBError, KnowledgeBase, NotFound
from graphify.kb_store import dumps as dumps_json

UI_DIR = Path(__file__).with_name("kb_ui")
# A JSON upload carries base64 (4/3 of the size), so the largest asset must fit.
MAX_BODY = MAX_ASSET_BYTES * 4 // 3 + 4 * 1024 * 1024
MAX_LIMIT = 100_000
# After a 413, read and drop the rest of the body for a short time, so the
# client sees the answer and not a reset connection (a "lingering close").
DRAIN_SECONDS = 15.0
DRAIN_BYTES = 2 * MAX_BODY
SESSION_MAX_AGE = 7 * 24 * 3600
MAX_SESSIONS = 10_000
MIN_TOKEN_LENGTH = 16
# Seconds that the server waits for the next bytes of a request (slowloris guard).
READ_TIMEOUT = 30.0
BOUNDARY_CAP = 2000
COOKIE = "graphify_kb"
WRITE_HEADER = "X-Graphify-KB"
# The seqs of the history entries that a write committed: "12" or "12,13" (#8177 X2).
CHANGE_HEADER = "X-Graphify-KB-Change"
UI_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/ui/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/ui/style.css": ("style.css", "text/css; charset=utf-8"),
}
UI_CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_ACTOR_RE = re.compile(r"[^A-Za-z0-9 ._@:/-]+")
_ETAG_RE = re.compile(r'(?:W/)?"([^"]*)"')
_DIGITS_RE = re.compile(r"[0-9]+")
# A new server process can run new code that builds the views differently
# (for example new ontology classes), so each process start gives new ETags.
_BOOT_TOKEN = f"{time.time_ns():x}"
# The store keeps each change at once; graph.json only makes the next load
# fast.  During a run of edits, the flush loop writes it at most once in this
# many intervals.
FLUSH_MAX_WAIT = 10


# Client libraries of outside services (LLM backends). A connection or timeout
# error from them means the service did not answer: 502, not 500.
_OUTSIDE_LIBS = ("openai", "anthropic", "httpx", "httpcore", "requests", "urllib3", "ollama", "google", "boto")


def outside_failure(exc: BaseException) -> bool:
    """Return True when ``exc`` says that an outside service did not answer."""
    from urllib.error import URLError

    if isinstance(exc, (ConnectionRefusedError, TimeoutError, socket.gaierror, URLError)):
        return True
    root = type(exc).__module__.split(".", 1)[0]
    name = type(exc).__name__
    return root in _OUTSIDE_LIBS and any(w in name for w in ("Connection", "Timeout", "Unavailable", "Overloaded"))


def _check_asset_size(data: bytes) -> None:
    if len(data) > MAX_ASSET_BYTES:
        raise TooLarge(f"the file is larger than {MAX_ASSET_BYTES // 1_048_576} MB")


def _b64(value: Any) -> bytes:
    """Decode a ``data_base64`` field; a file over the asset limit gives 413."""
    try:
        data = base64.b64decode(str(value), validate=True)
    except ValueError as exc:
        raise KBError(f"data_base64 is not base64: {exc}") from exc
    _check_asset_size(data)
    return data


class BodyError(KBError):
    """A request body that the server cannot read; the connection closes after the answer."""


class TooLarge(BodyError):
    status = 413


def _int_arg(value: Any, name: str, default: int, *, lo: int = 1, hi: int = MAX_LIMIT) -> int:
    """Read an integer query value, or raise a 400 with a short message."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise KBError(f"{name} must be an integer")
    if isinstance(value, int):
        number = value
    else:
        text = str(value).strip()
        if not re.fullmatch(r"-?[0-9]{1,12}", text):
            raise KBError(f"{name} must be an integer")
        number = int(text)
    if number < lo or number > hi:
        raise KBError(f"{name} must be between {lo} and {hi}")
    return number


def etag_matches(header: str | None, etag: str) -> bool:
    """Weak comparison for If-None-Match (RFC 9110, sections 8.8.3.2 and 13.1.2)."""
    if not header:
        return False
    if header.strip() == "*":
        return True
    mine = etag[2:] if etag.startswith("W/") else etag
    return mine.strip('"') in _ETAG_RE.findall(header)


def kb_etag(kb: KnowledgeBase) -> str:
    """Return a weak ETag for the current state of ``kb``.

    ``kb.version`` alone repeats after a restart or for another graph on the
    same port, so the tag also covers the graph path, the history length and
    newest seq, the store commit counter and the ontology file time.  It also
    covers the server process, because a restart can load code that builds
    the views differently from the same files.
    """
    with kb._lock:
        count, last = kb.history_state()
        try:
            onto = kb.onto_path.stat().st_mtime_ns
        except OSError:
            onto = 0
        parts = (_BOOT_TOKEN, str(kb.graph_path), kb.version, count, last, kb._store.seq(), onto)
    digest = hashlib.sha256(repr(parts).encode()).hexdigest()[:20]
    return f'W/"{digest}"'


class SessionStore:
    """Browser sessions in memory: a random id and its expiry time.

    The cookie holds only the id, so logout can end a session on the server
    (OWASP Session Management Cheat Sheet). A restart ends every session.
    """

    def __init__(self, *, max_age: float = SESSION_MAX_AGE, max_sessions: int = MAX_SESSIONS) -> None:
        self.max_age, self.max_sessions = max_age, max_sessions
        self._lock = threading.Lock()
        self._expiry: dict[str, float] = {}  # id hash -> expiry (monotonic seconds)

    @staticmethod
    def _key(session_id: str) -> str:
        # Keep a hash, so a dump of the memory does not give working cookies.
        return hashlib.sha256(session_id.encode()).hexdigest()

    def issue(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        session_id = secrets.token_urlsafe(32)
        with self._lock:
            for key in [k for k, until in self._expiry.items() if until <= now]:
                del self._expiry[key]
            while len(self._expiry) >= self.max_sessions:
                self._expiry.pop(next(iter(self._expiry)))
            self._expiry[self._key(session_id)] = now + self.max_age
        return session_id

    def valid(self, session_id: str | None, now: float | None = None) -> bool:
        if not session_id or len(session_id) > 200:
            return False
        now = time.monotonic() if now is None else now
        key = self._key(session_id)
        with self._lock:
            until = self._expiry.get(key)
            if until is None:
                return False
            if until <= now:
                del self._expiry[key]
                return False
            return True

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._expiry.pop(self._key(session_id), None)


class LoginGuard:
    """Exponential backoff for failed logins, per client address.

    The first ``free`` failures cost nothing. Each later failure doubles the
    wait, from ``base`` seconds up to ``cap``. The count ends ``window``
    seconds after the last failure. A success does not clear it: behind a
    proxy, many clients share one address, and one correct login must not
    reset the wait of a guessing client. NIST SP 800-63B (section 5.2.2)
    asks for this kind of throttling.
    """

    def __init__(self, *, free: int = 5, base: float = 1.0, cap: float = 300.0,
                 window: float = 900.0, max_clients: int = 4096) -> None:
        self.free, self.base, self.cap, self.window, self.max_clients = free, base, cap, window, max_clients
        self._lock = threading.Lock()
        self._state: dict[str, list[float]] = {}  # client -> [failures, last failure, blocked until]

    def retry_after(self, client: str, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.get(client)
            return max(0.0, state[2] - now) if state else 0.0

    def fail(self, client: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.get(client)
            if state is None or now - state[1] > self.window:
                state = [0.0, now, 0.0]
            state[0] += 1
            state[1] = now
            over = int(state[0]) - self.free
            if over > 0:
                state[2] = now + min(self.cap, self.base * 2 ** (over - 1))
            self._state.pop(client, None)
            self._state[client] = state
            while len(self._state) > self.max_clients:
                self._state.pop(next(iter(self._state)))


def build_overview(view: dict[str, Any]) -> dict[str, Any]:
    """Aggregate ``graph_view()`` into a community meta-graph.

    Overview first, details on demand (Shneiderman 1996); one aggregate node
    per community and weighted links between them (Elmqvist and Fekete 2010,
    Abello et al. 2006). ``links`` are unordered pairs with ``source < target``.
    """
    rows = view["nodes"]
    names = view.get("communities") or {}
    groups: dict[int, list[int]] = {}
    for i, row in enumerate(rows):
        groups.setdefault(int(row[3]), []).append(i)
    communities = []
    for cid, members in groups.items():
        classes: dict[str, int] = {}
        for i in members:
            classes[rows[i][2]] = classes.get(rows[i][2], 0) + 1
        top = sorted(members, key=lambda i: (-rows[i][4], str(rows[i][1]), str(rows[i][0])))[:5]
        communities.append({
            "id": cid,
            "name": names.get(str(cid)) or ("Unassigned" if cid < 0 else f"Community {cid}"),
            "size": len(members),
            "classes": dict(sorted(classes.items(), key=lambda kv: (-kv[1], kv[0]))),
            "top": [{"id": rows[i][0], "label": rows[i][1], "class": rows[i][2], "degree": rows[i][4]} for i in top],
        })
    communities.sort(key=lambda c: (-c["size"], c["id"]))
    weights: dict[tuple[int, int], int] = {}
    for s, t, _r in view["edges"]:
        a, b = int(rows[s][3]), int(rows[t][3])
        if a == b:
            continue
        key = (a, b) if a < b else (b, a)
        weights[key] = weights.get(key, 0) + 1
    links = [{"source": a, "target": b, "weight": w} for (a, b), w in sorted(weights.items())]
    return {"version": view.get("version"), "communities": communities, "links": links}


def build_community(view: dict[str, Any], cid: int, *, boundary_cap: int = BOUNDARY_CAP) -> dict[str, Any] | None:
    """Return one community: its nodes, the edges inside and the edges that leave it.

    ``nodes`` rows use the ``/api/graph`` format. ``edges`` rows are
    ``[source_index, target_index, relation_index]`` into this ``nodes`` list
    and ``relations``. ``boundary`` lists edges to other communities, capped.
    """
    rows = view["nodes"]
    local = {i: n for n, i in enumerate(i for i, row in enumerate(rows) if int(row[3]) == cid)}
    if not local:
        return None
    relations = view.get("relations") or []
    rel_local: dict[int, int] = {}
    edges: list[list[int]] = []
    boundary: list[dict[str, Any]] = []
    boundary_total = 0
    for s, t, r in view["edges"]:
        si, ti = local.get(s), local.get(t)
        if si is not None and ti is not None:
            ri = rel_local.setdefault(r, len(rel_local))
            edges.append([si, ti, ri])
        elif si is not None or ti is not None:
            boundary_total += 1
            if len(boundary) < boundary_cap:
                outside = t if si is not None else s
                boundary.append({"source": rows[s][0], "target": rows[t][0], "community": rows[outside][3]})
    names = view.get("communities") or {}
    return {
        "version": view.get("version"),
        "id": cid,
        "name": names.get(str(cid)) or ("Unassigned" if cid < 0 else f"Community {cid}"),
        "nodes": [rows[i] for i in local],
        "edges": edges,
        "relations": [relations[r] for r in sorted(rel_local, key=rel_local.__getitem__)],
        "boundary": boundary,
        "boundary_total": boundary_total,
        "boundary_truncated": boundary_total > len(boundary),
    }


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


# graphify writes these names in its output folder; publish never replaces them.
_OWNED_FILES = {"graph.html", "graph_tree.html"}
_OWNED_DIRS = {"kb", "cache", "obsidian", "memory", "converted", "wiki", "reflections", "schemas", "backups"}
_DATED_DIR = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
EXPORT_MARKER = 'id="kb-data"'


def publish_target(kb: KnowledgeBase, path: Any) -> Path | None:
    """Return the safe target of ``POST /api/publish``, or raise a 400.

    The file must be a new ``.html`` file inside the graphify output folder,
    or an earlier export (it holds the ``kb-data`` script). It must not be a
    file or folder that graphify or the knowledge base owns.
    """
    if path in (None, ""):
        return None
    raw = str(path).replace("\\", "/")
    out = kb.out_dir.resolve()
    target = (out / raw).resolve()
    if out not in target.parents or target.suffix.lower() != ".html":
        raise KBError("path must be an .html file inside the graphify output folder")
    rel = target.relative_to(out)
    first = rel.parts[0].lower()
    if len(rel.parts) > 1 and (first in _OWNED_DIRS or first.startswith(".") or _DATED_DIR.fullmatch(first)):
        raise KBError(f"{rel.parts[0]}/ belongs to graphify; publish to another folder")
    name = rel.name.lower()
    if len(rel.parts) == 1 and (name in _OWNED_FILES or name.endswith("-callflow.html")):
        raise KBError(f"{rel.name} belongs to graphify; choose another name")
    if target.exists():
        try:
            with target.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(1 << 20)
        except OSError as exc:
            raise KBError(f"cannot read {rel}: {exc}") from exc
        if not target.is_file() or EXPORT_MARKER not in head:
            raise KBError(f"{rel} exists and is not a knowledge base export")
    return target


def _origin_key(value: str) -> tuple[str, str, int] | None:
    """Return ``(scheme, host, port)`` of an origin, with the default port filled in."""
    try:
        parts = urlsplit(value.strip().lower())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.scheme, parts.hostname.rstrip("."), port or (443 if parts.scheme == "https" else 80)


def _host_name(value: str | None) -> str | None:
    """Return the lowercase host name of a Host header without the port, or None."""
    if not value:
        return None
    host = value.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end < 0 or (host[end + 1:] and not re.fullmatch(r":[0-9]{1,5}", host[end + 1:])):
            return None
        return host[1:end]
    name, sep, port = host.rpartition(":")
    if sep and ":" not in name:
        if not _DIGITS_RE.fullmatch(port):
            return None
        host = name
    elif sep:
        return host  # A bare IPv6 address without brackets.
    host = host.rstrip(".")
    return host if re.fullmatch(r"[a-z0-9._-]+", host) else None


def _machine_names() -> set[str]:
    try:
        name = socket.gethostname().lower().rstrip(".")
    except OSError:
        return set()
    if not name:
        return set()
    return {name, name.split(".", 1)[0], name.split(".", 1)[0] + ".local"}


class KBServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer bound to one KnowledgeBase."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        kb: KnowledgeBase,
        host: str = "127.0.0.1",
        port: int = 7878,
        *,
        token: str | None = None,
        read_only: bool = False,
        allowed_hosts: list[str] | None = None,
        ui_dir: Path | None = None,
        flush_interval: float = 1.0,
    ) -> None:
        if not is_loopback(host) and not token:
            raise KBError("a non-loopback bind needs a token (--token or GRAPHIFY_KB_TOKEN)")
        if token and len(token) < MIN_TOKEN_LENGTH:
            # A correct token passes during the login backoff, so the token
            # itself must be too long to guess.
            raise KBError(f"the token must have at least {MIN_TOKEN_LENGTH} characters; "
                          "make one with: python -c 'import secrets; print(secrets.token_urlsafe(24))'")
        self.kb = kb
        self.token = token or None
        self.read_only = read_only
        self.ui_dir = ui_dir or UI_DIR
        self._extra_hosts = {h.strip().lower().rstrip(".") for h in (allowed_hosts or []) if h.strip()}
        self._machine_names = _machine_names()
        self.logins = LoginGuard()
        self.sessions = SessionStore()
        self.json_cache: dict[str, tuple[str, bytes, bytes | None]] = {}
        self._stop = threading.Event()
        super().__init__((host, port), KBHandler)
        self.port = self.server_address[1]
        self._flusher = threading.Thread(target=self._flush_loop, args=(flush_interval,), daemon=True)
        self._flusher.start()

    @property
    def url(self) -> str:
        host = self.server_address[0]
        shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
        if ":" in shown:
            shown = f"[{shown}]"
        return f"http://{shown}:{self.port}"

    def host_allowed(self, value: str | None) -> bool:
        """Check the Host header against an allow-list (DNS rebinding guard).

        Allowed: loopback names and addresses, the bound address, the
        ``--allow-host`` names and, on a wildcard bind, any IP literal and the
        machine name. A rebinding attack always sends the attacker's DNS name.
        """
        name = _host_name(value)
        if name is None:
            return False
        if name in self._extra_hosts or is_loopback(name):
            return True
        bound = str(self.server_address[0]).lower()
        if name == bound:
            return True
        if bound in ("0.0.0.0", "::", ""):
            if name in self._machine_names:
                return True
            try:
                ipaddress.ip_address(name)
            except ValueError:
                return False
            return True
        return False

    def _flush_loop(self, interval: float) -> None:
        # Write graph.json after one quiet interval, so a run of edits does
        # not write the whole graph after each edit.
        seen, busy = self.kb.version, 0
        while not self._stop.wait(interval):
            try:
                self.kb.refresh_if_changed()
                version = self.kb.version
                busy = busy + 1 if version != seen else 0
                seen = version
                if busy == 0 or busy >= FLUSH_MAX_WAIT:
                    self.kb.flush()
                    busy = 0
            except Exception:
                pass

    def server_close(self) -> None:
        self._stop.set()
        try:
            self.kb.flush()
        finally:
            super().server_close()


class KBHandler(BaseHTTPRequestHandler):
    server: KBServer
    server_version = "graphify-kb"
    protocol_version = "HTTP/1.1"
    _body_cache: bytes | None = None
    _must_close = False

    def setup(self) -> None:
        super().setup()
        # E2E re-run N2: a client that stopped sending held a thread forever.
        # A stalled request line or header now closes the connection
        # (handle_one_request); a stalled body gives 408 (_read_exact).
        self.connection.settimeout(READ_TIMEOUT)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    # -- plumbing --------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None,
              *, cache_key: str | None = None) -> None:
        self._finish_body()
        headers = dict(headers or {})
        # Only a successful answer names its changes; a failed write committed none.
        seqs = self.server.kb.take_change_seqs()
        if seqs and status < 400:
            headers[CHANGE_HEADER] = ",".join(str(s) for s in seqs)
        if self.close_connection or self._must_close:
            headers["Connection"] = "close"
            self.close_connection = True
        gzip_ok = "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gzip_ok and len(body) > 2048 and (ctype.startswith("application/json") or ctype.startswith("text/")):
            body = self._gzip(body, cache_key, headers.get("ETag"))
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # A response with an ETag may be stored, but must be checked again each time.
        headers.setdefault("Cache-Control", "private, no-cache" if "ETag" in headers else "no-store")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _gzip(self, body: bytes, cache_key: str | None, etag: str | None) -> bytes:
        """Compress ``body``; keep the result with a cached response, so it runs once per version."""
        cache = self.server.json_cache
        hit = cache.get(cache_key) if cache_key else None
        if hit and hit[0] == etag and hit[2] is not None:
            return hit[2]
        packed = gzip.compress(body, compresslevel=5)
        if cache_key and hit and hit[0] == etag:
            with self.server.kb._lock:
                if cache.get(cache_key) is hit:
                    cache[cache_key] = (hit[0], hit[1], packed)
        return packed

    def _finish_body(self) -> None:
        """Deal with a request body that the route did not read, before the answer.

        A small body is read and dropped, so the connection stays usable.
        Otherwise the connection closes after the answer: the unread bytes
        must never be parsed as the next request.
        """
        if self._body_cache is not None or self._must_close:
            return
        if self.headers.get("Transfer-Encoding"):
            self._must_close = True
            return
        text = (self.headers.get("Content-Length") or "").strip()
        if not text or text == "0":
            return
        if _DIGITS_RE.fullmatch(text) and len(text) <= 15 and int(text) <= 1 << 20:
            try:
                self._body_cache = self.rfile.read(int(text))
                return
            except OSError:
                pass
        self._must_close = True
        if _DIGITS_RE.fullmatch(text) and len(text) <= 15:
            self._drain(int(text))

    def _json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
        body = dumps_json(data)
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message, "status": status})

    def _body(self) -> bytes:
        """Read the request body once (Content-Length or chunked, RFC 9112 section 6).

        A body over ``MAX_BODY`` gives 413. A bad length or a framing error
        gives 400. The connection closes after any body error, because the
        rest of the stream is not a request.
        """
        if self._body_cache is not None:
            return self._body_cache
        coding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        length_header = self.headers.get("Content-Length")
        if coding:
            self._must_close = True
            if length_header is not None:
                raise BodyError("do not send both Transfer-Encoding and Content-Length")
            if [c.strip() for c in coding.split(",")] != ["chunked"]:
                err = BodyError(f"Transfer-Encoding {coding} is not supported; use chunked or Content-Length")
                err.status = 501
                raise err
            try:
                body = self._read_chunked()
            except TimeoutError:
                raise self._stalled() from None
        elif length_header is None or length_header.strip() == "":
            body = b""
        else:
            text = length_header.strip()
            if not _DIGITS_RE.fullmatch(text) or len(text) > 15:
                self._must_close = True
                raise BodyError("Content-Length must be a non-negative integer")
            length = int(text)
            if length > MAX_BODY:
                self._must_close = True
                self._drain(length)
                raise TooLarge(f"the request body is larger than {MAX_BODY // 1_048_576} MB")
            body = self._read_exact(length)
            if len(body) != length:
                self._must_close = True
                raise BodyError("the request body ended early")
        self._body_cache = body
        return body

    def _read_exact(self, length: int) -> bytes:
        """Read ``length`` body bytes; a stalled client gives 408 and a close."""
        try:
            return self.rfile.read(length) if length else b""
        except TimeoutError:
            raise self._stalled() from None

    def _stalled(self) -> BodyError:
        self._must_close = True
        err = BodyError(f"the request body did not arrive within {READ_TIMEOUT:g} seconds")
        err.status = 408
        return err

    def _read_chunked(self) -> bytes:
        parts: list[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(1026)
            if not line.endswith(b"\n"):
                raise BodyError("a chunk header is broken")
            size_text = line.split(b";", 1)[0].strip()
            if not re.fullmatch(rb"[0-9A-Fa-f]{1,16}", size_text):
                raise BodyError("a chunk size is not hexadecimal")
            size = int(size_text, 16)
            if size == 0:
                break
            total += size
            if total > MAX_BODY:
                self._drain(DRAIN_BYTES)
                raise TooLarge(f"the request body is larger than {MAX_BODY // 1_048_576} MB")
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                raise BodyError("a chunk ended early")
            parts.append(chunk)
        # Skip the trailer fields up to the empty line.
        for _ in range(100):
            line = self.rfile.readline(8192)
            if line in (b"\r\n", b"\n", b""):
                break
        return b"".join(parts)

    def _drain(self, length: int) -> None:
        """Read and drop up to ``length`` body bytes for a short time.

        Without this, the client is still sending when the server closes, and
        the TCP reset hides the 413 answer (BrokenPipe on the client).
        """
        left = min(length, DRAIN_BYTES)
        deadline = time.monotonic() + DRAIN_SECONDS
        try:
            self.connection.settimeout(2.0)
            while left > 0 and time.monotonic() < deadline:
                chunk = self.rfile.read1(min(left, 1 << 20)) if hasattr(self.rfile, "read1") else self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                left -= len(chunk)
        except (OSError, ValueError):
            pass

    def _json_body(self) -> dict[str, Any]:
        raw = self._body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KBError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise KBError("the request body must be a JSON object")
        return data

    def _actor(self) -> str:
        raw = self.headers.get("X-Graphify-Actor") or ("ui" if self.headers.get(WRITE_HEADER) else "api")
        return _ACTOR_RE.sub("", raw)[:60] or "api"

    def _client(self) -> str:
        return str(self.client_address[0]) if self.client_address else "?"

    def _cookie_session(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return None
        morsel = jar.get(COOKIE)
        return morsel.value if morsel else None

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.lower().startswith("bearer ") else None

    def _auth_failure(self) -> int | None:
        """Return None for an allowed request, else 401 or 429.

        A correct credential always passes, also during a login backoff
        (E2E re-run N4: one guessing client locked out every agent behind
        the same proxy address). A wrong one counts as a failure and gets
        429 while the backoff runs. The token length rule keeps guessing
        impractical without the backoff.
        """
        token = self.server.token
        if not token:
            return None
        if self.server.sessions.valid(self._cookie_session()):
            return None
        bearer = self._bearer()
        if bearer is None:
            return 401
        return self._check_secret(bearer)

    def _check_secret(self, given: str) -> int | None:
        """Compare ``given`` with the token; count a wrong one. Return None, 401 or 429."""
        token = self.server.token or ""
        if hmac.compare_digest(given.encode(), token.encode()):
            return None
        client = self._client()
        waiting = self.server.logins.retry_after(client) > 0
        self.server.logins.fail(client)
        return 429 if waiting else 401

    def _foreign_origin(self) -> bool:
        """True when a browser sent this request from another site.

        E2E re-run N7: the ``Origin`` header of a write must name this
        server (scheme, host and port), as OWASP's CSRF cheat sheet advises.
        A request without the header (an agent, curl) passes.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return False
        key = _origin_key(origin)
        if key is None:
            return True
        # A reverse proxy can rewrite Host; the --allow-host names stay valid origins.
        if key[1] in self.server._extra_hosts:
            return False
        scheme = "https" if self._https() else "http"
        return key != _origin_key(f"{scheme}://{self.headers.get('Host') or ''}")

    def _https(self) -> bool:
        return isinstance(self.connection, ssl.SSLSocket) or (
            (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" and is_loopback(self._client())
        )

    # -- dispatch --------------------------------------------------------
    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:
        self._body_cache, self._must_close = None, False
        self._error(405, "cross-origin requests are not allowed")

    def _handle(self, method: str) -> None:
        self._body_cache: bytes | None = None
        self._must_close = False
        # A keep-alive connection reuses this thread: forget older seqs.
        self.server.kb.take_change_seqs()
        try:
            if not self.server.host_allowed(self.headers.get("Host")):
                self._error(403, "unknown Host header")
                return
            parts = urlsplit(self.path)
            path = parts.path
            query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            if not path.startswith("/api/"):
                if method != "GET":
                    self._error(405, "method not allowed")
                    return
                self._ui(path)
                return
            if path == "/api/health":
                self._json(200, {"ok": True, "version": self.server.kb.version, "auth": bool(self.server.token), "read_only": self.server.read_only})
                return
            if path == "/api/login" and method == "POST":
                self._login()
                return
            if path == "/api/logout" and method == "POST":
                self._logout()
                return
            failure = self._auth_failure()
            if failure == 429:
                self._too_many(self.server.logins.retry_after(self._client()))
                return
            if failure is not None:
                self._error(401, "a token is required" if self._bearer() is None else "the token is wrong")
                return
            if method != "GET":
                if self.server.read_only:
                    self._error(403, "the server is read-only")
                    return
                if not self.headers.get(WRITE_HEADER) and not self._bearer():
                    self._error(403, f"writes need the {WRITE_HEADER} header or a bearer token")
                    return
                if not self._bearer() and self._foreign_origin():
                    self._error(403, "writes from another site are not allowed")
                    return
            self._api(method, path, query)
        except KBError as exc:
            status = int(getattr(exc, "status", 400))
            if isinstance(exc, BodyError):
                self._must_close = True
            message = str(exc) or HTTPStatus(status).phrase
            if status == 502:
                message = f"an outside service did not answer: {message}"
            self._error(status, message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - last resort
            if outside_failure(exc):
                # E2E bug 15: say which part failed, without a stack trace.
                self._error(502, f"an outside service did not answer ({type(exc).__name__})")
                return
            # Keep the details in the server log; the client gets a short message.
            traceback.print_exc(file=sys.stderr)
            try:
                self._error(500, "internal error; see the server log")
            except Exception:
                pass
        finally:
            if self._must_close:
                self.close_connection = True

    def _login(self) -> None:
        token = self.server.token
        data = self._json_body()
        if token:
            failure = self._check_secret(str(data.get("token") or ""))
            if failure == 429:
                self._too_many(self.server.logins.retry_after(self._client()))
                return
            if failure is not None:
                self._error(401, "the token is wrong")
                return
        headers = {}
        if token:
            # E2E re-run N3: a random id that logout can revoke, not a signed time.
            value = self.server.sessions.issue()
            secure = "; Secure" if self._https() else ""
            headers["Set-Cookie"] = (
                f"{COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_MAX_AGE}{secure}"
            )
        self._json(200, {"ok": True}, headers)

    def _too_many(self, wait: float) -> None:
        self._json(429, {"error": "too many failed logins; wait and try again", "status": 429},
                   {"Retry-After": str(int(wait) + 1)})

    def _logout(self) -> None:
        self.server.sessions.revoke(self._cookie_session())
        secure = "; Secure" if self._https() else ""
        self._json(200, {"ok": True},
                   {"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0{secure}"})

    def _cached_json(self, key: str, build: Any) -> None:
        """Answer with a JSON body that has an ETag, or 304 when the client has it."""
        kb = self.server.kb
        cache = self.server.json_cache
        # Read the tag and the value under one lock, so a write cannot slip in between.
        with kb._lock:
            etag = kb_etag(kb)
            hit = cache.get(key)
            value = None
            if not etag_matches(self.headers.get("If-None-Match"), etag) and (hit is None or hit[0] != etag):
                value = build()
                if value is None:
                    raise NotFound(f"no {key.split(':', 1)[0]} {key.split(':', 1)[-1]}")
        if etag_matches(self.headers.get("If-None-Match"), etag):
            self._finish_body()
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, no-cache")
            self.send_header("Content-Length", "0")
            if self.close_connection or self._must_close:
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
            return
        if value is not None:
            # graph_view() builds new lists for each version and never changes them,
            # so the encoding can run outside the lock.
            body = dumps_json(value)
            hit = (etag, body, None)
            with kb._lock:
                for old in [k for k, v in cache.items() if v[0] != etag]:
                    del cache[old]
                if len(cache) >= 64:
                    cache.pop(next(iter(cache)))
                cache[key] = hit
        assert hit is not None  # no value is built only for a fresh cached hit
        self._send(200, hit[1], "application/json; charset=utf-8", {"ETag": etag}, cache_key=key)

    def _ui(self, path: str) -> None:
        spec = UI_FILES.get(path)
        if spec is None:
            self._error(404, "not found")
            return
        name, ctype = spec
        file = self.server.ui_dir / name
        try:
            body = file.read_bytes()
        except OSError:
            self._error(404, f"{name} is missing")
            return
        self._send(200, body, ctype, {"Content-Security-Policy": UI_CSP, "X-Frame-Options": "DENY"})

    def _api(self, method: str, path: str, q: dict[str, str]) -> None:
        kb = self.server.kb
        actor = self._actor()
        segs = [unquote(s) for s in path[len("/api/"):].split("/")]
        head = segs[0]
        rest = "/".join(segs[1:])

        if head == "events" and method == "GET":
            self._events()
            return
        if method == "GET":
            kb.refresh_if_changed()

        if head == "stats" and method == "GET":
            self._json(200, kb.stats())
        elif head == "graph" and method == "GET":
            self._cached_json("graph", kb.graph_view)
        elif head == "overview" and method == "GET" and not rest:
            self._cached_json("overview", lambda: build_overview(kb.graph_view()))
        elif head == "community" and method == "GET" and rest:
            cid = _int_arg(rest, "community id", 0, lo=-1, hi=10**9)
            self._cached_json(f"community:{cid}", lambda: build_community(kb.graph_view(), cid))
        elif head == "nodes":
            self._nodes(method, rest, q, actor)
        elif head == "edges":
            self._edges(method, rest, q, actor)
        elif head == "batch" and method == "POST":
            data = self._json_body()
            self._json(200, {"results": kb.batch(data.get("ops"), actor=actor), "version": kb.version})
        elif head == "search" and method == "GET":
            limit = _int_arg(q.get("limit"), "limit", 50, hi=1000)
            self._json(200, {"results": kb.search(q.get("q", ""), limit=limit, cls=q.get("class") or None)})
        elif head == "ontology":
            self._ontology(method, segs[1:], actor)
        elif head == "validate" and method == "GET":
            limit = _int_arg(q.get("limit"), "limit", 500)
            self._json(200, kb.validate(limit=limit, include_info=q.get("info", "1") != "0"))
        elif head == "documents":
            self._documents(method, segs[1:], q, actor)
        elif head == "ingest" and method == "POST":
            self._ingest(q, actor)
        elif head == "history" and method == "GET":
            self._json(200, {"history": kb.history_view(limit=_int_arg(q.get("limit"), "limit", 100))})
        elif head == "undo" and method == "POST":
            data = self._json_body()
            seq = data.get("seq")
            seq = None if seq is None else _int_arg(seq, "seq", 0, hi=10**15)
            self._json(200, kb.undo(seq=seq, actor=actor))
        elif head == "publish" and method == "POST":
            from graphify.kb_html import write_static_html

            data = self._json_body()
            target = publish_target(kb, data.get("path"))
            target = write_static_html(kb, target)
            self._json(200, {"path": str(target), "bytes": target.stat().st_size})
        elif head == "export" and method == "GET":
            self._send(200, kb.export_json(assets=q.get("assets") == "1"), "application/json; charset=utf-8")
        elif head == "flush" and method == "POST":
            self._json(200, {"written": kb.flush()})
        else:
            self._error(404, f"no route {method} {path}")

    def _nodes(self, method: str, nid: str, q: dict[str, str], actor: str) -> None:
        kb = self.server.kb
        if not nid:
            if method == "POST":
                d = self._json_body()
                node = kb.add_node(
                    d.get("label", ""),
                    cls=d.get("class"),
                    node_id=d.get("id"),
                    attrs=d.get("attrs"),
                    file_type=d.get("file_type"),
                    source_file=d.get("source_file"),
                    actor=actor,
                )
                self._json(201, node)
                return
            if method == "GET" and q.get("label"):
                node = kb.find_node(q["label"])
                if node is None:
                    raise NotFound(f"no node has the label {q['label']}")
                self._json(200, kb.get_node(str(node["id"])))
                return
            self._error(405, "use POST /api/nodes or GET /api/nodes/<id>")
            return
        if method == "GET":
            self._json(200, kb.get_node(nid, limit=_int_arg(q.get("limit"), "limit", 200)))
        elif method == "PATCH":
            d = self._json_body()
            attrs = d.get("attrs") if isinstance(d.get("attrs"), dict) else d
            self._json(200, kb.update_node(nid, attrs, actor=actor))
        elif method == "DELETE":
            self._json(200, kb.delete_node(nid, actor=actor))
        else:
            self._error(405, "method not allowed")

    def _edges(self, method: str, eid: str, q: dict[str, str], actor: str) -> None:
        kb = self.server.kb
        if not eid:
            if method == "POST":
                d = self._json_body()
                self._json(201, kb.add_edge(
                    str(d.get("source", "")),
                    str(d.get("target", "")),
                    str(d.get("relation", "")),
                    confidence=d.get("confidence") or "EXTRACTED",
                    attrs=d.get("attrs"),
                    actor=actor,
                ))
                return
            if method == "DELETE" and q.get("source"):
                from graphify.kb import edge_id

                self._json(200, kb.delete_edge(edge_id(q.get("source"), q.get("target"), q.get("relation")), actor=actor))
                return
            self._error(405, "use POST /api/edges or /api/edges/<id>")
            return
        if method == "GET":
            self._json(200, kb.get_edge(eid))
        elif method == "PATCH":
            d = self._json_body()
            attrs = d.get("attrs") if isinstance(d.get("attrs"), dict) else d
            self._json(200, kb.update_edge(eid, attrs, actor=actor))
        elif method == "DELETE":
            self._json(200, kb.delete_edge(eid, actor=actor))
        else:
            self._error(405, "method not allowed")

    def _ontology(self, method: str, segs: list[str], actor: str) -> None:
        kb = self.server.kb
        if not segs or segs == [""]:
            if method == "GET":
                self._json(200, kb.ontology_view())
            elif method == "PUT":
                self._json(200, kb.set_ontology(self._json_body(), actor=actor))
            else:
                self._error(405, "method not allowed")
            return
        kind = segs[0]
        if kind == "infer" and method == "POST":
            d = self._json_body()
            self._json(200, kb.infer_ontology(save=bool(d.get("save")), actor=actor))
            return
        if kind in ("classes", "relations") and len(segs) >= 2 and segs[1]:
            name = "/".join(segs[1:])
            if method == "PUT":
                d = self._json_body()
                d["name"] = name
                fn = kb.put_class if kind == "classes" else kb.put_relation
                self._json(200, fn(d, actor=actor))
                return
            if method == "DELETE":
                fn = kb.delete_class if kind == "classes" else kb.delete_relation
                self._json(200, fn(name, actor=actor))
                return
        self._error(404, "no such ontology route")

    def _documents(self, method: str, segs: list[str], q: dict[str, str], actor: str) -> None:
        kb = self.server.kb
        if not segs or segs == [""]:
            if method == "GET":
                self._json(200, {"documents": kb.list_documents()})
            elif method == "POST":
                d = self._json_body()
                self._json(201, kb.create_document(
                    d.get("title", ""),
                    body=d.get("body", ""),
                    tags=d.get("tags"),
                    nodes=d.get("nodes"),
                    source=d.get("source"),
                    doc_id=d.get("id"),
                    # N3: a new document links its text, like ingest.
                    link=bool(d.get("link", True)),
                    actor=actor,
                ))
            else:
                self._error(405, "method not allowed")
            return
        doc_id = segs[0]
        if len(segs) == 1:
            if method == "GET":
                self._json(200, kb.get_document(doc_id))
            elif method == "PATCH":
                d = self._json_body()
                self._json(200, kb.update_document(
                    doc_id,
                    title=d.get("title"),
                    body=d.get("body"),
                    tags=d.get("tags"),
                    nodes=d.get("nodes"),
                    actor=actor,
                ))
            elif method == "DELETE":
                self._json(200, kb.delete_document(doc_id, actor=actor))
            else:
                self._error(405, "method not allowed")
            return
        if segs[1] == "review" and len(segs) == 2:
            # D3: decide a review item.  {"mention", "node"}; node null = not a link.
            if method != "POST":
                self._error(405, "method not allowed")
                return
            d = self._json_body()
            node = d.get("node")
            if node is not None and not isinstance(node, str):
                raise KBError("node must be a node id or null")
            mention = d.get("mention")
            if not isinstance(mention, str):
                raise KBError("mention must be text")
            self._json(200, kb.review_mention(doc_id, mention, node, actor=actor))
            return
        if segs[1] != "assets":
            self._error(404, "no such document route")
            return
        name = segs[2] if len(segs) > 2 else q.get("name", "")
        if method == "GET" and name:
            self._asset(doc_id, name)
        elif method in ("POST", "PUT"):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype == "application/json":
                d = self._json_body()
                data = _b64(d.get("data_base64") or "")
                info = kb.add_asset(doc_id, d.get("name") or name, data, mime=d.get("mime"), actor=actor)
            else:
                body = self._body()
                _check_asset_size(body)
                info = kb.add_asset(doc_id, name, body, mime=ctype or None, actor=actor)
            self._json(201, info)
        elif method == "DELETE" and name:
            self._json(200, kb.delete_asset(doc_id, name, actor=actor))
        else:
            self._error(405, "method not allowed")

    def _asset(self, doc_id: str, name: str) -> None:
        path = self.server.kb.asset_path(doc_id, name)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        inline = ctype.startswith("image/") or ctype == "application/pdf" or ctype.startswith("text/plain")
        headers = {
            "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
            "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{path.name}"',
        }
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=60")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _ingest(self, q: dict[str, str], actor: str) -> None:
        kb = self.server.kb
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype == "application/json":
            d = self._json_body()
            data = None
            if "data_base64" in d:
                # An empty string is an empty file (b""), not "no file" (E2E bug 17).
                data = _b64(d["data_base64"])
                if not data:
                    if not d.get("text") and not d.get("url"):
                        raise KBError(f"{d.get('name') or 'the file'} is empty")
                    data = None
            tags = d.get("tags")
            result = kb.ingest(
                str(d.get("name") or ""),
                data=data,
                text=d.get("text"),
                url=d.get("url"),
                mode=str(d.get("mode") or "text"),
                title=d.get("title"),
                tags=tags if isinstance(tags, list) else None,
                link=d.get("link", True) is not False,
                backend=d.get("backend"),
                model=d.get("model"),
                actor=actor,
            )
        else:
            tags = [t for t in (q.get("tags") or "").split(",") if t.strip()]
            body = self._body()
            if not body:
                raise KBError(f"{q.get('name') or 'the file'} is empty")  # E2E bug 17
            _check_asset_size(body)
            result = kb.ingest(
                q.get("name") or "upload",
                data=body,
                content_type=ctype or None,
                mode=q.get("mode", "text"),
                title=q.get("title") or None,
                tags=tags or None,
                link=q.get("link", "1") != "0",
                backend=q.get("backend") or None,
                model=q.get("model") or None,
                actor=actor,
            )
        self._json(201, result)

    def _events(self) -> None:
        events: queue.Queue = queue.Queue(maxsize=1000)

        def listener(event: dict[str, Any]) -> None:
            try:
                events.put_nowait(event)
            except queue.Full:
                pass

        unsubscribe = self.server.kb.subscribe(listener)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            hello = {"type": "hello", "version": self.server.kb.version}
            self.wfile.write(f"data: {json.dumps(hello)}\n\n".encode())
            self.wfile.flush()
            last = time.monotonic()
            while not self.server._stop.is_set():
                try:
                    event = events.get(timeout=1.0)
                except queue.Empty:
                    if time.monotonic() - last > 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last = time.monotonic()
                    continue
                self.wfile.write(f"data: {json.dumps(event, default=str)}\n\n".encode())
                self.wfile.flush()
                last = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unsubscribe()


def serve(
    graph_path: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 7878,
    token: str | None = None,
    read_only: bool = False,
    allowed_hosts: list[str] | None = None,
    open_browser: bool = False,
) -> None:
    """Run the knowledge base server until Ctrl+C, SIGTERM or SIGHUP."""
    kb = KnowledgeBase(graph_path, autoflush=False)
    server = KBServer(kb, host, port, token=token, read_only=read_only, allowed_hosts=allowed_hosts)
    restore = _stop_on_signals(server)
    counts = kb.counts()
    print(f"graphify knowledge base: {counts['nodes']} nodes, {counts['edges']} edges, "
          f"{counts['documents']} documents", flush=True)
    print(f"Serving {kb.graph_path} at {server.url}", flush=True)
    if token:
        print("Token auth is on.", flush=True)
    if open_browser:
        import webbrowser

        webbrowser.open(server.url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        restore()


def _stop_on_signals(server: KBServer) -> Any:
    """Stop ``server`` on SIGTERM and SIGHUP as on Ctrl+C; return a function that undoes it.

    E2E re-run N1: a process manager stops the server with SIGTERM. Without a
    handler the process died before ``server_close`` wrote graph.json.
    ``shutdown`` waits for ``serve_forever``, which runs in this thread, so a
    helper thread calls it. Python runs signal handlers only in the main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    old: dict[int, Any] = {}

    def stop(signum: int, frame: Any) -> None:
        threading.Thread(target=server.shutdown, name="kb-stop", daemon=True).start()

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            old[sig] = signal.signal(sig, stop)

    def restore() -> None:
        for sig, handler in old.items():
            signal.signal(sig, handler)

    return restore
