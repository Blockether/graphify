"""Ontology inference and graph validation (graphify.ontology)."""
import json

from graphify.ontology import Ontology, infer_ontology, validate_graph
from tests.kb_fixtures import make_graph


def _graph(tmp_path):
    return json.loads(make_graph(tmp_path).read_text())


def test_infer_covers_classes_and_relations(tmp_path):
    onto = infer_ontology(_graph(tmp_path))
    assert set(onto.classes) == {"code", "rationale"}
    assert onto.relations["calls"].domain == ["code"]
    assert onto.relations["rationale_for"].range == ["code"]


def test_inferred_ontology_validates_its_own_graph(tmp_path):
    graph = _graph(tmp_path)
    report = validate_graph(graph, infer_ontology(graph))
    assert report["ok"] is True
    assert report["counts"]["error"] == 0
    assert report["nodes"] == 3 and report["edges"] == 2


def test_validate_reports_domain_range_and_unknowns(tmp_path):
    graph = _graph(tmp_path)
    onto = infer_ontology(graph)
    onto.strict = True
    graph["links"].append({"source": "a", "target": "r", "relation": "calls"})
    graph["links"].append({"source": "a", "target": "b", "relation": "teleports"})
    graph["nodes"].append({"id": "x", "label": "X", "file_type": "alien"})
    report = validate_graph(graph, onto)
    codes = set(report["by_code"])
    assert {"range_violation", "unknown_relation", "unknown_class"} <= codes
    assert report["ok"] is False


def test_validate_reports_missing_required_property(tmp_path):
    graph = _graph(tmp_path)
    onto = Ontology.from_dict({
        "classes": [{"name": "code", "properties": [{"name": "owner", "type": "string", "required": True}]},
                    {"name": "rationale"}],
        "relations": [{"name": "calls"}, {"name": "rationale_for"}],
    })
    report = validate_graph(graph, onto)
    assert report["by_code"].get("missing_property") == 2


def test_validate_respects_limit(tmp_path):
    graph = _graph(tmp_path)
    graph["nodes"] += [{"id": f"o{i}", "label": f"Orphan {i}", "file_type": "code"} for i in range(10)]
    report = validate_graph(graph, infer_ontology(graph), limit=3)
    assert len(report["findings"]) == 3
    assert report["truncated"] is True and report["total"] >= 10


def test_ontology_round_trips(tmp_path):
    onto = infer_ontology(_graph(tmp_path))
    again = Ontology.from_dict(onto.to_dict())
    assert again.to_dict() == onto.to_dict()


def test_empty_graph_has_empty_ontology():
    onto = infer_ontology({"nodes": [], "links": []})
    assert not onto.classes and not onto.relations
    assert validate_graph({"nodes": [], "links": []}, onto)["ok"] is True
