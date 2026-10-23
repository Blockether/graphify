"""HTTP API of the knowledge base server (graphify.kb_server) and the kb CLI."""
import base64
import json
import threading
import urllib.error
import urllib.request

import pytest

from graphify.kb import KnowledgeBase
from graphify.kb_cli import main as kb_main
from graphify.kb_server import KBServer
from tests.kb_fixtures import make_graph


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
    status, res = call(server, "POST", "/api/ingest", {"name": "n.md", "text": "Alpha and Beta."})
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
    srv = KBServer(kb, "127.0.0.1", 0, token="s3cret")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert call(srv, "GET", "/api/stats")[0] == 401
        assert call(srv, "GET", "/api/stats", headers={"Authorization": "Bearer s3cret"})[0] == 200
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
