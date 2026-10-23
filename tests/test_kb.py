"""Knowledge base edits, overlay, history, documents and ingest (graphify.kb)."""
import json

import pytest

from graphify.kb import KBError, KnowledgeBase, NotFound, edge_id
from graphify.kb_html import render_static_html, write_static_html
from tests.kb_fixtures import make_graph


@pytest.fixture
def kb(tmp_path):
    return KnowledgeBase(make_graph(tmp_path))


def test_add_update_delete_node(kb):
    node = kb.add_node("Gateway client", cls="concept", attrs={"owner": "core"})
    nid = node["id"]
    assert nid.startswith("kb:")
    assert kb.get_node(nid)["node"]["owner"] == "core"
    kb.update_node(nid, {"owner": "infra", "label": "Gateway"})
    detail = kb.get_node(nid)["node"]
    assert detail["owner"] == "infra" and detail["label"] == "Gateway"
    kb.update_node(nid, {"owner": None})
    assert "owner" not in kb.get_node(nid)["node"]
    kb.delete_node(nid)
    with pytest.raises(NotFound):
        kb.get_node(nid)


def test_add_edge_is_idempotent_and_deletable(kb):
    first = kb.add_edge("b", "a", "references")
    again = kb.add_edge("b", "a", "references")
    assert first["created"] is True and again["created"] is False
    eid = edge_id("b", "a", "references")
    assert kb.get_edge(eid)["relation"] == "references"
    kb.delete_edge(eid)
    with pytest.raises(NotFound):
        kb.get_edge(eid)


def test_edge_to_missing_node_fails(kb):
    with pytest.raises(NotFound):
        kb.add_edge("a", "missing", "calls")


def test_edits_survive_a_graph_rebuild(tmp_path):
    path = make_graph(tmp_path)
    kb = KnowledgeBase(path)
    nid = kb.add_node("Kept", cls="concept")["id"]
    kb.add_edge(nid, "a", "mentions")
    kb.update_node("a", {"owner": "core"})
    kb.flush()
    make_graph_again = json.loads(path.read_text())
    make_graph_again["nodes"] = [n for n in make_graph_again["nodes"] if n.get("_origin") != "kb"]
    make_graph_again["links"] = [e for e in make_graph_again["links"] if e.get("_origin") != "kb"]
    for n in make_graph_again["nodes"]:
        n.pop("owner", None)
    path.write_text(json.dumps(make_graph_again))  # what `graphify update` writes
    reloaded = KnowledgeBase(path)
    assert reloaded.get_node(nid)["node"]["label"] == "Kept"
    assert reloaded.get_node("a")["node"]["owner"] == "core"
    assert any(e["other"] == "a" for e in reloaded.get_node(nid)["out"])


def test_undo_walks_back(kb):
    nid = kb.add_node("Temp")["id"]
    kb.update_node(nid, {"note": "x"})
    kb.undo()
    assert "note" not in kb.get_node(nid)["node"]
    kb.undo()
    with pytest.raises(NotFound):
        kb.get_node(nid)


def test_batch_rolls_back_on_failure(kb):
    before = kb.stats()["nodes"]
    with pytest.raises((KBError, NotFound)):
        kb.batch([
            {"op": "add_node", "label": "One"},
            {"op": "add_edge", "source": "a", "target": "nope", "relation": "calls"},
        ])
    assert kb.stats()["nodes"] == before


def test_batch_applies_all_ops(kb):
    res = kb.batch([
        {"op": "add_node", "label": "One", "node_id": "kb:one"},
        {"op": "add_edge", "source": "kb:one", "target": "a", "relation": "mentions"},
    ])
    assert len(res) == 2
    assert kb.get_node("kb:one")["out_total"] == 1


def test_documents_link_wiki_targets(kb):
    doc = kb.create_document("Notes", body="See [[Alpha]] and [[New idea|concept]].", tags=["t"])
    full = kb.get_document(doc["id"])
    labels = {x["label"] for x in full["linked"]}
    assert {"Alpha", "New idea"} <= labels
    assert kb.find_node("New idea")
    kb.add_asset(doc["id"], "pic.png", b"\x89PNG fake")
    assert kb.asset_path(doc["id"], "pic.png").read_bytes().startswith(b"\x89PNG")
    kb.delete_document(doc["id"])
    assert kb.list_documents() == []


def test_unknown_document_fails(kb):
    with pytest.raises(NotFound):
        kb.get_document("nope")


def test_ingest_text_links_known_labels(kb):
    res = kb.ingest("note.md", text="# Design\n\nAlpha calls Beta.", link=True)
    assert res["mode"] == "text"
    linked = {x["id"] for x in res["document"]["linked"]}
    assert {"a", "b"} <= linked


def test_ingest_without_content_fails(kb):
    with pytest.raises(KBError):
        kb.ingest("empty.md")


def test_search_finds_nodes_and_documents(kb):
    kb.create_document("Alpha handbook", body="text")
    kinds = {hit["kind"] for hit in kb.search("alpha")}
    assert kinds == {"node", "document"}
    assert kb.search("zzzz-nothing") == []


def test_validate_flags_class_outside_ontology(kb):
    kb.set_ontology({"classes": [{"name": "code"}, {"name": "rationale"}],
                     "relations": [{"name": "calls"}, {"name": "rationale_for"}], "strict": True})
    kb.add_node("Odd", cls="alien")
    report = kb.validate()
    assert report["by_code"].get("unknown_class") == 1


def test_static_html_embeds_data_safely(kb, tmp_path):
    kb.create_document("Evil", body="</script><script>alert(1)</script>")
    page = render_static_html(kb, name="Demo")
    assert '<script id="kb-data" type="application/json">' in page
    assert "</script><script>alert(1)" not in page
    target = write_static_html(kb)
    assert target.name == "kb.html" and target.parent == kb.out_dir
