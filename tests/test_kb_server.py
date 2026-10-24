"""HTTP API of the knowledge base server (graphify.kb_server) and the kb CLI."""
import base64
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from graphify.kb import KnowledgeBase
from graphify.kb_cli import main as kb_main
from graphify.kb_server import KBServer
from tests.kb_fixtures import make_graph

TOKEN = "kb-test-token-0123456789"


@pytest.fixture
def server(tmp_path):
    kb = KnowledgeBase(make_graph(tmp_path), autoflush=False)
    srv = KBServer(kb, "127.0.0.1", 0, flush_interval=0.05)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    hdrs = {"X-Graphify-KB": "1", "Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(srv.url + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, json.loads(payload) if "json" in ctype else payload
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def test_health_stats_and_graph(server):
    assert call(server, "GET", "/api/health")[0] == 200
    status, stats = call(server, "GET", "/api/stats")
    assert status == 200 and stats["nodes"] == 3
    status, view = call(server, "GET", "/api/graph")
    assert len(view["nodes"]) == 3 and len(view["edges"]) == 2


def test_node_and_edge_crud(server):
    status, node = call(server, "POST", "/api/nodes", {"label": "Agent note", "class": "concept"})
    assert status == 201
    nid = node["id"]
    assert call(server, "PATCH", f"/api/nodes/{nid}", {"attrs": {"owner": "vis"}})[0] == 200
    status, edge = call(server, "POST", "/api/edges", {"source": nid, "target": "a", "relation": "mentions"})
    assert status in (200, 201)
    status, detail = call(server, "GET", f"/api/nodes/{nid}")
    assert detail["node"]["owner"] == "vis" and detail["out_total"] == 1
    assert call(server, "DELETE", f"/api/edges/{edge['id']}")[0] == 200
    assert call(server, "DELETE", f"/api/nodes/{nid}")[0] == 200
    assert call(server, "GET", f"/api/nodes/{nid}")[0] == 404


def test_write_without_header_is_refused(server):
    req = urllib.request.Request(server.url + "/api/nodes", data=b'{"label":"x"}', method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 403


def test_bad_request_is_400(server):
    status, err = call(server, "POST", "/api/edges", {"source": "a"})
    assert status == 400 and "error" in err


def test_ontology_validate_and_documents(server):
    status, onto = call(server, "POST", "/api/ontology/infer", {"save": True})
    assert status == 200 and {c["name"] for c in onto["classes"]} >= {"code", "rationale"}
    assert call(server, "PUT", "/api/ontology/classes/service", {"description": "A service"})[0] == 200
    status, report = call(server, "GET", "/api/validate?limit=10")
    assert status == 200 and "counts" in report
    status, doc = call(server, "POST", "/api/documents", {"title": "Ops", "body": "See [[Alpha]]."})
    assert status == 201
    png = base64.b64encode(b"\x89PNG data").decode()
    assert call(server, "POST", f"/api/documents/{doc['id']}/assets", {"name": "x.png", "data_base64": png})[0] == 201
    status, data = call(server, "GET", f"/api/documents/{doc['id']}/assets/x.png")
    assert status == 200 and data.startswith(b"\x89PNG")
    status, hits = call(server, "GET", "/api/search?q=ops")
    assert any(h["kind"] == "document" for h in hits["results"])


def test_ingest_history_and_undo(server):
    status, res = call(server, "POST", "/api/ingest", {"name": "n.md", "text": "`Alpha` and `Beta`."})
    assert status in (200, 201) and res["linked"] >= 2
    status, hist = call(server, "GET", "/api/history")
    assert hist["history"][0]["summary"].startswith("Ingest")
    assert call(server, "POST", "/api/undo", {})[0] == 200
    assert call(server, "GET", f"/api/documents/{res['document']['id']}")[0] == 404


def test_publish_stays_inside_output_folder(server):
    status, res = call(server, "POST", "/api/publish", {})
    assert status == 200 and res["path"].endswith("kb.html")
    assert call(server, "POST", "/api/publish", {"path": "../outside.html"})[0] == 400


def test_read_only_server_refuses_writes(tmp_path):
    kb = KnowledgeBase(make_graph(tmp_path), autoflush=False)
    srv = KBServer(kb, "127.0.0.1", 0, read_only=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert call(srv, "POST", "/api/nodes", {"label": "x"})[0] == 403
        assert call(srv, "GET", "/api/stats")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_token_is_required_when_set(tmp_path):
    kb = KnowledgeBase(make_graph(tmp_path), autoflush=False)
    srv = KBServer(kb, "127.0.0.1", 0, token=TOKEN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert call(srv, "GET", "/api/stats")[0] == 401
        assert call(srv, "GET", "/api/stats", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_non_loopback_without_token_is_refused(tmp_path):
    kb = KnowledgeBase(make_graph(tmp_path), autoflush=False)
    with pytest.raises(Exception):
        KBServer(kb, "0.0.0.0", 0)


def test_ui_is_served(server):
    status, page = call(server, "GET", "/")
    assert status == 200 and b"graphify" in page


def test_cli_validate_infer_export_ingest(tmp_path, capsys):
    graph = make_graph(tmp_path)
    assert kb_main(["infer", str(graph), "--save"]) == 0
    assert (graph.parent / "ontology.json").is_file()
    assert kb_main(["validate", str(graph), "--strict"]) == 0
    note = tmp_path / "note.md"
    note.write_text("# Note\n\nAlpha is here.")
    assert kb_main(["ingest", str(note), "--graph", str(graph)]) == 0
    assert kb_main(["export", str(graph)]) == 0
    assert (graph.parent / "kb.html").is_file()
    assert kb_main(["validate", str(tmp_path / "missing.json")]) == 1
    assert "Ingested" in capsys.readouterr().out


def test_cli_move(tmp_path, capsys):
    graph = make_graph(tmp_path)
    assert kb_main(["move", "a", "a_v2", "gone", "elsewhere", "--graph", str(graph), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"from": "a", "to": "a_v2", "status": "pending"},
        {"from": "gone", "to": "elsewhere", "status": "skipped"},
    ]
    assert KnowledgeBase(graph).overlay["moved_nodes"] == {"a": "a_v2"}
    assert kb_main(["move", "b", "b_v2", "--graph", str(graph)]) == 0
    assert capsys.readouterr().out.strip() == "0 moved, 1 pending, 0 skipped"
    assert kb_main(["move", "a", "--graph", str(graph)]) == 1
    assert "pairs of node ids" in capsys.readouterr().err



# -- E2E report regressions (task C1) -----------------------------------------
def raw_request(srv, head: str, body: bytes = b"", host: str | None = None) -> tuple[int, dict, bytes]:
    """Send one hand-made request on a new socket; return status, headers and body."""
    import socket

    sock = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
    try:
        lines = head.replace("{host}", host or f"127.0.0.1:{srv.port}")
        sock.sendall(lines.encode() + b"\r\n" + body)
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    top, _, rest = data.partition(b"\r\n\r\n")
    first, *fields = top.decode("latin-1").split("\r\n")
    headers = {k.lower(): v.strip() for k, v in (f.split(":", 1) for f in fields)}
    return int(first.split()[1]), headers, rest


def test_bad_integers_are_400_not_500(server):
    # E2E bug 10: a bad integer gave 500 with Python exception text.
    for path in ("/api/search?q=a&limit=abc", "/api/validate?limit=x", "/api/history?limit=1e3",
                 "/api/search?q=a&limit=-5", "/api/nodes/a?limit=0"):
        status, err = call(server, "GET", path)
        assert status == 400, path
        assert "Error" not in err["error"] and "invalid literal" not in err["error"]
    assert call(server, "POST", "/api/undo", {"seq": "abc"}) == (400, {"error": "seq must be an integer", "status": 400})
    assert call(server, "POST", "/api/undo", {"seq": True})[0] == 400
    status, _, body = raw_request(server, "POST /api/nodes HTTP/1.1\r\nHost: {host}\r\nX-Graphify-KB: 1\r\n"
                                          "Content-Type: application/json\r\nContent-Length: abc\r\n", b'{"label":"x"}')
    assert status == 400 and b"Content-Length" in body


def test_oversized_body_gets_413_and_a_clean_close(server, monkeypatch):
    # E2E bug 11: the server answered before it read the body, so the client saw a reset.
    import graphify.kb_server as ks

    monkeypatch.setattr(ks, "MAX_BODY", 1000)
    status, headers, body = raw_request(server, "POST /api/ingest HTTP/1.1\r\nHost: {host}\r\nX-Graphify-KB: 1\r\n"
                                                "Content-Type: application/json\r\nContent-Length: 300000\r\n",
                                        b"x" * 300000)
    assert status == 413 and headers.get("connection") == "close"
    assert json.loads(body)["status"] == 413
    assert call(server, "GET", "/api/health")[0] == 200


def test_chunked_body_is_read(server):
    # E2E bug 14: a chunked upload created an empty document.
    chunked = b"6\r\n# Note\r\n9\r\n\nAlpha ok\r\n0\r\n\r\n"
    status, _, body = raw_request(server, "POST /api/ingest?name=chunk.md HTTP/1.1\r\nHost: {host}\r\nX-Graphify-KB: 1\r\n"
                                          "Content-Type: text/markdown\r\nTransfer-Encoding: chunked\r\n", chunked)
    assert status == 201
    doc = server.kb.get_document(json.loads(body)["document"]["id"])
    assert doc["body"].strip() == "# Note\nAlpha ok"
    status, _, _ = raw_request(server, "POST /api/ingest?name=x.md HTTP/1.1\r\nHost: {host}\r\nX-Graphify-KB: 1\r\n"
                                       "Transfer-Encoding: gzip, chunked\r\n", b"0\r\n\r\n")
    assert status == 501
    status, _, _ = raw_request(server, "POST /api/ingest?name=x.md HTTP/1.1\r\nHost: {host}\r\nX-Graphify-KB: 1\r\n"
                                       "Transfer-Encoding: chunked\r\nContent-Length: 5\r\n", b"0\r\n\r\n")
    assert status == 400
    status, _, _ = raw_request(server, "POST /api/ingest?name=x.md HTTP/1.1\r\nHost: {host}\r\nX-Graphify-KB: 1\r\n"
                                       "Transfer-Encoding: chunked\r\n", b"zz\r\nhello\r\n0\r\n\r\n")
    assert status == 400
    # E2E bug 17: an empty upload is refused the same way on both paths.
    assert call(server, "POST", "/api/ingest?name=e.md", raw=b"", headers={"Content-Type": "text/plain"})[0] == 400
    assert call(server, "POST", "/api/ingest", {"name": "e.md", "data_base64": ""})[0] == 400
    assert len(server.kb.list_documents()) == 1


def test_unread_body_does_not_poison_keep_alive(server):
    # A body that a route does not read must never be parsed as the next request.
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.request("POST", "/api/nope", body=b"GET /api/stats HTTP/1.1\r\n\r\n", headers={"X-Graphify-KB": "1"})
        first = conn.getresponse()
        first.read()
        assert first.status == 404
        conn.request("GET", "/api/health")
        second = conn.getresponse()
        assert second.status == 200 and json.loads(second.read())["ok"] is True
    finally:
        conn.close()


def test_publish_refuses_files_that_graphify_owns(server):
    # E2E bug 12: publish overwrote graph.html and wrote into kb/documents.
    out = server.kb.out_dir
    (out / "graph.html").write_text("<html>graphify</html>")
    (out / "notes.html").write_text("<html>mine</html>")
    for path in ("graph.html", "GRAPH_TREE.html", "kb/documents/x/assets/evil.html", "kb/x.html",
                 "cache/x.html", ".hidden/x.html", "2026-01-01/graph.html", "vis-callflow.html",
                 "notes.html", "../outside.html", "GRAPH_REPORT.md"):
        status, err = call(server, "POST", "/api/publish", {"path": path})
        assert status == 400, path
    assert (out / "graph.html").read_text() == "<html>graphify</html>"
    assert (out / "notes.html").read_text() == "<html>mine</html>"
    assert not (out / "kb" / "documents" / "x").exists()
    assert call(server, "POST", "/api/publish", {"path": "pub/kb.html"})[0] == 200
    assert call(server, "POST", "/api/publish", {"path": "pub/kb.html"})[0] == 200  # replace its own export


def test_export_in_a_subfolder_keeps_asset_links(tmp_path):
    # E2E bug 13: asset URLs were relative to graphify-out, not to the page.
    from graphify.kb_html import static_payload

    kb = KnowledgeBase(make_graph(tmp_path), autoflush=False)
    assert static_payload(kb)["asset_base"] == "kb/documents/"
    assert static_payload(kb, target=kb.out_dir / "pub" / "deep" / "x.html")["asset_base"] == "../../kb/documents/"
    outside = tmp_path / "site" / "kb.html"
    base = static_payload(kb, target=outside)["asset_base"]
    assert (outside.parent / base).resolve() == kb.docs_dir.resolve()


def test_unavailable_service_is_502(server, monkeypatch):
    # E2E bug 15: an LLM backend that does not answer must be a 502 with a clear message.
    from graphify.kb import Unavailable

    def down(*_a, **_k):
        raise Unavailable("ollama at 127.0.0.1:11434 refused the connection")

    monkeypatch.setattr(server.kb, "search", down)
    status, err = call(server, "GET", "/api/search?q=a")
    assert status == 502 and "ollama" in err["error"] and "did not answer" in err["error"]

    # A raw client error (openai.APIConnectionError in the report) is a 502 too, not a 500.
    APIConnectionError = type("APIConnectionError", (Exception,), {"__module__": "openai._exceptions"})

    def refused(*_a, **_k):
        raise APIConnectionError("Connection error.")

    monkeypatch.setattr(server.kb, "search", refused)
    status, err = call(server, "GET", "/api/search?q=a")
    assert status == 502 and "APIConnectionError" in err["error"]


def test_unexpected_error_hides_the_exception_text(server, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("secret detail /etc/passwd")

    monkeypatch.setattr(server.kb, "stats", broken)
    status, err = call(server, "GET", "/api/stats")
    assert status == 500 and "secret" not in err["error"]


def test_etag_is_weak_matches_lists_and_changes_after_restart(tmp_path, monkeypatch):
    # E2E bug 16: ETag "v1" repeated after a restart; W/ and list forms never matched.
    from graphify import kb_server
    from graphify.kb_server import etag_matches

    assert etag_matches('W/"abc"', 'W/"abc"') and etag_matches('"abc"', 'W/"abc"')
    assert etag_matches('"x", W/"abc" , "y"', 'W/"abc"') and etag_matches("*", 'W/"abc"')
    assert not etag_matches('"abcd"', 'W/"abc"') and not etag_matches("", 'W/"abc"')

    def start(kb):
        srv = KBServer(kb, "127.0.0.1", 0, flush_interval=0.05)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return srv

    def get(srv, etag=None):
        req = urllib.request.Request(srv.url + "/api/graph", headers={"If-None-Match": etag} if etag else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers.get("ETag"), resp.headers.get("Cache-Control")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("ETag"), exc.headers.get("Cache-Control")

    graph = make_graph(tmp_path)
    srv = start(KnowledgeBase(graph, autoflush=False))
    try:
        status, tag, cache = get(srv)
        assert status == 200 and (tag or "").startswith('W/"') and cache == "private, no-cache"
        assert get(srv, tag)[0] == 304 and get(srv, f'"other", {tag}')[0] == 304
        assert call(srv, "POST", "/api/nodes", {"label": "New idea", "class": "concept"})[0] == 201
        assert get(srv, tag)[0] == 200
        status, tag, _ = get(srv)
    finally:
        srv.shutdown()
        srv.server_close()
    # kb.version persists, so only a new process (a new boot token) changes an unchanged store's tag.
    srv = start(KnowledgeBase(graph, autoflush=False))
    try:
        assert get(srv, tag)[0] == 304  # same process, same content
    finally:
        srv.shutdown()
        srv.server_close()
    monkeypatch.setattr(kb_server, "_BOOT_TOKEN", "restarted")
    srv = start(KnowledgeBase(graph, autoflush=False))
    try:
        assert get(srv, tag)[0] == 200  # a new process must not reuse the old tag
    finally:
        srv.shutdown()
        srv.server_close()


def test_etag_changes_with_the_server_process(tmp_path, monkeypatch):
    # A restart with new code (new ontology classes) kept the old tag, so browsers kept stale classes.
    from graphify import kb_server

    kb = KnowledgeBase(make_graph(tmp_path), autoflush=False)
    tag = kb_server.kb_etag(kb)
    assert kb_server.kb_etag(kb) == tag
    monkeypatch.setattr(kb_server, "_BOOT_TOKEN", "another-process")
    assert kb_server.kb_etag(kb) != tag


def test_wildcard_bind_checks_the_host_header(tmp_path):
    # E2E bug 21: on 0.0.0.0 every Host was accepted, so the DNS rebinding guard was off.
    kb = KnowledgeBase(make_graph(tmp_path), autoflush=False)
    srv = KBServer(kb, "0.0.0.0", 0, token=TOKEN, allowed_hosts=["kb.example.com"])
    try:
        allowed = ["127.0.0.1:7878", "localhost", "[::1]:80", "10.0.0.5:7878", "KB.example.com:443", "kb.example.com."]
        refused = ["evil.example.com", "localhost.evil.com", "127.0.0.1.nip.io", "", None, "kb.example.com:x",
                   "a b", "[::1"]
        assert all(srv.host_allowed(h) for h in allowed)
        assert not any(srv.host_allowed(h) for h in refused)
    finally:
        srv.server_close()
    srv = KBServer(kb, "127.0.0.1", 0)
    try:
        assert srv.host_allowed("127.0.0.1:1") and not srv.host_allowed("10.0.0.5")
    finally:
        srv.server_close()


@pytest.fixture
def token_server(tmp_path):
    kb = KnowledgeBase(make_graph(tmp_path), autoflush=False)
    srv = KBServer(kb, "127.0.0.1", 0, token=TOKEN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def login(srv, token=TOKEN):
    """Log in and return the cookie value."""
    req = urllib.request.Request(srv.url + "/api/login", data=json.dumps({"token": token}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Graphify-KB": "1"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Max-Age=" in cookie
    assert "Secure" not in cookie  # plain HTTP on loopback
    return cookie.split(";", 1)[0].split("=", 1)[1]


def test_login_cookie_is_a_session_id_with_backoff(token_server):
    # E2E bug 23: the cookie held the token itself and failed logins had no limit.
    srv = token_server
    value = login(srv)
    assert TOKEN not in value and len(value) >= 40
    assert call(srv, "GET", "/api/stats", headers={"Cookie": f"graphify_kb={value}"})[0] == 200
    forged = value[:-1] + ("A" if value[-1] != "A" else "B")
    assert call(srv, "GET", "/api/stats", headers={"Cookie": f"graphify_kb={forged}"})[0] == 401
    assert call(srv, "GET", "/api/stats", headers={"Cookie": f"graphify_kb={TOKEN}"})[0] == 401
    # Five failures are free; the sixth starts a wait, so the seventh gets 429.
    codes = [call(srv, "POST", "/api/login", {"token": "wrong"})[0] for _ in range(7)]
    assert codes[:6] == [401] * 6 and codes[6:] == [429]
    assert call(srv, "GET", "/api/stats", headers={"Cookie": f"graphify_kb={value}"})[0] == 200


def test_correct_credentials_pass_during_a_login_backoff(token_server):
    # E2E re-run N4: wrong logins from one address locked out the correct bearer token
    # of every client behind the same proxy.
    srv = token_server
    for _ in range(6):
        call(srv, "POST", "/api/login", {"token": "wrong"})
    assert srv.logins.retry_after("127.0.0.1") > 0
    assert call(srv, "GET", "/api/stats", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 200
    assert call(srv, "POST", "/api/login", {"token": TOKEN})[0] == 200
    # A correct login does not end the wait of the guessing client.
    status, body = call(srv, "POST", "/api/login", {"token": "wrong"})
    assert status == 429 and "too many" in body["error"]
    assert call(srv, "GET", "/api/stats", headers={"Authorization": "Bearer wrong-token-0000000"})[0] == 429


def test_logout_revokes_the_session(token_server):
    # E2E re-run N3: the old cookie still worked after logout, for 7 days.
    srv = token_server
    value, other = login(srv), login(srv)
    status, _ = call(srv, "POST", "/api/logout", {}, headers={"Cookie": f"graphify_kb={value}"})
    assert status == 200
    assert call(srv, "GET", "/api/stats", headers={"Cookie": f"graphify_kb={value}"})[0] == 401
    # Logout ends one session, not the others.
    assert call(srv, "GET", "/api/stats", headers={"Cookie": f"graphify_kb={other}"})[0] == 200


def test_short_token_is_refused_at_start(tmp_path):
    # E2E re-run N4: a correct token passes during the backoff, so the token must be long.
    from graphify.kb import KBError
    from graphify.kb_server import MIN_TOKEN_LENGTH

    kb = KnowledgeBase(make_graph(tmp_path), autoflush=False)
    with pytest.raises(KBError, match=f"at least {MIN_TOKEN_LENGTH} characters"):
        KBServer(kb, "127.0.0.1", 0, token="s3cret")
    rc = kb_main(["serve", str(kb.graph_path), "--port", "0", "--token", "short"])
    assert rc == 1


def test_cookie_write_from_another_origin_is_refused(token_server):
    # E2E re-run N7: a cookie write with a foreign Origin header was accepted.
    srv = token_server
    cookie = {"Cookie": f"graphify_kb={login(srv)}"}
    status, body = call(srv, "POST", "/api/nodes", {"label": "Evil"},
                        headers={**cookie, "Origin": "https://evil.example.com"})
    assert status == 403 and "another site" in body["error"]
    assert call(srv, "POST", "/api/nodes", {"label": "Evil"}, headers={**cookie, "Origin": "null"})[0] == 403
    own = f"http://127.0.0.1:{srv.port}"
    assert call(srv, "POST", "/api/nodes", {"label": "Own"}, headers={**cookie, "Origin": own})[0] == 201
    assert call(srv, "POST", "/api/nodes", {"label": "No origin"}, headers=cookie)[0] == 201
    # A bearer token is not sent by a browser on its own, so the Origin does not matter.
    bearer = {"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example.com"}
    assert call(srv, "POST", "/api/nodes", {"label": "Agent"}, headers=bearer)[0] == 201
    # A name from --allow-host is a valid origin, also when a proxy rewrites Host.
    srv._extra_hosts.add("kb.example.com")
    proxied = {**cookie, "Origin": "https://kb.example.com"}
    assert call(srv, "POST", "/api/nodes", {"label": "Proxied"}, headers=proxied)[0] == 201
    # Reads keep working from any origin.
    assert call(srv, "GET", "/api/stats", headers={**cookie, "Origin": "https://evil.example.com"})[0] == 200


def test_stalled_body_times_out(server, monkeypatch):
    # E2E re-run N2: a client that sent fewer body bytes than Content-Length held a thread forever.
    import socket
    import time

    import graphify.kb_server as kb_server

    monkeypatch.setattr(kb_server, "READ_TIMEOUT", 0.5)
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    try:
        start = time.monotonic()
        sock.sendall(f"POST /api/nodes HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nX-Graphify-KB: 1\r\n"
                     "Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n{\"label\":".encode())
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
        took = time.monotonic() - start
    finally:
        sock.close()
    assert data.startswith(b"HTTP/1.1 408") and b"Connection: close" in data
    assert took < 5
    # Stalled headers close the connection without an answer.
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    try:
        sock.sendall(b"GET /api/stats HTTP/1.1\r\nHost: 127.0.0.1\r\n")
        assert sock.recv(65536) == b""
    finally:
        sock.close()
    assert call(server, "GET", "/api/stats")[0] == 200


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_flushes_the_graph(tmp_path):
    # E2E re-run N1: SIGTERM right after a write left graph.json without the change.
    import os
    import signal
    import subprocess

    graph = make_graph(tmp_path)
    proc = subprocess.Popen([sys.executable, "-m", "graphify", "kb", "serve", str(graph), "--port", "0"],
                            stdout=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    try:
        url = None
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith("Serving "):
                url = line.rsplit(" at ", 1)[1].strip()
                break
        assert url, "the server did not start"
        req = urllib.request.Request(url + "/api/nodes", data=b'{"id": "term:zero", "label": "Term zero"}',
                                     method="POST", headers={"X-Graphify-KB": "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 201
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    ids = {n["id"] for n in json.loads(graph.read_text())["nodes"]}
    assert "term:zero" in ids


def test_login_guard_backoff_doubles_and_resets():
    from graphify.kb_server import LoginGuard

    guard = LoginGuard(free=2, base=1.0, cap=8.0, window=100.0)
    for _ in range(2):
        guard.fail("c", now=0.0)
    assert guard.retry_after("c", now=0.0) == 0.0
    waits = []
    for _ in range(5):
        guard.fail("c", now=0.0)
        waits.append(guard.retry_after("c", now=0.0))
    assert waits == [1.0, 2.0, 4.0, 8.0, 8.0]
    assert guard.retry_after("other", now=0.0) == 0.0
    # The count starts again a window after the last failure.
    guard.fail("c", now=200.0)
    assert guard.retry_after("c", now=200.0) == 0.0


def test_session_store_expires_and_revokes():
    from graphify.kb_server import SessionStore

    store = SessionStore(max_age=100.0, max_sessions=2)
    first = store.issue(now=0.0)
    assert store.valid(first, now=50.0) and not store.valid(first, now=100.0)
    a, b = store.issue(now=0.0), store.issue(now=0.0)
    store.revoke(a)
    assert not store.valid(a, now=1.0) and store.valid(b, now=1.0)
    assert not store.valid(None) and not store.valid("x" * 300)
    # Over the cap, the oldest session goes.
    c, d = store.issue(now=2.0), store.issue(now=2.0)
    assert not store.valid(b, now=3.0) and store.valid(c, now=3.0) and store.valid(d, now=3.0)


def test_overview_and_community(server):
    # Overview first, details on demand (memo R8): community meta-graph and drill-down.
    kb = server.kb
    for node, community in zip(kb.data["nodes"], (0, 1, 0)):
        node["community"] = community
        node["community_name"] = "Core" if community == 0 else "Edge"
    kb.version += 1
    kb._cache.clear()
    status, ov = call(server, "GET", "/api/overview")
    assert status == 200 and ov["version"] == kb.version
    by_id = {c["id"]: c for c in ov["communities"]}
    assert by_id[0]["name"] == "Core" and by_id[0]["size"] == 2 and by_id[0]["classes"] == {"code": 1, "rationale": 1}
    assert by_id[0]["top"][0] == {"id": "a", "label": "Alpha", "class": "code", "degree": 2}
    assert ov["links"] == [{"source": 0, "target": 1, "weight": 1}]  # a->b; r->a stays inside 0
    status, com = call(server, "GET", "/api/community/0")
    assert status == 200 and [n[0] for n in com["nodes"]] == ["a", "r"]
    assert com["edges"] == [[1, 0, 0]] and com["relations"] == ["rationale_for"]
    assert com["boundary"] == [{"source": "a", "target": "b", "community": 1}] and not com["boundary_truncated"]
    assert call(server, "GET", "/api/community/7")[0] == 404
    assert call(server, "GET", "/api/community/x")[0] == 400


def test_ingest_status_and_asset_limit(server, monkeypatch):
    # Leader #8075: the server keeps the 415 of an unsupported type unchanged.
    from graphify import kb_server
    from graphify.kb import KBError

    def unsupported(*_a, **_k):
        err = KBError("tool.exe: this file type is not supported")
        err.status = 415
        raise err

    monkeypatch.setattr(server.kb, "ingest", unsupported)
    status, err = call(server, "POST", "/api/ingest?name=tool.exe", raw=b"MZ\x90\x00",
                       headers={"Content-Type": "application/x-msdownload"})
    assert status == 415 and "not supported" in err["error"]
    # Leader #8075 item 3: a file over the asset limit is 413, on the raw and the JSON path.
    monkeypatch.setattr(kb_server, "MAX_ASSET_BYTES", 1000)
    big = b"x" * 1001
    assert call(server, "POST", "/api/ingest?name=big.md", raw=big,
                headers={"Content-Type": "text/plain"})[0] == 413
    payload = {"name": "big.md", "data_base64": base64.b64encode(big).decode()}
    assert call(server, "POST", "/api/ingest", payload)[0] == 413
    doc = server.kb.create_document("Files")
    assert call(server, "POST", f"/api/documents/{doc['id']}/assets?name=big.png", raw=big,
                headers={"Content-Type": "image/png"})[0] == 413


def test_raw_ingest_sniffs_programs_and_keeps_the_content_type(server):
    # C2 #8073: tool.exe sent as application/x-msdownload created a document (201).
    exe = b"MZ\x90\x00" + bytes(60)
    status, err = call(server, "POST", "/api/ingest?name=tool.exe", raw=exe,
                       headers={"Content-Type": "application/x-msdownload"})
    assert status == 415 and "not supported" in err["error"]
    # Only the Content-Type of a raw body tells that this upload is a program.
    status, err = call(server, "POST", "/api/ingest?name=tool", raw=b"plain words",
                       headers={"Content-Type": "application/x-msdownload"})
    assert status == 415
    # A text file sent as application/octet-stream stays text.
    status, res = call(server, "POST", "/api/ingest?name=notes", raw=b"# Notes\n\nplain words",
                       headers={"Content-Type": "application/octet-stream"})
    assert status == 201 and res["document"]["title"] == "Notes"


def test_writes_name_their_change_seqs(server):
    # #8177 X2: a client must undo its own change, not a later one of another writer.
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    hdrs = {"X-Graphify-KB": "1", "Content-Type": "application/json"}

    def send(method, path, body=None):
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=hdrs)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"null")
        return resp.status, resp.getheader("X-Graphify-KB-Change"), data

    try:
        status, first, node = send("POST", "/api/nodes", {"label": "Seq one", "node_id": "kb:seq-one"})
        assert status == 201 and first
        status, _, hist = send("GET", "/api/history")
        assert int(first) == hist["history"][0]["seq"]
        status, second, _ = send("POST", "/api/nodes", {"label": "Seq two"})
        assert status == 201 and second and second != first
        status, header, _ = send("GET", f"/api/nodes/{node['id']}")
        assert status == 200 and header is None
        status, header, _ = send("POST", "/api/edges", {"source": "a", "target": "b", "relation": "uses"})
        assert status == 409 and header is None
    finally:
        conn.close()


def test_review_route_decides_a_mention(tmp_path):
    # D3: POST /api/documents/<id>/review {"mention", "node"}; node null = not a link.
    import http.client

    path = make_graph(tmp_path)
    graph = json.loads(path.read_text())
    graph["nodes"] += [
        {"id": "g1", "label": "request!()", "file_type": "code", "source_file": "src/gateway/client.clj"},
        {"id": "g2", "label": "request!()", "file_type": "code", "source_file": "src/council/core.clj"},
    ]
    path.write_text(json.dumps(graph))
    kb = KnowledgeBase(path, autoflush=False)
    doc_id = kb.create_document("Notes", body="The `request!` call sends jobs.", link=True)["id"]
    srv = KBServer(kb, "127.0.0.1", 0, flush_interval=0.05)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    hdrs = {"X-Graphify-KB": "1", "Content-Type": "application/json"}

    def send(method, route, body=None):
        conn.request(method, route, body=json.dumps(body) if body is not None else None, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, resp.getheader("X-Graphify-KB-Change"), json.loads(resp.read() or b"null")

    try:
        status, _, listed = send("GET", "/api/documents")
        assert status == 200 and listed["documents"][0]["review"] == 1
        route = f"/api/documents/{doc_id}/review"
        assert send("POST", route, {"mention": "Alpha", "node": "a"})[0] == 400
        assert send("POST", route, {"mention": "", "node": None})[0] == 400
        assert send("POST", route, {"mention": "request!", "node": "nope"})[0] == 404
        assert send("POST", "/api/documents/nope/review", {"mention": "request!", "node": None})[0] == 404
        assert send("GET", route)[0] == 405
        status, change, doc = send("POST", route, {"mention": "request!", "node": "g1"})
        assert status == 200 and change
        assert doc["id"] == doc_id and doc["possible"] == [] and doc["decisions"] == {"request!": "g1"}
        assert "body" in doc and any(x["id"] == "g1" for x in doc["linked"])
        _, _, hist = send("GET", "/api/history")
        assert hist["history"][0]["seq"] == int(change)
        assert hist["history"][0]["summary"] == "Review request! in Notes"
        status, _, listed = send("GET", "/api/documents")
        assert listed["documents"][0]["review"] == 0
    finally:
        conn.close()
        srv.shutdown()
        srv.server_close()


def test_new_documents_link_and_edge_delete_unlinks(server):
    # #8229 items 2 and 3: POST /api/documents links by default (N3), and
    # DELETE /api/edges/<id> of a document's mentions edge changes the document (N4).
    from urllib.parse import quote

    from graphify.kb import edge_id

    status, doc = call(server, "POST", "/api/documents", {"title": "Notes", "body": "The `Alpha` call."})
    assert status == 201 and doc["link"] is True and "a" in doc["auto_nodes"]
    eid = quote(edge_id(f"doc:{doc['id']}", "a", "mentions"), safe="")
    status, res = call(server, "DELETE", f"/api/edges/{eid}")
    assert status == 200 and res == {"deleted": edge_id(f"doc:{doc['id']}", "a", "mentions"), "document": doc["id"]}
    status, after = call(server, "GET", f"/api/documents/{doc['id']}")
    assert after["auto_nodes"] == [] and after["decisions"] == {"alpha": None}
    status, hist = call(server, "GET", "/api/history")
    assert hist["history"][0]["summary"] == "Unlink Alpha from Notes"
    status, wiki = call(server, "POST", "/api/documents", {"title": "Wiki", "body": "See [[Beta]].", "link": False})
    assert status == 201 and wiki["link"] is False
    eid = quote(edge_id(f"doc:{wiki['id']}", "b", "mentions"), safe="")
    status, err = call(server, "DELETE", f"/api/edges/{eid}")
    assert status == 409 and err["error"] == "the text links it with [[Beta]]; edit the text"
