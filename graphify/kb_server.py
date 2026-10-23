"""HTTP server for a graphify knowledge base: REST API, live events and the admin UI.

Start it with ``graphify kb serve``.  The server uses only the standard library.

Security model:

- It binds to 127.0.0.1 by default.  A non-loopback bind needs a token.
- With a token, every ``/api`` route except ``/api/health`` needs
  ``Authorization: Bearer <token>`` or the ``graphify_kb`` cookie from
  ``POST /api/login``.
- Every write (POST, PUT, PATCH, DELETE) needs the ``X-Graphify-KB`` header or a
  bearer token, so a web page from another origin cannot send it.
- The ``Host`` header must name this server, which blocks DNS rebinding.
"""

from __future__ import annotations

import base64
import gzip
import hmac
import ipaddress
import json
import mimetypes
import queue
import re
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from graphify.kb import KBError, KnowledgeBase, NotFound

UI_DIR = Path(__file__).with_name("kb_ui")
MAX_BODY = 80 * 1024 * 1024
COOKIE = "graphify_kb"
WRITE_HEADER = "X-Graphify-KB"
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


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


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
            raise ValueError("a non-loopback bind needs a token (--token or GRAPHIFY_KB_TOKEN)")
        self.kb = kb
        self.token = token or None
        self.read_only = read_only
        self.ui_dir = ui_dir or UI_DIR
        self._extra_hosts = {h.lower() for h in (allowed_hosts or [])}
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
        if not value:
            return False
        host = value.lower()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        bound = str(self.server_address[0]).lower()
        if host in self._extra_hosts or name in self._extra_hosts:
            return True
        if bound in ("0.0.0.0", "::", ""):
            return True
        return is_loopback(name.strip("[]")) or name.strip("[]") == bound

    def _flush_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.kb.flush()
                self.kb.refresh_if_changed()
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

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    # -- plumbing --------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        gzip_ok = "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gzip_ok and len(body) > 2048 and (ctype.startswith("application/json") or ctype.startswith("text/")):
            body = gzip.compress(body, compresslevel=5)
            headers = dict(headers or {})
            headers["Content-Encoding"] = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message, "status": status})

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise KBError(f"request body is larger than {MAX_BODY // 1_048_576} MB")
        return self.rfile.read(length) if length > 0 else b""

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

    def _cookie_token(self) -> str | None:
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

    def _authorized(self) -> bool:
        token = self.server.token
        if not token:
            return True
        for given in (self._bearer(), self._cookie_token()):
            if given and hmac.compare_digest(given.encode(), token.encode()):
                return True
        return False

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
        self._error(405, "cross-origin requests are not allowed")

    def _handle(self, method: str) -> None:
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
            if not self._authorized():
                self._error(401, "a token is required")
                return
            if method != "GET":
                if self.server.read_only:
                    self._error(403, "the server is read-only")
                    return
                if not self.headers.get(WRITE_HEADER) and not self._bearer():
                    self._error(403, f"writes need the {WRITE_HEADER} header or a bearer token")
                    return
            self._api(method, path, query)
        except KBError as exc:
            self._error(getattr(exc, "status", 400), str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - last resort
            try:
                self._error(500, f"{type(exc).__name__}: {exc}")
            except Exception:
                pass

    def _login(self) -> None:
        data = self._json_body()
        token = self.server.token
        given = str(data.get("token") or "")
        if token and not hmac.compare_digest(given.encode(), token.encode()):
            self._error(401, "the token is wrong")
            return
        headers = {}
        if token:
            headers["Set-Cookie"] = f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"
        self._json(200, {"ok": True}, headers)

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
            etag = f'"v{kb.version}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._json(200, kb.graph_view(), {"ETag": etag})
        elif head == "nodes":
            self._nodes(method, rest, q, actor)
        elif head == "edges":
            self._edges(method, rest, q, actor)
        elif head == "batch" and method == "POST":
            data = self._json_body()
            self._json(200, {"results": kb.batch(data.get("ops"), actor=actor), "version": kb.version})
        elif head == "search" and method == "GET":
            self._json(200, {"results": kb.search(q.get("q", ""), limit=int(q.get("limit", 50)), cls=q.get("class") or None)})
        elif head == "ontology":
            self._ontology(method, segs[1:], actor)
        elif head == "validate" and method == "GET":
            self._json(200, kb.validate(limit=int(q.get("limit", 500)), include_info=q.get("info", "1") != "0"))
        elif head == "documents":
            self._documents(method, segs[1:], q, actor)
        elif head == "ingest" and method == "POST":
            self._ingest(q, actor)
        elif head == "history" and method == "GET":
            self._json(200, {"history": kb.history_view(limit=int(q.get("limit", 100)))})
        elif head == "undo" and method == "POST":
            data = self._json_body()
            self._json(200, kb.undo(seq=data.get("seq"), actor=actor))
        elif head == "publish" and method == "POST":
            from graphify.kb_html import write_static_html

            data = self._json_body()
            target = None
            if data.get("path"):
                # Keep HTTP writes inside the graphify output folder.
                target = (kb.out_dir / str(data["path"])).resolve()
                if kb.out_dir.resolve() not in target.parents or target.suffix != ".html":
                    raise KBError("path must be an .html file inside the graphify output folder")
            target = write_static_html(kb, target)
            self._json(200, {"path": str(target), "bytes": target.stat().st_size})
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
            self._json(200, kb.get_node(nid, limit=int(q.get("limit", 200))))
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
                try:
                    data = base64.b64decode(str(d.get("data_base64") or ""), validate=True)
                except ValueError as exc:
                    raise KBError(f"data_base64 is not base64: {exc}") from exc
                info = kb.add_asset(doc_id, d.get("name") or name, data, mime=d.get("mime"), actor=actor)
            else:
                info = kb.add_asset(doc_id, name, self._body(), mime=ctype or None, actor=actor)
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
            if d.get("data_base64"):
                try:
                    data = base64.b64decode(str(d["data_base64"]), validate=True)
                except ValueError as exc:
                    raise KBError(f"data_base64 is not base64: {exc}") from exc
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
            result = kb.ingest(
                q.get("name") or "upload",
                data=self._body(),
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
    """Run the knowledge base server until Ctrl+C."""
    kb = KnowledgeBase(graph_path, autoflush=False)
    server = KBServer(kb, host, port, token=token, read_only=read_only, allowed_hosts=allowed_hosts)
    stats = kb.stats()
    print(f"graphify knowledge base: {stats['nodes']} nodes, {stats['edges']} edges, {stats['documents']} documents", flush=True)
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
