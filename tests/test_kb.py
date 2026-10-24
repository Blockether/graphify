"""Knowledge base edits, overlay, history, documents and ingest (graphify.kb)."""
import json

import pytest

from graphify.kb import (
    EDGE_ANCHOR,
    PATCH_ANCHOR,
    Conflict,
    KBError,
    KnowledgeBase,
    NotFound,
    UndoConflict,
    apply_overlay,
    edge_id,
    move_document_ids,
    patch_anchor,
)
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
    res = kb.ingest("note.md", text="# Design\n\n`Alpha` calls `Beta`.", link=True)
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


def _method_kb(tmp_path):
    out = tmp_path / "graphify-out"
    out.mkdir()
    graph = {"directed": True, "multigraph": False, "graph": {}, "nodes": [
        {"id": "cls_svc", "label": "Service", "file_type": "code", "source_file": "s.py"},
        {"id": "fn_run", "label": ".run()", "file_type": "code", "source_file": "s.py"},
    ], "links": [{"source": "cls_svc", "target": "fn_run", "relation": "method", "confidence": "EXTRACTED"}]}
    (out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    return KnowledgeBase(out / "graph.json")


def test_search_class_filter_takes_subclasses(tmp_path):
    # Found in the vis-graphify release check: code nodes have subclasses, so cls="code" found no method.
    kb = _method_kb(tmp_path)
    assert [hit["class"] for hit in kb.search("run")] == ["code.method"]
    for cls in ("code", "code.function", "code.method"):
        assert [hit["id"] for hit in kb.search("run", cls=cls)] == ["fn_run"]
    assert kb.search("run", cls="code.class") == []
    assert [hit["id"] for hit in kb.search("service", cls="code")] == ["cls_svc"]


def test_wiki_links_find_code_nodes_by_parent_class_and_by_id(tmp_path):
    # Found in the same release check: [[run|code]] made a second node next to the code.method node,
    # and a link to an exact node id went to review.
    kb = _method_kb(tmp_path)
    doc = kb.create_document("Notes", body="See [[run|code]] and [[cls_svc]].")
    full = kb.get_document(doc["id"])
    assert {x["id"] for x in full["linked"]} == {"fn_run", "cls_svc"} and full["possible"] == []
    assert [n["id"] for n in kb.data["nodes"] if n.get("label") in (".run()", "run")] == ["fn_run"]


def test_validate_flags_class_outside_ontology(kb):
    # E2E bug 18: an open ontology accepts the class and validation warns.
    kb.set_ontology({"classes": [{"name": "code"}, {"name": "rationale"}],
                     "relations": [{"name": "calls"}, {"name": "rationale_for"}]})
    kb.add_node("Odd", cls="alien")
    report = kb.validate()
    assert report["by_code"].get("unknown_class") == 1


def test_strict_ontology_refuses_unknown_class(kb):
    # E2E bug 18: a strict ontology refuses the class and names the known ones.
    kb.set_ontology({"classes": [{"name": "code"}, {"name": "rationale"}],
                     "relations": [{"name": "calls"}, {"name": "rationale_for"}], "strict": True})
    with pytest.raises(KBError, match="Known classes: .*rationale"):
        kb.add_node("Odd", cls="alien")
    node = kb.add_node("Fine", cls="rationale")
    with pytest.raises(KBError, match="alien"):
        kb.update_node(node["id"], {"class": "alien"})


def test_two_processes_never_repeat_a_seq(tmp_path):
    # E2E bug 2: two stores on one graph (a CLI and a server) share one history.
    graph = make_graph(tmp_path)
    server, cli = KnowledgeBase(graph), KnowledgeBase(graph)
    server.add_node("From server", cls="concept")
    cli.add_node("From CLI", cls="concept")
    server.add_node("Server again", cls="concept")
    for store in (server, cli):
        store.refresh_if_changed()
        assert [e["seq"] for e in store.history] == [1, 2, 3]
    labels = {n["label"] for n in KnowledgeBase(graph).data["nodes"]}
    assert {"From server", "From CLI", "Server again"} <= labels
    assert server.undo()["undone"] == 3
    assert cli.undo()["undone"] == 2  # the CLI change is in the shared history


def test_undo_refuses_when_a_later_edge_needs_the_node(kb):
    # E2E bug 3: undo of add_node must not leave the later edge dangling.
    node = kb.add_node("Lease", cls="concept")
    kb.add_edge(node["id"], "a", "references")
    first, second = (e["seq"] for e in kb.history)
    with pytest.raises(UndoConflict) as err:
        kb.undo(seq=first)
    assert err.value.status == 409 and err.value.later == [second]
    assert str(second) in str(err.value)
    assert kb.undo()["undone"] == second
    assert kb.undo(seq=first)["undone"] == first
    assert kb.validate()["by_code"].get("dangling_edge") is None


def test_undo_refuses_when_a_later_change_wrote_the_same_field(kb):
    # E2E bug 4: undo of an older update must not drop the later update.
    node = kb.add_node("Gateway", cls="concept")
    kb.update_node(node["id"], {"owner": "core"})
    kb.update_node(node["id"], {"owner": "infra"})
    seqs = [e["seq"] for e in kb.history]
    with pytest.raises(UndoConflict) as err:
        kb.undo(seq=seqs[1])
    assert err.value.later == [seqs[2]]
    assert kb.get_node(node["id"])["node"]["owner"] == "infra"


def test_selective_undo_of_an_independent_change(kb):
    # Selective undo (Berlage 1994; Prakash and Knister 1994): a change that no
    # later change touches can go alone.
    first = kb.add_node("First", cls="concept")
    second = kb.add_node("Second", cls="concept")
    kb.undo(seq=kb.history[0]["seq"])
    with pytest.raises(NotFound):
        kb.get_node(first["id"])
    assert kb.get_node(second["id"])["node"]["label"] == "Second"
    with pytest.raises(Conflict):
        kb.undo(seq=kb.history[0]["seq"])  # undone already


def test_undo_seq_must_be_an_integer(kb):
    kb.add_node("X", cls="concept")
    with pytest.raises(KBError):
        kb.undo(seq="one")
    with pytest.raises(NotFound):
        kb.undo(seq=99)


def test_version_survives_a_restart_and_only_grows(tmp_path):
    # E2E bug 16: ETags use the version, so it must not repeat after a restart.
    graph = make_graph(tmp_path)
    store = KnowledgeBase(graph)
    store.add_node("One", cls="concept")
    before = store.version
    again = KnowledgeBase(graph)
    assert again.version == before
    again.add_node("Two", cls="concept")
    assert again.version > before
    assert KnowledgeBase(graph).version == again.version


def _write_overlay(graph, overlay):
    folder = graph.parent / "kb"
    folder.mkdir(exist_ok=True)
    (folder / "overlay.json").write_text(json.dumps(overlay), encoding="utf-8")


def test_validate_reports_overlay_records_that_stay_out(tmp_path):
    # Memo R6: conflicts, dangling edges and stale patches are warnings.
    graph = make_graph(tmp_path)
    _write_overlay(graph, {
        "version": 1, "nodes": {}, "deleted_nodes": {}, "deleted_edges": {},
        "patches": {"gone": {"owner": "core"}},
        "edges": {
            "e1": {"source": "a", "target": "b", "relation": "references"},
            "e2": {"source": "a", "target": "gone", "relation": "references"},
        },
    })
    store = KnowledgeBase(graph)
    stats = store.overlay_stats
    assert stats["conflict_edges"] == ["e1"] and stats["dangling_edges"] == ["e2"]
    assert stats["stale_patch_nodes"] == ["gone"] and stats["conflicts"] == 1
    by_code = store.validate()["by_code"]
    assert by_code["overlay_conflict"] == 1
    assert by_code["stale_overlay_edge"] == 1 and by_code["stale_overlay_patch"] == 1
    assert store.delete_edge("e2")["overlay_only"] is True
    store.delete_node("gone")
    by_code = store.validate()["by_code"]
    assert "stale_overlay_edge" not in by_code and "stale_overlay_patch" not in by_code


def test_validate_runs_once_per_version(kb, monkeypatch):
    # Memo R7: the full check is cached by version.
    import graphify.kb as kb_module

    calls = []
    real = kb_module.validate_graph
    monkeypatch.setattr(kb_module, "validate_graph", lambda *a, **k: calls.append(1) or real(*a, **k))
    first = kb.validate()
    assert kb.validate(limit=1)["total"] == first["total"]
    assert len(calls) == 1
    kb.add_node("New", cls="concept")
    kb.validate()
    assert len(calls) == 2


def test_add_edge_checks_provenance(kb):
    with pytest.raises(KBError):
        kb.add_edge("b", "r", "references", attrs={"confidence_score": 1.5})
    kb.add_edge("b", "r", "references", attrs={"confidence_score": 0.4, "source_file": "notes.md"})
    edge = next(e for e in kb.data["links"] if {e["source"], e["target"]} == {"b", "r"})
    assert edge["confidence_score"] == 0.4 and edge["source_file"] == "notes.md"


def test_new_kb_nodes_get_the_smallest_tied_community(tmp_path):
    # Label propagation (Raghavan et al. 2007), ties go to the smallest id.
    graph = make_graph(tmp_path)
    data = json.loads(graph.read_text())
    for node, cid in zip(data["nodes"], (3, 1, 2)):
        node["community"] = cid
    graph.write_text(json.dumps(data))
    store = KnowledgeBase(graph)
    lone = store.add_node("Lone", cls="concept")
    mid = store.add_node("Middle", cls="concept")
    chain = store.add_node("Chain", cls="concept")
    store.add_edge(mid["id"], "a", "references")
    store.add_edge(mid["id"], "b", "references")
    store.add_edge(chain["id"], mid["id"], "references")
    store.flush()
    nodes = {n["id"]: n for n in json.loads(graph.read_text())["nodes"]}
    assert nodes[mid["id"]]["community"] == 1
    assert nodes[chain["id"]]["community"] == 1
    assert nodes[lone["id"]].get("community") is None

def test_static_html_embeds_data_safely(kb, tmp_path):
    kb.create_document("Evil", body="</script><script>alert(1)</script>")
    page = render_static_html(kb, name="Demo")
    assert '<script id="kb-data" type="application/json">' in page
    assert "</script><script>alert(1)" not in page
    target = write_static_html(kb)
    assert target.name == "kb.html" and target.parent == kb.out_dir


# Review 2, item 4: a patch keeps its anchor (source_file, label, class), so it
# follows the node when graphify gives the node a new id.
def _overlay(**parts):
    overlay = {"version": 1, "nodes": {}, "patches": {}, "deleted_nodes": {}, "edges": {}, "deleted_edges": {}}
    overlay.update(parts)
    return overlay


def _fn(nid, label, source_file):
    return {"id": nid, "label": label, "file_type": "code", "source_file": source_file, "_callable": True, "_origin": "ast"}


def _anchored(nid, node, **attrs):
    return {nid: {**attrs, PATCH_ANCHOR: patch_anchor(node)}}


def test_patch_follows_a_file_rename():
    old = _fn("old_run", "run()", "old.py")
    data = {"nodes": [_fn("new_run", "run()", "new.py"), _fn("new_stop", "stop()", "new.py")], "links": []}
    overlay = _overlay(patches=_anchored("old_run", old, owner="team"))
    stats = apply_overlay(data, overlay)
    assert data["nodes"][0]["owner"] == "team" and PATCH_ANCHOR not in data["nodes"][0]
    assert stats["rekeyed_patches"] == [{"from": "old_run", "to": "new_run"}]
    assert stats["stale_patches"] == 0 and list(overlay["patches"]) == ["new_run"]


def test_patch_follows_an_id_scheme_change():
    old = _fn("app_run", "run()", "app.py")
    data = {"nodes": [_fn("app.py::run", "run()", "app.py"), _fn("lib.py::run", "run()", "lib.py")], "links": []}
    stats = apply_overlay(data, _overlay(patches=_anchored("app_run", old, owner="team")))
    # The same file decides first, so the run() in lib.py does not compete.
    assert stats["rekeyed_patches"] == [{"from": "app_run", "to": "app.py::run"}]
    assert data["nodes"][0]["owner"] == "team" and "owner" not in data["nodes"][1]


def test_ambiguous_patch_stays_stale():
    old = _fn("old_run", "run()", "old.py")
    data = {"nodes": [_fn("a_run", "run()", "a.py"), _fn("b_run", "run()", "b.py")], "links": []}
    overlay = _overlay(patches=_anchored("old_run", old, owner="team"))
    stats = apply_overlay(data, overlay)
    assert stats["stale_patch_nodes"] == ["old_run"] and stats["rekeyed_patches"] == []
    assert all("owner" not in n for n in data["nodes"]) and list(overlay["patches"]) == ["old_run"]


def test_patch_of_a_deleted_symbol_stays_stale_while_its_file_lives():
    # Leader follow-up X3: parse() left a.py, but a.py still has nodes.  The
    # only other parse() is in z.py: that is no rename, so no rekey.
    old = _fn("a_parse", "parse()", "a.py")
    data = {"nodes": [_fn("a_load", "load()", "a.py"), _fn("z_parse", "parse()", "z.py")], "links": []}
    overlay = _overlay(patches=_anchored("a_parse", old, owner="team"))
    stats = apply_overlay(data, overlay)
    assert stats["stale_patch_nodes"] == ["a_parse"] and stats["rekeyed_patches"] == []
    assert all("owner" not in n for n in data["nodes"])


def test_old_patch_gets_an_anchor_from_its_node():
    data = {"nodes": [_fn("run", "run()", "app.py"), _fn("util", "util2()", "app.py")], "links": []}
    overlay = _overlay(patches={"run": {"owner": "team"}, "util": {"label": "util2()"}})
    stats = apply_overlay(data, overlay)
    assert stats["patches_anchored"] == 1
    assert overlay["patches"]["run"][PATCH_ANCHOR] == {"source_file": "app.py", "label": "run()", "class": "code.function"}
    # The node holds the patched label already, so its extracted label is unknown.
    assert PATCH_ANCHOR not in overlay["patches"]["util"]


def test_kb_rekeys_a_patch_after_a_rename_and_keeps_it(tmp_path):
    graph = make_graph(tmp_path)
    store = KnowledgeBase(graph)
    store.update_node("a", {"owner": "team"})
    assert store.overlay["patches"]["a"][PATCH_ANCHOR] == {"source_file": "a.py", "label": "Alpha", "class": "code"}
    data = json.loads(graph.read_text(encoding="utf-8"))
    for node in data["nodes"]:
        if node["id"] == "a":
            node.update({"id": "src_a", "source_file": "src/a.py"})
        elif node["source_file"] == "a.py":
            node["source_file"] = "src/a.py"  # a rename moves every node of the file
    data["links"] = [x for x in data["links"] if "a" not in (x["source"], x["target"])]
    graph.write_text(json.dumps(data), encoding="utf-8")
    store.refresh_if_changed()
    assert store.get_node("src_a")["node"]["owner"] == "team"
    found = [f for f in store.validate()["findings"] if f["code"] == "rekeyed_overlay_patch"]
    assert [(f["from"], f["node"], f["severity"]) for f in found] == [("a", "src_a", "info")]
    stored = json.loads((graph.parent / "kb" / "overlay.json").read_text(encoding="utf-8"))
    assert list(stored["patches"]) == ["src_a"]


def _rebuild(graph, base, changes):
    """Write graph.json like a graphify rebuild of ``base`` after a code change.

    ``changes`` maps a node id to new attributes.  A new ``id`` moves the
    node, and the rebuild extracts its edges with that id.
    """
    data = json.loads(json.dumps(base))
    ids = {old: attrs["id"] for old, attrs in changes.items() if "id" in attrs}
    for node in data["nodes"]:
        node.update(changes.get(node["id"], {}))
    for link in data["links"]:
        link["source"] = ids.get(link["source"], link["source"])
        link["target"] = ids.get(link["target"], link["target"])
    graph.write_text(json.dumps(data), encoding="utf-8")


def test_kb_edge_follows_a_file_rename(tmp_path):
    # A KB edge to a code node waited for the old id after a file rename
    # (stale_overlay_edge).  The anchor of its code end now finds the node.
    graph = make_graph(tmp_path)
    base = json.loads(graph.read_text(encoding="utf-8"))
    store = KnowledgeBase(graph)
    lease = store.add_node("Lease", cls="concept")["id"]
    store.add_edge(lease, "a", "references")
    old_eid, new_eid = edge_id(lease, "a", "references"), edge_id(lease, "src_a", "references")
    assert store.overlay["edges"][old_eid][EDGE_ANCHOR] == {
        "target": {"source_file": "a.py", "label": "Alpha", "class": "code"}
    }
    _rebuild(graph, base, {"a": {"id": "src_a", "source_file": "src/a.py"}, "r": {"source_file": "src/a.py"}})
    store.refresh_if_changed()
    assert any(x["source"] == lease and x["target"] == "src_a" for x in store.data["links"])
    found = [f for f in store.validate()["findings"] if f["code"] in ("rekeyed_overlay_edge", "stale_overlay_edge")]
    assert [(f["code"], f["from"], f["edge"]["id"]) for f in found] == [("rekeyed_overlay_edge", old_eid, new_eid)]
    stored = json.loads((graph.parent / "kb" / "overlay.json").read_text(encoding="utf-8"))
    assert list(stored["edges"]) == [new_eid]
    assert stored["edges"][new_eid][EDGE_ANCHOR]["target"]["source_file"] == "src/a.py"
    store.flush()
    assert not any(EDGE_ANCHOR in x for x in json.loads(graph.read_text(encoding="utf-8"))["links"])


def test_move_node_waits_for_the_rebuild_and_moves_every_record(tmp_path):
    graph = make_graph(tmp_path)
    base = json.loads(graph.read_text(encoding="utf-8"))
    store = KnowledgeBase(graph)
    lease = store.add_node("Lease", cls="concept")["id"]
    store.add_edge(lease, "a", "references")
    store.update_node("a", {"owner": "team"})
    store.update_edge(edge_id("a", "b", "calls"), {"note": "hot path"})
    store.delete_edge(edge_id("r", "a", "rationale_for"))
    assert store.batch([{"op": "move_node", "from": "a", "to": "a_v2"}]) == [
        {"from": "a", "to": "a_v2", "status": "pending"}
    ]
    assert "pending_overlay_move" in {f["code"] for f in store.validate()["findings"]}
    # A rename changes the label too, so no anchor finds the node.
    _rebuild(graph, base, {"a": {"id": "a_v2", "label": "AlphaV2"}})
    store.refresh_if_changed()
    assert store.get_node("a_v2")["node"]["owner"] == "team"
    links = {(x["source"], x["target"], x["relation"]): x for x in store.data["links"]}
    assert (lease, "a_v2", "references") in links
    assert links[("a_v2", "b", "calls")]["note"] == "hot path"
    assert ("r", "a_v2", "rationale_for") not in links
    codes = {f["code"] for f in store.validate()["findings"]}
    assert "moved_overlay_node" in codes and "pending_overlay_move" not in codes
    assert not {"stale_overlay_patch", "stale_overlay_edge"} & codes
    stored = json.loads((graph.parent / "kb" / "overlay.json").read_text(encoding="utf-8"))
    assert stored["moved_nodes"] == {} and list(stored["patches"]) == ["a_v2"]
    assert stored["patches"]["a_v2"][PATCH_ANCHOR]["label"] == "AlphaV2"
    assert edge_id("r", "a_v2", "rationale_for") in stored["deleted_edges"]


def test_move_node_moves_the_records_at_once_after_the_rebuild(tmp_path):
    graph = make_graph(tmp_path)
    base = json.loads(graph.read_text(encoding="utf-8"))
    store = KnowledgeBase(graph)
    lease = store.add_node("Lease", cls="concept")["id"]
    store.add_edge(lease, "a", "references")
    store.update_node("a", {"owner": "team"})
    _rebuild(graph, base, {"a": {"id": "a_v2", "label": "AlphaV2"}})
    store.refresh_if_changed()
    assert {"stale_overlay_patch", "stale_overlay_edge"} <= {f["code"] for f in store.validate()["findings"]}
    assert store.batch([{"op": "move_node", "from": "a", "to": "a_v2"}]) == [
        {"from": "a", "to": "a_v2", "status": "moved"}
    ]
    assert store.get_node("a_v2")["node"]["owner"] == "team"
    assert any(x["source"] == lease and x["target"] == "a_v2" for x in store.data["links"])
    assert store.overlay["moved_nodes"] == {}
    # The code moved, so an undo keeps the records at the new id.
    store.undo()
    assert store.get_node("a_v2")["node"]["owner"] == "team"


def test_move_node_undo_skip_and_refusals(kb):
    assert kb.batch([{"op": "move_node", "from": "a", "to": "a_v2"}])[0]["status"] == "pending"
    kb.undo()
    assert kb.overlay["moved_nodes"] == {}
    entries = len(kb.history)
    assert kb.move_node("gone", "elsewhere")["status"] == "skipped"
    # The graph has the new id and no record names the old id: nothing follows.
    assert kb.move_node("gone", "a")["status"] == "skipped"
    assert len(kb.history) == entries and kb.overlay["moved_nodes"] == {}
    lease = kb.add_node("Lease", cls="concept")["id"]
    with pytest.raises(KBError, match="knowledge-base node"):
        kb.move_node(lease, "a")
    with pytest.raises(KBError, match="same node"):
        kb.move_node("a", "a")


def test_moves_follow_a_chain_and_drop_a_revert():
    # Two renames before a rebuild: a -> b -> c.  The graph has c.
    data = {"nodes": [_fn("c", "c()", "app.py")], "links": []}
    overlay = _overlay(patches={"a": {"owner": "team"}}, moved_nodes={"a": "b", "b": "c", "x": "y"})
    stats = apply_overlay(data, overlay)
    assert data["nodes"][0]["owner"] == "team" and list(overlay["patches"]) == ["c"]
    assert stats["applied_moves"] == [{"from": "a", "to": "c"}, {"from": "b", "to": "c"}]
    # No record of x and no move to x, so nothing can follow its move.
    assert overlay["moved_nodes"] == {} and stats["overlay_changed"]
    # A rename and its revert before a rebuild: the graph has a again.
    data = {"nodes": [_fn("a", "a()", "app.py")], "links": []}
    overlay = _overlay(patches={"a": {"owner": "team"}}, moved_nodes={"a": "b", "b": "a"})
    apply_overlay(data, overlay)
    assert overlay["moved_nodes"] == {} and data["nodes"][0]["owner"] == "team"


@pytest.mark.parametrize("pending", [True, False], ids=["before-rebuild", "after-rebuild"])
def test_documents_follow_a_moved_node(tmp_path, pending):
    # A document kept the old id after a move.  Its next change then dropped
    # the mentions edge that had moved to the new id.
    graph = make_graph(tmp_path)
    base = json.loads(graph.read_text(encoding="utf-8"))
    store = KnowledgeBase(graph)
    doc = store.create_document("Notes", nodes=["a"])
    move = [{"op": "move_node", "from": "a", "to": "a_v2"}]
    if pending:
        assert store.batch(move)[0]["status"] == "pending"
    _rebuild(graph, base, {"a": {"id": "a_v2", "label": "AlphaV2"}})
    store.refresh_if_changed()
    if not pending:
        assert store.batch(move)[0]["status"] == "moved"
    assert store.get_document(doc["id"])["nodes"] == ["a_v2"]
    store.update_document(doc["id"], tags=["notes"])
    assert edge_id(doc["node_id"], "a_v2", "mentions") in store.overlay["edges"]


def test_documents_follow_a_file_rename(tmp_path):
    # The anchor of the mentions edge finds the node, and the document follows the edge.
    graph = make_graph(tmp_path)
    base = json.loads(graph.read_text(encoding="utf-8"))
    store = KnowledgeBase(graph)
    doc = store.create_document("Notes", nodes=["a"])["id"]
    _rebuild(graph, base, {"a": {"id": "src_a", "source_file": "src/a.py"}, "r": {"source_file": "src/a.py"}})
    store.refresh_if_changed()
    assert store.get_document(doc)["linked"] == [{"id": "src_a", "label": "Alpha", "exists": True}]


def test_move_document_ids_moves_every_node_id_of_a_document():
    meta = {
        "nodes": ["a", "b"], "wiki_nodes": ["c"], "auto_nodes": ["x", "b"], "llm_nodes": [],
        "auto_scores": {"x": 0.4, "b": 0.9, "a": 0.7},
        "decisions": {"alpha": "a", "noise": None},
        "possible": [{"mention": "Alpha", "candidates": [
            {"id": "a", "label": "Alpha", "source_file": "a.py"}, {"id": "q", "label": "Q"}]}],
    }
    nodes = {"a2": {"label": "AlphaV2", "source_file": "src/a.py"}}
    assert move_document_ids(meta, {"a": "a2", "x": "b"}, nodes)
    assert (meta["nodes"], meta["wiki_nodes"], meta["auto_nodes"]) == (["a2", "b"], ["c"], ["b"])
    assert meta["auto_scores"] == {"b": 0.9, "a2": 0.7}  # b keeps its own score
    assert meta["decisions"] == {"alpha": "a2", "noise": None}
    assert meta["possible"][0]["candidates"] == [
        {"id": "a2", "label": "AlphaV2", "source_file": "src/a.py"}, {"id": "q", "label": "Q"}]
    assert not move_document_ids(meta, {"gone": "z"}, nodes)


def test_undo_uses_the_footprint_from_write_time(tmp_path):
    # Review 2, item 7: change 2 adds an edge to node b.  A rebuild then
    # removes b, so the edge is only in the overlay.  Undo of change 1 must
    # still see that change 2 needs node kb:lease.
    graph = make_graph(tmp_path)
    store = KnowledgeBase(graph)
    lease = store.add_node("Lease", cls="concept")
    store.add_edge(lease["id"], "b", "references")
    second = store.history[-1]
    assert f"n:{lease['id']}" in second["keys"]["r"]
    data = json.loads(graph.read_text(encoding="utf-8"))
    data["nodes"] = [n for n in data["nodes"] if n["id"] != "b"]
    data["links"] = [x for x in data["links"] if "b" not in (x["source"], x["target"])]
    graph.write_text(json.dumps(data), encoding="utf-8")
    store.refresh_if_changed()
    assert not store._edges.get(second["undo"][0][1])  # the edge waits in the overlay
    with pytest.raises(UndoConflict) as err:
        store.undo(seq=store.history[0]["seq"])
    assert err.value.later == [second["seq"]]
    assert store.undo()["undone"] == second["seq"]
    assert store.undo()["undone"] == store.history[0]["seq"]
    assert store.overlay["edges"] == {} and store.overlay["nodes"] == {}


def test_undo_refuses_to_bring_back_a_removed_extracted_node(tmp_path):
    graph = make_graph(tmp_path)
    store = KnowledgeBase(graph)
    store.update_node("b", {"owner": "core"})
    data = json.loads(graph.read_text(encoding="utf-8"))
    data["nodes"] = [n for n in data["nodes"] if n["id"] != "b"]
    data["links"] = [x for x in data["links"] if "b" not in (x["source"], x["target"])]
    graph.write_text(json.dumps(data), encoding="utf-8")
    store.refresh_if_changed()
    with pytest.raises(Conflict, match="no node b"):
        store.undo()
    assert "b" not in store._nodes


def test_find_node_uses_the_link_key_and_prefers_kb_nodes(kb):
    # Review 2, item 8: one match key with the linker, through the cached index.
    assert kb.find_node("alpha()")["id"] == "a"
    assert kb.find_node("  ALPHA ")["id"] == "a"
    assert kb.find_node("why alpha")["id"] == "r"  # not linkable, still found
    assert kb.find_node("nothing here") is None and kb.find_node("") is None
    mine = kb.add_node("Alpha", cls="concept")
    assert kb.find_node("Alpha")["id"] == mine["id"]
    with kb._tx("batch", "probe", None):
        made = kb.add_node("Fresh idea", cls="concept")
        assert kb.find_node("fresh idea")["id"] == made["id"]


def test_a_real_file_named_kb_keeps_its_nodes(tmp_path):
    # Review 2, item 9: KB records use source_file "kb", but every guard keys
    # on _origin.  A real top-level file named kb keeps its extracted nodes.
    from graphify.cli import _stale_graph_sources
    from graphify.detect import detect
    from graphify.watch import _rebuild_code

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    script = corpus / "kb"
    script.write_text("#!/usr/bin/env bash\nhelper() {\n  echo hi\n}\nhelper\n", encoding="utf-8")
    (corpus / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    assert _rebuild_code(corpus, acquire_lock=False) is True
    graph = corpus / "graphify-out" / "graph.json"

    def extracted():
        nodes = json.loads(graph.read_text(encoding="utf-8"))["nodes"]
        return {n["id"] for n in nodes if n.get("source_file") == "kb" and n.get("_origin") == "ast"}

    code_ids = extracted()
    assert code_ids
    helper = next(i for i in code_ids if i.endswith("helper"))
    store = KnowledgeBase(graph)
    idea = store.add_node("Helper idea", cls="concept")
    assert store.get_node(idea["id"])["node"]["source_file"] == "kb"
    store.add_edge(idea["id"], helper, "describes")
    store.update_node(helper, {"owner": "ops"})
    store.flush()
    script.write_text(script.read_text(encoding="utf-8") + "other() {\n  echo bye\n}\n", encoding="utf-8")
    _rebuild_code(corpus, acquire_lock=False)
    assert code_ids <= extracted()
    detection = detect(corpus)
    seen = {f for files in detection["files"].values() for f in files}
    assert _stale_graph_sources(graph, corpus, seen, detection=detection) == []
    store.refresh_if_changed()
    assert store.get_node(helper)["node"]["owner"] == "ops"
    store.undo()  # the owner patch
    store.undo()  # the edge
    store.undo()  # the KB node
    store.flush()
    assert code_ids <= extracted() and idea["id"] not in store._nodes


def test_same_name_asset_gets_a_new_name(kb):
    # E2E report 2, N6: a second upload with the same name never replaces the first.
    doc = kb.create_document("Pics", body="Pictures.")
    first = kb.add_asset(doc["id"], "same.png", b"one")
    second = kb.add_asset(doc["id"], "../../same.png", b"two")
    third = kb.add_asset(doc["id"], "/etc/same.png", b"three")
    assert [first["name"], second["name"], third["name"]] == ["same.png", "same-2.png", "same-3.png"]
    assert kb.asset_path(doc["id"], "same.png").read_bytes() == b"one"
    assert kb.asset_path(doc["id"], "same-2.png").read_bytes() == b"two"
    kb.undo()
    with pytest.raises(NotFound):
        kb.asset_path(doc["id"], "same-3.png")
    names = [a["name"] for a in kb.get_document(doc["id"])["assets"]]
    assert names == ["same.png", "same-2.png"]


def test_flush_puts_the_new_document_into_the_view_community(tmp_path):
    # Report 2 (#8161): the cached view kept community -1 after the flush.
    path = make_graph(tmp_path)
    graph = json.loads(path.read_text())
    for node in graph["nodes"]:
        node["community"], node["community_name"] = 3, "Core"
    path.write_text(json.dumps(graph))
    kb = KnowledgeBase(path, autoflush=False)
    doc = kb.create_document("Notes", body="See [[Alpha]] and [[Beta]].")
    rows = {r[0]: r for r in kb.graph_view()["nodes"]}
    assert rows[doc["node_id"]][3] == -1
    before = kb.version
    assert kb.flush()
    assert kb.version > before
    rows = {r[0]: r for r in kb.graph_view()["nodes"]}
    assert rows[doc["node_id"]][3] == 3


def test_node_detail_rows_name_the_other_source_file(kb):
    # Report 2 (#8161): rows of nodes with the same label need their file.
    detail = kb.get_node("a")
    assert detail["out"][0]["other_source_file"] == "b.py"
    assert detail["in"][0]["other_source_file"] == "a.py"


def test_search_gives_one_hit_for_each_document(kb):
    # Report 2 (#8161): the document row and its doc: node were two hits.
    doc = kb.create_document("Lease notes", body="How the lease works.")
    hits = [h for h in kb.search("lease notes") if h["label"] == "Lease notes"]
    assert len(hits) == 1
    assert hits[0]["kind"] == "document" and hits[0]["node"] == doc["node_id"]


def test_llm_ingest_sends_every_chunk(tmp_path, monkeypatch):
    # B-ingest #8159: a long body went to the LLM as chunk 1 only.
    from graphify import kb_ingest

    def answer(phrase):
        return {
            "entities": [{"name": phrase, "class": "concept"}],
            "triples": [{
                "subject": "THIS_DOCUMENT", "relation": "describes", "object": phrase,
                "evidence": f"This document describes the {phrase}.",
            }],
        }

    prompts = []

    def fake(prompt, **_):
        prompts.append(prompt)
        return answer("lease policy" if "the lease policy" in prompt else "refund policy")

    monkeypatch.setattr(kb_ingest, "resolve_backend", lambda backend: "fake")
    monkeypatch.setattr(kb_ingest, "call_llm", fake)
    filler = "\n\n".join("Plain filler text without facts. " * 20 for _ in range(120))
    body = f"# Refunds\n\nThis document describes the refund policy.\n\n{filler}\n\n# Leases\n\nThis document describes the lease policy.\n"
    assert len(body) > kb_ingest.MAX_PROMPT_CHARS
    kb = KnowledgeBase(make_graph(tmp_path))
    res = kb.ingest("long.md", text=body, mode="llm")
    assert len(prompts) >= 2 and res["llm"]["chunks"] == len(prompts)
    offsets = {}
    for link in kb.data["links"]:
        if link.get("relation") == "describes":
            offsets[kb.get_node(link["target"])["node"]["label"]] = link["chunk_offset"]
    assert offsets["refund policy"] == 0 and offsets["lease policy"] > 0


def test_relink_keeps_llm_review_items_while_the_mention_lives(tmp_path, monkeypatch):
    # #8177 X1: a re-link dropped the review items of ingest --llm.
    from graphify import kb_ingest

    path = make_graph(tmp_path)
    graph = json.loads(path.read_text())
    graph["nodes"] += [
        {"id": "g1", "label": "Gamma", "file_type": "code", "source_file": "c.py"},
        {"id": "g2", "label": "Gamma", "file_type": "code", "source_file": "d.py"},
    ]
    path.write_text(json.dumps(graph))
    triple = {"subject": "Gamma", "relation": "calls", "object": "Alpha", "evidence": "Gamma calls Alpha."}
    monkeypatch.setattr(kb_ingest, "resolve_backend", lambda backend: "fake")
    monkeypatch.setattr(kb_ingest, "call_llm", lambda prompt, **_: {"entities": [], "triples": [triple]})
    kb = KnowledgeBase(path)
    res = kb.ingest("n.md", text="# Note\n\nGamma calls Alpha.\n", mode="llm")
    doc_id = res["document"]["id"]

    def llm_mentions():
        return [p["mention"] for p in kb.get_document(doc_id)["possible"] if p.get("kind") == "llm"]

    assert llm_mentions() == ["Gamma"]
    kb.update_document(doc_id, nodes=["b"])
    assert llm_mentions() == ["Gamma"]
    kb.update_document(doc_id, body="# Note\n\nAlpha alone.\n")
    assert llm_mentions() == []


# -- review decisions (D3) ---------------------------------------------------


def _review_kb(tmp_path, **kw):
    path = make_graph(tmp_path)
    graph = json.loads(path.read_text())
    graph["nodes"] += [
        {"id": "g1", "label": "request!()", "file_type": "code", "source_file": "src/gateway/client.clj"},
        {"id": "g2", "label": "request!()", "file_type": "code", "source_file": "src/council/core.clj"},
    ]
    path.write_text(json.dumps(graph))
    kb = KnowledgeBase(path, **kw)
    doc = kb.create_document("Notes", body="The `request!` call sends jobs.", link=True)
    assert [p["mention"] for p in doc["possible"]] == ["request!"]
    return kb, doc["id"]


def _mention_edge(kb, doc_id, target):
    return kb._edges.get(edge_id(f"doc:{doc_id}", target, "mentions"))


def test_review_link_adds_an_explicit_mention_and_closes_the_item(tmp_path):
    # D3: "this mention is node N" links like an explicit node.
    kb, doc_id = _review_kb(tmp_path)
    doc = kb.review_mention(doc_id, "Request!", "g1", actor="ana")
    assert doc["possible"] == [] and doc["decisions"] == {"request!": "g1"}
    assert "g1" in doc["nodes"]
    edge = _mention_edge(kb, doc_id, "g1")
    assert edge and edge[0]["confidence"] == "EXTRACTED"
    entry = kb.history[-1]
    assert entry["summary"] == "Review Request! in Notes" and entry["actor"] == "ana"


def test_review_not_a_link_survives_every_relink(tmp_path):
    # D3: a "not a link" decision hides the item and blocks text links of that mention.
    kb, doc_id = _review_kb(tmp_path)
    assert kb.review_mention(doc_id, "request!", None)["decisions"] == {"request!": None}
    assert kb.update_document(doc_id, nodes=["a"])["possible"] == []
    assert kb.update_document(doc_id, body="The `request!` call sends more jobs.")["possible"] == []
    # With one candidate left, the linker would link the mention without the decision.
    kb.delete_node("g2")
    doc = kb.update_document(doc_id, body="The `request!` call sends all jobs.")
    assert doc["possible"] == [] and doc["auto_nodes"] == []
    assert _mention_edge(kb, doc_id, "g1") is None
    # A wiki link in the text still wins.
    doc = kb.update_document(doc_id, body="The `request!` call, see [[request!]].")
    assert "g1" in doc["wiki_nodes"] and _mention_edge(kb, doc_id, "g1")


def test_undo_of_a_review_restores_the_item_and_the_edges(tmp_path):
    kb, doc_id = _review_kb(tmp_path)
    kb.review_mention(doc_id, "request!", "g1")
    kb.undo()
    doc = kb.get_document(doc_id)
    assert [p["mention"] for p in doc["possible"]] == ["request!"]
    assert doc["decisions"] == {} and "g1" not in doc["nodes"]
    assert _mention_edge(kb, doc_id, "g1") is None


def test_review_errors(tmp_path):
    kb, doc_id = _review_kb(tmp_path)
    with pytest.raises(KBError) as err:
        kb.review_mention(doc_id, "Alpha", "a")
    assert err.value.status == 400
    with pytest.raises(KBError):
        kb.review_mention(doc_id, "  ", None)
    with pytest.raises(NotFound):
        kb.review_mention(doc_id, "request!", "nope")
    with pytest.raises(NotFound):
        kb.review_mention("no-such-doc", "request!", None)
    # A decided mention has no open item any more.
    kb.review_mention(doc_id, "request!", None)
    with pytest.raises(KBError):
        kb.review_mention(doc_id, "request!", "g1")


def test_document_list_counts_open_review_items(tmp_path):
    kb, doc_id = _review_kb(tmp_path)
    assert [d["review"] for d in kb.list_documents()] == [1]
    kb.review_mention(doc_id, "request!", "g2")
    assert [d["review"] for d in kb.list_documents()] == [0]


def test_review_decides_an_llm_item(tmp_path, monkeypatch):
    # D3: an LLM possible_match item is decided the same way.
    from graphify import kb_ingest

    path = make_graph(tmp_path)
    graph = json.loads(path.read_text())
    graph["nodes"] += [
        {"id": "g1", "label": "Gamma", "file_type": "code", "source_file": "c.py"},
        {"id": "g2", "label": "Gamma", "file_type": "code", "source_file": "d.py"},
    ]
    path.write_text(json.dumps(graph))
    triple = {"subject": "Gamma", "relation": "calls", "object": "Alpha", "evidence": "Gamma calls Alpha."}
    monkeypatch.setattr(kb_ingest, "resolve_backend", lambda backend: "fake")
    monkeypatch.setattr(kb_ingest, "call_llm", lambda prompt, **_: {"entities": [], "triples": [triple]})
    kb = KnowledgeBase(path)
    doc_id = kb.ingest("n.md", text="# Note\n\nGamma calls Alpha.\n", mode="llm")["document"]["id"]
    doc = kb.review_mention(doc_id, "gamma", "g2")
    assert [p for p in doc["possible"] if p.get("kind") == "llm"] == []
    edge = _mention_edge(kb, doc_id, "g2")
    assert edge and edge[0]["confidence"] == "EXTRACTED"
    assert kb.update_document(doc_id, body="# Note\n\nGamma calls Alpha again.\n")["possible"] == []


def test_link_decision_ends_when_its_node_leaves_the_explicit_nodes(tmp_path):
    # #8211: a link decision counts only while its node is an explicit node.
    kb, doc_id = _review_kb(tmp_path)
    kb.review_mention(doc_id, "request!", "g1")
    doc = kb.update_document(doc_id, nodes=[])
    assert doc["decisions"] == {}
    assert [p["mention"] for p in doc["possible"]] == ["request!"]
    kb.undo()
    doc = kb.get_document(doc_id)
    assert doc["decisions"] == {"request!": "g1"} and doc["possible"] == []
    assert doc["nodes"] == ["g1"]


def test_not_a_link_decision_stays_when_the_nodes_change(tmp_path):
    kb, doc_id = _review_kb(tmp_path)
    kb.review_mention(doc_id, "request!", None)
    assert kb.update_document(doc_id, nodes=["a"])["decisions"] == {"request!": None}


# -- one decision for each name, unlink (#8229) ------------------------------


def _names_kb(tmp_path):
    path = make_graph(tmp_path)
    graph = json.loads(path.read_text())

    def code(nid, label, src):
        return {"id": nid, "label": label, "file_type": "code", "source_file": src}

    graph["nodes"] += [
        code("cf", "client.clj", "src/tui/client.clj"),
        code("ci", "init!()", "src/tui/client.clj"),
        code("sf", "state.clj", "src/tui/state.clj"),
        code("si", "init!()", "src/tui/state.clj"),
        code("sr", "run!()", "src/tui/state.clj"),
        code("ss", "shutdown!()", "src/tui/state.clj"),
        code("rf", "relay.clj", "src/gw/relay.clj"),
    ]
    pairs = [("cf", "ci"), ("sf", "si"), ("sf", "sr"), ("sf", "ss"), ("si", "sr")]
    graph["links"] += [{"source": s, "target": t, "relation": "contains"} for s, t in pairs]
    path.write_text(json.dumps(graph))
    return KnowledgeBase(path)


def _doc_targets(kb, doc_id):
    return sorted(str(x["target"]) for x in kb._out.get(f"doc:{doc_id}", []) if x["relation"] == "mentions")


def test_a_link_decision_stops_every_other_text_link_of_the_name(tmp_path):
    # #8229 item 1: after Link of init! to client.clj, the re-link also linked
    # the init!() of state.clj, so one name had two links.
    kb = _names_kb(tmp_path)
    doc_id = kb.create_document("Notes", body="Call `init!` first.", link=True)["id"]
    kb.review_mention(doc_id, "init!", "ci")
    doc = kb.update_document(doc_id, body="Call `init!` first, then `run!` and `shutdown!`.")
    # The decision is context for the other names: run! and shutdown! link.
    assert _doc_targets(kb, doc_id) == ["ci", "sr", "ss"]
    assert doc["possible"] == []
    kb.update_document(doc_id, nodes=["ci", "a"])
    assert "si" not in _doc_targets(kb, doc_id)
    # When the decided node goes, the linker decides the name again.
    kb.update_document(doc_id, nodes=["a"])
    assert kb.get_document(doc_id)["decisions"] == {}
    assert "si" in _doc_targets(kb, doc_id)


def test_unlink_of_an_explicit_node(tmp_path):
    # #8229 item 2 (N4): the edge delete also changes the document, in one change.
    kb = _names_kb(tmp_path)
    doc_id = kb.create_document("Notes", body="Some text.", nodes=["b"])["id"]
    eid = edge_id(f"doc:{doc_id}", "b", "mentions")
    assert kb.delete_edge(eid, actor="ana") == {"deleted": eid, "document": doc_id}
    doc = kb.get_document(doc_id)
    assert doc["nodes"] == [] and _doc_targets(kb, doc_id) == []
    assert kb.history[-1]["summary"] == "Unlink Beta from Notes" and kb.history[-1]["actor"] == "ana"
    kb.update_document(doc_id, body="Some more text.")
    assert _doc_targets(kb, doc_id) == []
    kb.undo()
    kb.undo()
    assert kb.get_document(doc_id)["nodes"] == ["b"] and _doc_targets(kb, doc_id) == ["b"]


def test_unlink_of_a_text_link_stores_not_a_link(tmp_path):
    kb = _names_kb(tmp_path)
    doc_id = kb.create_document("Notes", body="Then `run!` loops.", link=True)["id"]
    assert _doc_targets(kb, doc_id) == ["sr"]
    kb.batch([{"op": "delete_edge", "id": edge_id(f"doc:{doc_id}", "sr", "mentions")}])
    doc = kb.get_document(doc_id)
    assert doc["decisions"] == {"run!": None} and _doc_targets(kb, doc_id) == []
    kb.update_document(doc_id, body="Then `run!` loops again.")
    assert _doc_targets(kb, doc_id) == []
    kb.undo()
    kb.undo()
    doc = kb.get_document(doc_id)
    assert doc["decisions"] == {} and _doc_targets(kb, doc_id) == ["sr"]


def test_unlink_of_a_same_file_link(tmp_path):
    # The same-file link has the file name as its mention.
    kb = _names_kb(tmp_path)
    doc_id = kb.create_document("relay.clj", body="```clojure\n(defn go [] 1)\n```\n", link=True)["id"]
    assert _doc_targets(kb, doc_id) == ["rf"]
    kb.delete_edge(edge_id(f"doc:{doc_id}", "rf", "mentions"))
    assert kb.get_document(doc_id)["decisions"] == {"relay.clj": None} and _doc_targets(kb, doc_id) == []
    kb.update_document(doc_id, body="```clojure\n(defn go [] 2)\n```\n")
    assert _doc_targets(kb, doc_id) == []


def test_unlink_of_a_wiki_link_is_a_conflict(tmp_path):
    kb = _names_kb(tmp_path)
    doc_id = kb.create_document("Notes", body="See [[Alpha]].")["id"]
    before = len(kb.history)
    with pytest.raises(Conflict) as err:
        kb.delete_edge(edge_id(f"doc:{doc_id}", "a", "mentions"))
    assert err.value.status == 409
    assert str(err.value) == "the text links it with [[Alpha]]; edit the text"
    assert len(kb.history) == before and _doc_targets(kb, doc_id) == ["a"]


def test_retitle_of_a_code_document_relinks(tmp_path):
    # #8229 item 3 (N3): the title names the file of a pasted code block.
    kb = _names_kb(tmp_path)
    doc_id = kb.create_document("Paste", body="```clojure\n(defn go [] 1)\n```\n", link=True)["id"]
    assert _doc_targets(kb, doc_id) == []
    kb.update_document(doc_id, title="relay.clj")
    assert _doc_targets(kb, doc_id) == ["rf"]


def test_batch_add_document_links_by_default(kb):
    # #8239: a batch add_document op links like POST /api/documents.
    linked, plain = kb.batch([
        {"op": "add_document", "title": "One", "body": "The `Alpha` call."},
        {"op": "add_document", "title": "Two", "body": "The `Alpha` call.", "link": False},
    ])
    assert linked["link"] is True and "a" in linked["auto_nodes"]
    assert plain["link"] is False and plain["auto_nodes"] == []


def test_compact_graph_write_is_the_text_of_json_dumps(tmp_path):
    # #8264: the flush writes graph.json with the C encoder, in list slices.
    from graphify import kb as kb_module

    size = kb_module._JSON_SLICE * 2 + 7
    data = {
        "directed": True,
        "graph": {"name": "Zażółć gęślą jaźń"},
        "nodes": [{"id": f"n{i}", "label": f"Węzeł {i}", "w": i / 3, "tags": [None, True]} for i in range(size)],
        "links": [],
        "hyperedges": [[1, 2]] * (kb_module._JSON_SLICE + 1),
    }
    out = tmp_path / "graph.json"
    for value in (data, [1, {"a": "b"}], {1: "x", "k": [0] * (kb_module._JSON_SLICE + 1)}):
        kb_module._dump_atomic(out, value, compact=True)
        assert out.read_text(encoding="utf-8") == json.dumps(value, ensure_ascii=False, separators=(",", ":"))
