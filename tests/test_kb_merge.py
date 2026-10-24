"""One graph: knowledge-base (KB) records live in graphify's own graph.json.

``graphify update`` and the incremental extract rebuilt graph.json from the
scan alone. They removed every KB node and edge until the next
``KnowledgeBase`` load (KB research memo F7, E2E report bug 1). These tests pin
the merge: graphify's writers apply ``graphify-out/kb/overlay.json``, and the
stale-source prune skips KB records.
"""
from __future__ import annotations

import json

import pytest

from graphify.build import build_merge
from graphify.cli import _stale_graph_sources
from graphify.detect import detect
from graphify.kb import (
    Conflict,
    KBError,
    KnowledgeBase,
    apply_overlay,
    edge_id,
    is_kb_record,
    read_overlay,
)

LEASE = {"label": "Lease policy", "file_type": "concept", "source_file": "kb", "_origin": "kb"}


def _overlay(**parts):
    overlay = {
        "version": 1,
        "nodes": {},
        "patches": {},
        "deleted_nodes": {},
        "edges": {},
        "deleted_edges": {},
    }
    overlay.update(parts)
    return overlay


def _code_node(nid, label):
    return {"id": nid, "label": label, "file_type": "code", "source_file": "app.py", "_origin": "ast"}


def _code_graph():
    return {
        "directed": False,
        "nodes": [_code_node("app_run", "run()"), _code_node("app_util", "util()")],
        "links": [
            {
                "source": "app_run",
                "target": "app_util",
                "relation": "calls",
                "confidence": "EXTRACTED",
                "source_file": "app.py",
                "_origin": "ast",
            }
        ],
    }


def _kb_edge(source, target, relation):
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": "INFERRED",
        "_origin": "kb",
    }


def _kb_edges(*triples):
    return {edge_id(*triple): _kb_edge(*triple) for triple in triples}


def test_apply_overlay_adds_kb_records_and_keeps_the_graph_simple():
    data = _code_graph()
    overlay = _overlay(
        nodes={"kb_lease": {**LEASE, "community": 7, "norm_label": "stale"}},
        edges=_kb_edges(
            ("kb_lease", "app_run", "describes"),
            # The extracted `calls` edge already joins this pair: it wins.
            ("app_util", "app_run", "mentions"),
            ("kb_lease", "gone", "describes"),
        ),
    )
    stats = apply_overlay(data, overlay)
    assert (stats["nodes_added"], stats["edges_added"]) == (1, 1)
    assert (stats["conflicts"], stats["dangling"]) == (1, 1)
    lease = next(n for n in data["nodes"] if n["id"] == "kb_lease")
    # Clustering and export own the derived keys, not the overlay.
    assert "community" not in lease and "norm_label" not in lease
    assert sorted(link["relation"] for link in data["links"]) == ["calls", "describes"]


def test_apply_overlay_removes_stale_kb_records_and_applies_deletions():
    data = _code_graph()
    data["nodes"][0]["community"] = 3
    data["nodes"].append({**LEASE, "id": "kb_old"})
    data["links"].append(_kb_edge("kb_old", "app_run", "describes"))
    overlay = _overlay(
        deleted_nodes={"app_util": {"label": "util()"}},
        patches={"app_run": {"owner": "team", "community": 99}},
    )
    stats = apply_overlay(data, overlay)
    assert [n["id"] for n in data["nodes"]] == ["app_run"]
    assert data["links"] == []
    assert (stats["nodes_removed"], stats["edges_removed"]) == (2, 2)
    run = data["nodes"][0]
    assert run["owner"] == "team"
    assert run["community"] == 3


def test_apply_overlay_updates_a_kb_node_and_keeps_derived_keys():
    data = _code_graph()
    data["nodes"].append({**LEASE, "id": "kb_lease", "community": 4, "norm_label": "lease policy"})
    stats = apply_overlay(data, _overlay(nodes={"kb_lease": {**LEASE, "label": "Lease rules"}}))
    lease = next(n for n in data["nodes"] if n["id"] == "kb_lease")
    assert stats["nodes_updated"] == 1
    assert lease["label"] == "Lease rules"
    assert (lease["community"], lease["norm_label"]) == (4, "lease policy")


@pytest.mark.parametrize(("directed", "conflicts"), [(True, 0), (False, 1)])
def test_apply_overlay_pairs_follow_the_graph_direction(directed, conflicts):
    data = _code_graph()
    data["directed"] = directed
    stats = apply_overlay(data, _overlay(edges=_kb_edges(("app_util", "app_run", "uses"))))
    assert stats["conflicts"] == conflicts
    assert stats["edges_added"] == 1 - conflicts


def test_read_overlay(tmp_path):
    assert read_overlay(tmp_path) is None
    path = tmp_path / "kb" / "overlay.json"
    path.parent.mkdir()
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(KBError):
        read_overlay(tmp_path)
    path.write_text(json.dumps({"nodes": {"kb_lease": LEASE}, "edges": [], "junk": 1}), encoding="utf-8")
    overlay = read_overlay(tmp_path)
    assert overlay["nodes"] == {"kb_lease": LEASE}
    assert overlay["edges"] == {}
    assert "junk" not in overlay


def test_stale_sources_skip_kb_records(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    body = tmp_path / "graphify-out" / "kb" / "documents" / "x" / "body.md"
    body.parent.mkdir(parents=True)
    body.write_text("# Note\n", encoding="utf-8")
    graph_path = tmp_path / "graphify-out" / "graph.json"
    nodes = [
        {"id": "a_f", "label": "f()", "source_file": "a.py", "_origin": "ast"},
        {"id": "gone_g", "label": "g()", "source_file": "gone.py", "_origin": "ast"},
        {"id": "kb_c", "label": "Concept", "source_file": "kb", "_origin": "kb"},
        {"id": "doc_x", "label": "Note", "source_file": str(body.relative_to(tmp_path)), "_origin": "kb"},
    ]
    graph_path.write_text(json.dumps({"nodes": nodes, "links": []}), encoding="utf-8")
    detection = detect(tmp_path)
    seen = {f for files in detection["files"].values() for f in files}
    seen.update(detection.get("unclassified", []))
    assert _stale_graph_sources(graph_path, tmp_path, seen, detection=detection) == ["gone.py"]


def _graph(graph_path):
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    return {n["id"]: n for n in data["nodes"]}, data.get("links", data.get("edges", []))


def test_update_keeps_kb_records(tmp_path):
    """E2E bug 1: `graphify update` without a KB server removed KB records."""
    from graphify.watch import _rebuild_code

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    app = corpus / "app.py"
    app.write_text("def run():\n    return 1\n", encoding="utf-8")
    assert _rebuild_code(corpus, acquire_lock=False) is True
    graph_path = corpus / "graphify-out" / "graph.json"
    nodes, _ = _graph(graph_path)
    run_id = next(nid for nid, n in nodes.items() if n["label"] == "run()")

    kb = KnowledgeBase(graph_path)
    concept = kb.add_node("Lease policy", file_type="concept")
    kb.add_edge(concept["id"], run_id, "describes", confidence="INFERRED")
    kb.flush()

    app.write_text("def run():\n    return 1\n\n\ndef stop():\n    return 0\n", encoding="utf-8")
    for _ in range(2):  # a rebuild, then a run without changes
        _rebuild_code(corpus, acquire_lock=False)
        nodes, links = _graph(graph_path)
        assert "stop()" in {n["label"] for n in nodes.values()}
        lease = nodes[concept["id"]]
        assert lease["norm_label"] == "lease policy"
        assert lease.get("community") is not None
        assert any(link["relation"] == "describes" and is_kb_record(link) for link in links)


def test_build_merge_keeps_kb_records(tmp_path):
    """The incremental extract (build_merge) keeps KB records, also when a prune lists them."""
    out = tmp_path / "graphify-out"
    (out / "kb").mkdir(parents=True)
    graph_path = out / "graph.json"
    data = _code_graph()
    data["nodes"].append({**LEASE, "id": "kb_lease"})
    data["links"].append(_kb_edge("kb_lease", "app_run", "describes"))
    graph_path.write_text(json.dumps(data), encoding="utf-8")
    overlay = _overlay(nodes={"kb_lease": LEASE}, edges=_kb_edges(("kb_lease", "app_run", "describes")))
    (out / "kb" / "overlay.json").write_text(json.dumps(overlay), encoding="utf-8")
    chunk = {"nodes": _code_graph()["nodes"], "edges": _code_graph()["links"]}
    G = build_merge([chunk], graph_path, prune_sources=["app.py", "kb"], dedup=False, root=tmp_path)
    assert "kb_lease" in G
    assert G.has_edge("kb_lease", "app_run")
    assert G.number_of_edges() == 2


def test_add_edge_refuses_a_second_edge_between_joined_nodes(tmp_path):
    graph_path = tmp_path / "graphify-out" / "graph.json"
    graph_path.parent.mkdir()
    graph_path.write_text(json.dumps(_code_graph()), encoding="utf-8")
    kb = KnowledgeBase(graph_path)
    with pytest.raises(Conflict):
        kb.add_edge("app_util", "app_run", "mentions")


def test_update_drops_a_kb_node_that_the_overlay_removed(tmp_path):
    """Follow-up #8155 A: graph.json still holds a KB node that the overlay no
    longer has (another process removed it before its flush).  A code change
    then rebuilds: the stale KB node goes, the new code node comes, and the
    shrink guards do not count the KB node as a lost semantic node."""
    from graphify.watch import _rebuild_code

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    app = corpus / "app.py"
    app.write_text("def run():\n    return 1\n", encoding="utf-8")
    assert _rebuild_code(corpus, acquire_lock=False) is True
    graph_path = corpus / "graphify-out" / "graph.json"
    kb = KnowledgeBase(graph_path)
    concept = kb.add_node("Lease policy", file_type="concept")
    kb.flush()
    assert concept["id"] in _graph(graph_path)[0]

    # A written kb/overlay.json is an import: the store and the rebuild read it.
    overlay = read_overlay(graph_path.parent)
    assert overlay is not None
    del overlay["nodes"][concept["id"]]
    (graph_path.parent / "kb" / "overlay.json").write_text(json.dumps(overlay), encoding="utf-8")

    app.write_text("def run():\n    return 1\n\n\ndef stop():\n    return 0\n", encoding="utf-8")
    assert _rebuild_code(corpus, acquire_lock=False) is True
    nodes, _ = _graph(graph_path)
    assert concept["id"] not in nodes
    assert "stop()" in {n["label"] for n in nodes.values()}


def test_shrink_guards_never_count_a_kb_node_as_lost():
    # E2E report 2, N1: the overlay owns KB nodes, so the shrink guards skip them.
    from graphify.build import _lost_saved_semantic_nodes
    from graphify.watch import _check_shrink

    code = {"id": "a_f", "label": "f()", "file_type": "code", "source_file": "a.py"}
    stale = {"id": "del:me", **LEASE}
    assert _lost_saved_semantic_nodes([stale], []) == []
    assert _check_shrink(False, {"nodes": [code, stale]}, {"nodes": [code]}) is True
