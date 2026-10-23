"""Ontology inference and graph validation (graphify.ontology).

The tests name the research-memo item that they cover (F1-F8, R1-R7).
"""
import json

import pytest

from graphify.ontology import (
    NONCONFORMING_EXTRACTION,
    Ontology,
    OntologyError,
    RelationSpec,
    graph_norm_label,
    infer_ontology,
    method_owners,
    method_targets,
    nonconforming_extraction,
    validate_graph,
    with_kb_vocabulary,
)
from graphify.validate import VALID_FILE_TYPES
from tests.kb_fixtures import make_graph


def _graph(tmp_path):
    return json.loads(make_graph(tmp_path).read_text())


def _code(nid, **extra):
    return {"id": nid, "label": nid, "file_type": "code", "source_file": f"{nid}.py", **extra}


def _fn(nid, **extra):
    return _code(nid, _callable=True, **extra)


def _edge(s, t, rel, **extra):
    return {"source": s, "target": t, "relation": rel, **extra}


def _codes(report, severity=None):
    return [f["code"] for f in report["findings"] if severity is None or f["severity"] == severity]


# -- classes (F1, R1) ----------------------------------------------------


def test_infer_roots_are_graphify_file_types(tmp_path):
    onto = infer_ontology(_graph(tmp_path))
    roots = {name for name, spec in onto.classes.items() if spec.parent is None}
    assert roots == VALID_FILE_TYPES
    assert onto.classes["code"].support == 2 and onto.classes["rationale"].support == 1
    assert all(spec.origin == "inferred" for spec in onto.classes.values())
    assert onto.relations["calls"].support == 1
    assert onto.relations["rationale_for"].domain == []  # fewer than 30 edges: open (R2)


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({"file_type": "code", "label": "ns", "metadata": {"kind": "namespace"}}, "code.namespace"),
        ({"file_type": "code", "label": "run()", "metadata": {"kind": "var"}}, "code.function"),
        ({"file_type": "code", "label": "run-test (test)", "metadata": {"kind": "var"}}, "code.test"),
        ({"file_type": "code", "label": "when-let* (macro)", "metadata": {"kind": "var"}}, "code.macro"),
        ({"file_type": "code", "label": "config", "metadata": {"kind": "var"}}, "code.variable"),
        ({"file_type": "code", "label": "deploy", "metadata": {"kind": "bash_function"}}, "code.function"),
        ({"file_type": "code", "label": "x script", "metadata": {"kind": "bash_entrypoint"}}, "code.entrypoint"),
        ({"file_type": "code", "label": "Gateway", "_callable": True, "_callable_class": True}, "code.class"),
        ({"file_type": "code", "label": "start()", "_callable": True}, "code.function"),
        ({"file_type": "code", "label": "babashka.fs", "type": "module", "source_file": ""}, "code.module"),
        ({"file_type": "code", "label": "core.py", "source_file": "src/core.py", "source_location": "L1"}, "code.file"),
        # A bare import target has no location, so it is not a file node.
        ({"file_type": "code", "label": "pdfjs-dist", "source_file": "pdfjs-dist"}, "code"),
        # S3 review: a definition with no kind stays plain code; a name with
        # no source is unresolved, unless a person wrote it in the KB.
        ({"file_type": "code", "label": "Props", "source_file": "a.ts", "source_location": "L3"}, "code"),
        ({"file_type": "code", "label": "AtomicU64"}, "code.unresolved"),
        ({"file_type": "code", "label": "Idea", "_origin": "kb"}, "code"),
        ({"file_type": "code", "label": ".lease()"}, "code.method"),
        ({"file_type": "concept", "label": "react", "type": "external", "external": True, "source_file": ""}, "concept.external"),
        ({"file_type": "concept", "label": "Lease policy", "source_file": "kb"}, "concept"),
        ({"file_type": "document", "label": "README", "node_kind": "page"}, "document.page"),
        ({"file_type": "document", "label": "Install", "node_kind": "heading"}, "document.section"),
        ({"file_type": "document", "label": "Note"}, "document"),
        ({"file_type": "rationale", "label": "Why"}, "rationale"),
        ({"file_type": "code", "label": "X", "class": "Service"}, "Service"),
        ({"label": "nothing"}, None),
    ],
)
def test_node_class_follows_graphify_attributes(node, expected):
    assert Ontology().node_class(dict(node, id="n")) == expected


def test_method_comes_from_method_edges():
    # F1: the method edge is the evidence; the ".x()" label shape is a fallback.
    graph = {
        "nodes": [_code("C", _callable_class=True, _callable=True), _fn("run"), _fn("other")],
        "links": [_edge("C", "run", "method")],
    }
    targets = method_targets(graph)
    onto = Ontology()
    assert onto.node_class(graph["nodes"][1], method_targets=targets) == "code.method"
    assert onto.node_class(graph["nodes"][2], method_targets=targets) == "code.function"
    assert onto.is_a("code.method", "code.function") and onto.is_a("code.method", "code")
    assert infer_ontology(graph).classes["code.method"].support == 1


def test_method_owner_is_a_class():
    # S3 review: a Clojure protocol or record var that owns methods is a class.
    store = _code("Store", metadata={"kind": "var"})
    graph = {"nodes": [store, _fn("run")], "links": [_edge("Store", "run", "method")]}
    onto = Ontology()
    assert onto.node_class(store) == "code.variable"
    assert onto.node_class(store, method_owners=method_owners(graph)) == "code.class"
    assert infer_ontology(graph).classes["code.class"].support == 1


def test_custom_class_key_reads_that_key():
    onto = Ontology(class_key="kind")
    assert onto.node_class({"id": "n", "kind": "service", "file_type": "code"}) == "service"


# -- domain and range (F2, R2) -------------------------------------------


def _call_graph(n=40):
    nodes = [_fn(f"f{i}") for i in range(n)] + [{"id": "r", "label": "Why", "file_type": "rationale", "source_file": "a.py"}]
    links = [_edge(f"f{i}", f"f{(i + 1) % n}", "calls") for i in range(n)]
    return {"nodes": nodes, "links": links}


def test_inferred_ontology_validates_its_own_graph(tmp_path):
    graph = _graph(tmp_path)
    report = validate_graph(graph, infer_ontology(graph))
    assert report["ok"] is True
    assert report["counts"] == {"error": 0, "warning": 0, "info": 0}
    assert report["nodes"] == 3 and report["edges"] == 2


def test_inferred_domain_and_range_never_give_errors():
    graph = _call_graph()
    onto = infer_ontology(graph)
    assert onto.relations["calls"].domain == ["code.function"] and onto.relations["calls"].range == ["code.function"]
    graph["links"].append(_edge("f1", "r", "calls"))
    report = validate_graph(graph, onto)
    assert report["ok"] is True
    finding = next(f for f in report["findings"] if f["code"] == "unlikely_range")
    assert finding["severity"] == "warning" and finding["score"] == 0 and finding["support"] == 40


def test_declared_domain_and_range_default_to_error(tmp_path):
    # SHACL: sh:Violation is the default severity of a declared shape.
    graph = _graph(tmp_path)
    onto = Ontology.from_dict({
        "strict": True,
        "classes": [{"name": "code"}, {"name": "rationale"}],
        "relations": [{"name": "calls", "domain": ["code"], "range": ["code"]}, {"name": "rationale_for"}],
    })
    graph["links"].append(_edge("a", "r", "calls"))
    graph["links"].append(_edge("a", "b", "teleports"))
    graph["nodes"].append({"id": "x", "label": "X", "file_type": "alien"})
    report = validate_graph(graph, onto)
    assert {"range_violation", "unknown_relation", "unknown_class"} <= set(report["by_code"])
    assert report["ok"] is False


def test_small_relation_stays_open():
    # R2: a relation with fewer than 30 edges gets no domain or range.
    graph = _call_graph(12)
    onto = infer_ontology(graph)
    assert onto.relations["calls"].domain == [] and onto.relations["calls"].range == []
    graph["links"].append(_edge("r", "f1", "calls"))
    assert "unlikely_domain" not in validate_graph(graph, onto)["by_code"]


def test_subclasses_roll_up_to_their_parent():
    # R2: function and method both qualify and cover the parent, so the
    # domain rolls up to code.function.
    nodes = [_code("C", _callable_class=True)] + [_fn(f"f{i}") for i in range(20)] + [_fn(f"m{i}") for i in range(20)]
    links = [_edge("C", f"m{i}", "method") for i in range(20)]
    links += [_edge(f"f{i}", f"m{i}", "calls") for i in range(20)] + [_edge(f"m{i}", f"f{i}", "calls") for i in range(20)]
    onto = infer_ontology({"nodes": nodes, "links": links})
    assert onto.relations["calls"].domain == ["code.function"]
    assert onto.relations["calls"].domain_profile == {"code.function": 20, "code.method": 20}


def test_edge_end_score_sets_the_severity():
    # R2 (SDValidate): outside the mined range, p < 0.01 is a warning and a
    # rarer class with p >= 0.01 is an info. A subclass of the range matches.
    onto = Ontology()
    onto.relations["calls"] = RelationSpec(
        "calls", domain=["code"], range=["code"], origin="inferred", support=1000,
        domain_profile={"code": 1000}, range_profile={"code": 960, "concept": 30, "rationale": 5},
    )
    assert onto.check_edge("code.function", "calls", "code.method") == []
    [info] = onto.check_edge("code", "calls", "concept")
    assert (info["code"], info["severity"], info["score"]) == ("unlikely_range", "info", 0.03)
    [warn] = onto.check_edge("code", "calls", "rationale")
    assert (warn["code"], warn["severity"]) == ("unlikely_range", "warning")
    assert onto.check_edge("document.page", "calls", "code")[0]["code"] == "unlikely_domain"


def test_common_edge_end_is_never_flagged():
    # S1 review: at p >= RARE_SCORE (0.05) an edge end is normal, not rare.
    onto = Ontology()
    onto.relations["calls"] = RelationSpec(
        "calls", domain=["code"], range=["code.function"], origin="inferred", support=100,
        domain_profile={"code": 100}, range_profile={"code.function": 94, "code.module": 6},
    )
    assert onto.check_edge("code", "calls", "code.module") == []
    [info] = onto.check_edge("code", "calls", "concept")
    assert info["severity"] == "warning" and info["score"] == 0.0


def test_small_class_with_a_large_edge_share_joins_the_range():
    # S1 review: dynamic_import ends at a few modules in 30% of its edges.
    fns = [_fn(f"f{i}") for i in range(40)]
    mods = [_code(f"m{i}", type="module", source_file="") for i in range(3)]
    links = [_edge(f"f{i}", f"f{(i + 1) % 28}", "dynamic_import") for i in range(28)]
    links += [_edge(f"f{28 + i}", f"m{i % 3}", "dynamic_import") for i in range(12)]
    # Two rare variable targets (2 of 42 edges, below 5%) keep the two
    # classes from rolling up to code (31 of 33 nodes, below 95%).
    vars_ = [_code(f"v{i}", metadata={"kind": "var"}) for i in range(2)]
    links += [_edge("f0", f"v{i}", "dynamic_import") for i in range(2)]
    onto = infer_ontology({"nodes": fns + mods + vars_, "links": links})
    assert onto.relations["dynamic_import"].range == ["code.function", "code.module"]
    assert onto.check_edge("code.function", "dynamic_import", "code.module") == []


# -- properties and edge shapes (F3, R3) ---------------------------------


def _owned(total, owned):
    return {"nodes": [_code(f"n{i}", **({"owner": "team"} if i < owned else {})) for i in range(total)], "links": []}


@pytest.mark.parametrize(
    ("total", "owned", "severity"),
    [(100, 100, "warning"), (100, 99, "warning"), (100, 97, "info"), (100, 90, None), (40, 40, None)],
)
def test_inferred_min_count_bands(total, owned, severity):
    onto = infer_ontology(_owned(total, owned))
    prop = next(p for p in onto.classes["code"].properties if p.name == "owner")
    assert prop.origin == "inferred" and prop.severity == severity and prop.required is (severity is not None)
    assert prop.support == owned and prop.confidence == round(owned / total, 4)
    graph = _owned(total, owned)
    graph["nodes"].append(_code("late"))
    report = validate_graph(graph, onto)
    assert report["ok"] is True
    missing = [f for f in report["findings"] if f["code"] == "missing_property"]
    assert {f["severity"] for f in missing} == ({severity} if severity else set())


def test_declared_required_property_is_an_error(tmp_path):
    graph = _graph(tmp_path)
    onto = Ontology.from_dict({
        "classes": [{"name": "code", "properties": [{"name": "owner", "type": "string", "required": True}]},
                    {"name": "rationale"}],
        "relations": [{"name": "calls"}, {"name": "rationale_for"}],
    })
    report = validate_graph(graph, onto)
    assert report["by_code"].get("missing_property") == 2 and report["ok"] is False


def test_bookkeeping_keys_are_not_properties():
    # F3c: graphify bookkeeping never becomes a required property.
    extra = {"confidence": "EXTRACTED", "version": "1.0", "ecosystem": "npm", "type": "x", "metadata": {"kind": "y"}, "node_kind": "z"}
    onto = infer_ontology({"nodes": [_code(f"n{i}", **extra) for i in range(60)], "links": []})
    assert onto.classes["code"].properties == []


def test_inferred_shapes_skip_kb_nodes():
    # R3: inferred node shapes were mined from extracted nodes; a KB node
    # meets the declared shapes only.
    onto = infer_ontology(_owned(100, 100))
    graph = _owned(100, 100)
    graph["nodes"] += [_code("kb1", _origin="kb"), _code("ex1")]
    report = validate_graph(graph, onto)
    nodes = [f["node"] for f in report["findings"] if f["code"] == "missing_property"]
    assert nodes == ["ex1"]


def test_behaviour_relations_get_no_node_shape():
    # S2 review: a test without calls edges is valid (open world); only the
    # structural relations of SHAPE_RELATIONS get an inferred node shape.
    tests = [_code(f"t{i}", metadata={"kind": "var"}, label=f"t{i} (test)") for i in range(100)]
    graph = {"nodes": tests + [_fn("f")], "links": [_edge(f"t{i}", "f", "calls") for i in range(99)]}
    onto = infer_ontology(graph)
    assert not [s for s in onto.edge_shapes_for("code.test") if s.relation == "calls"]
    assert "missing_edge" not in validate_graph(graph, onto)["by_code"]


def test_dynamic_import_counts_as_an_import():
    # S2 review: a module that only a dynamic import reaches still meets an
    # "imported at least once" shape (RELATION_FAMILY, like sh:alternativePath).
    onto = Ontology.from_dict({
        "version": 2,
        "classes": [{"name": "code.module", "edges": [{"relation": "imports", "direction": "in", "min_count": 1}]}],
        "relations": [{"name": "imports"}, {"name": "dynamic_import"}],
    })
    mod = _code("pdfjs", type="module", source_file="")
    graph = {"nodes": [_fn("f"), mod, _code("lone", type="module", source_file="")], "links": [_edge("f", "pdfjs", "dynamic_import")]}
    missing = [f["node"] for f in validate_graph(graph, onto)["findings"] if f["code"] == "missing_edge"]
    assert missing == ["lone"]


def test_rationale_needs_rationale_for():
    # F4/R3: 99% of rationale nodes explain code; the rest are extractor gaps.
    nodes = [_fn(f"f{i}") for i in range(100)] + [{"id": f"r{i}", "label": f"Why {i}", "file_type": "rationale", "source_file": "a.py"} for i in range(100)]
    links = [_edge(f"r{i}", f"f{i}", "rationale_for") for i in range(99)]
    graph = {"nodes": nodes, "links": links}
    onto = infer_ontology(graph)
    [shape] = [s for s in onto.classes["rationale"].edges if s.relation == "rationale_for"]
    assert (shape.direction, shape.severity, shape.support) == ("out", "warning", 99)
    report = validate_graph(graph, onto)
    missing = [f for f in report["findings"] if f["code"] == "missing_edge" and f["ontology_class"] == "rationale"]
    assert [f["node"] for f in missing] == ["r99"] and missing[0]["severity"] == "warning"


def test_contains_has_one_container():
    # F8/R3: inverse cardinality, inferred at QSE's 100% maxCount rule.
    nodes = [_code("file", source_location="L1")] + [_fn(f"f{i}") for i in range(60)]
    graph = {"nodes": nodes + [_code("file2")], "links": [_edge("file", f"f{i}", "contains") for i in range(60)]}
    onto = infer_ontology(graph)
    assert onto.relations["contains"].in_cardinality == "one"
    graph["links"].append(_edge("file2", "f0", "contains"))
    report = validate_graph(graph, onto)
    [finding] = [f for f in report["findings"] if f["code"] == "in_cardinality"]
    assert finding["severity"] == "warning" and finding["node"] == "f0"


# -- checks (F8, R7) -----------------------------------------------------


def test_contains_cycle_is_an_error_and_recursion_is_not_flagged():
    graph = _call_graph()
    graph["links"] += [_edge("f0", "f0", "calls"), _edge("f1", "f2", "contains"), _edge("f2", "f1", "contains")]
    onto = infer_ontology({"nodes": graph["nodes"], "links": graph["links"][:40]})
    with_kb_vocabulary(onto)
    onto.relations["contains"] = RelationSpec("contains", acyclic=True, origin="inferred", severity={"acyclic": "error"})
    report = validate_graph(graph, onto)
    [cycle] = [f for f in report["findings"] if f["code"] == "cycle"]
    assert cycle["severity"] == "error" and set(cycle["nodes"]) == {"f1", "f2"}
    assert not [f for f in report["findings"] if f.get("edge", {}).get("source") == "f0" and f["edge"]["target"] == "f0"]


def test_infer_marks_contains_and_inherits_acyclic():
    graph = {"nodes": [_code(f"c{i}", _callable_class=True) for i in range(31)], "links": [_edge(f"c{i}", f"c{i + 1}", "inherits") for i in range(30)]}
    spec = infer_ontology(graph).relations["inherits"]
    assert spec.acyclic is True and spec.rule_severity("acyclic") == "error"


@pytest.mark.parametrize(
    ("confidence", "score", "expected"),
    [
        ("EXTRACTED", 1.0, None),
        ("INFERRED", 0.85, None),
        ("AMBIGUOUS", 0.2, None),
        ("EXTRACTED", 0.85, ("confidence_mismatch", "warning")),
        ("INFERRED", 0.5, ("confidence_mismatch", "warning")),
        ("INFERRED", 1.0, ("confidence_mismatch", "warning")),
        ("INFERRED", 0.7, ("confidence_mismatch", "info")),
        ("INFERRED", 0.45, ("confidence_mismatch", "info")),
        # N2 review: at 0.4 or below the spec says AMBIGUOUS.
        ("INFERRED", 0.4, ("confidence_mismatch", "warning")),
        ("INFERRED", 0.35, ("confidence_mismatch", "warning")),
        ("AMBIGUOUS", 0.6, ("confidence_mismatch", "warning")),
        ("SURE", 1.0, ("invalid_confidence", "warning")),
        ("EXTRACTED", 1.5, ("invalid_confidence", "warning")),
    ],
)
def test_confidence_score_follows_graphify_rubric(confidence, score, expected):
    # F8: graphify/skills/<agent>/references/extraction-spec.md and
    # export._CONFIDENCE_SCORE_DEFAULTS.
    graph = {"nodes": [_fn("a"), _fn("b")], "links": [_edge("a", "b", "calls", confidence=confidence, confidence_score=score)]}
    report = validate_graph(graph, infer_ontology(graph))
    found = [(f["code"], f["severity"]) for f in report["findings"]]
    assert found == ([expected] if expected else [])


def test_orphans_are_reported_for_kb_nodes_only():
    # F4: an extracted orphan is an extractor artefact, a KB orphan is an
    # authoring gap.
    graph = {"nodes": [_code("lone"), {"id": "kb:x", "label": "Idea", "file_type": "concept", "source_file": "kb", "_origin": "kb"}], "links": []}
    report = validate_graph(graph, infer_ontology(graph))
    orphans = [f for f in report["findings"] if f["code"] == "orphan_node"]
    assert [(f["node"], f["severity"]) for f in orphans] == [("kb:x", "info")]


def test_duplicate_entity_across_origins():
    # F8/R6.4: a KB node that repeats an extracted entity.
    cls = _code("gw", label="GatewayClient", _callable_class=True)
    kb = {"id": "kb:gw", "label": "Gatewayclient", "file_type": "concept", "source_file": "kb", "_origin": "kb"}
    page = {"id": "kb:install", "label": "Install", "file_type": "document", "node_kind": "page", "source_file": "kb", "_origin": "kb"}
    heading = {"id": "h", "label": "Install", "file_type": "document", "node_kind": "heading", "source_file": "README.md"}
    graph = {"nodes": [cls, kb, page, heading], "links": []}
    onto = with_kb_vocabulary(infer_ontology({"nodes": [cls, heading], "links": []}))
    report = validate_graph(graph, onto)
    [dup] = [f for f in report["findings"] if f["code"] == "duplicate_entity"]
    assert dup["severity"] == "warning" and dup["nodes"] == ["kb:gw", "gw"]
    assert graph_norm_label("Gatewayclient") == graph_norm_label("GatewayClient")
    graph["links"].append(_edge("kb:gw", "gw", "mentions"))
    assert "duplicate_entity" not in validate_graph(graph, onto)["by_code"]


def test_duplicate_entity_skips_weak_candidates():
    # S7 review: a test-local variable is not the entity that a KB concept
    # names, and several candidates give an info with the list.
    kb = {"id": "kb:s", "label": "Session", "file_type": "concept", "source_file": "kb", "_origin": "kb"}
    local = _code("gw_var", label="Session", metadata={"kind": "var"})
    in_test = _code("t_cls", label="Session", _callable_class=True, source_file="src/__tests__/a.test.ts")
    graph = {"nodes": [kb, local, in_test], "links": []}
    onto = with_kb_vocabulary(infer_ontology({"nodes": [local, in_test], "links": []}))
    assert "duplicate_entity" not in validate_graph(graph, onto)["by_code"]
    graph["nodes"] += [_code("a", label="Session", _callable_class=True), _code("b", label="session", _callable=True)]
    [dup] = [f for f in validate_graph(graph, onto)["findings"] if f["code"] == "duplicate_entity"]
    assert dup["severity"] == "info" and dup["candidates"] == 2 and set(dup["nodes"]) == {"kb:s", "a", "b"}


def test_check_edge_and_nonconforming_extraction():
    onto = with_kb_vocabulary(infer_ontology(_call_graph()))
    assert onto.check_edge("document.page", "mentions", "code.function") == []
    [bad] = onto.check_edge("code.function", "mentions", "code.function")
    assert (bad["code"], bad["severity"]) == ("domain_violation", "error")
    findings = onto.check_edge("concept", "teleports", "code") + [bad]
    wrapped = nonconforming_extraction({"source": "a", "target": "b", "relation": "teleports"}, findings)
    assert wrapped is not None
    assert wrapped["code"] == NONCONFORMING_EXTRACTION and wrapped["severity"] == "error"
    assert wrapped["edge"]["relation"] == "teleports" and len(wrapped["findings"]) == 2
    info = {"severity": "info", "code": "unlikely_range", "message": "rare"}
    assert nonconforming_extraction({"id": "n"}, [info]) is None


def test_kb_vocabulary_extends_and_never_overrides():
    onto = infer_ontology({"nodes": [{"id": "c", "label": "C", "file_type": "concept", "source_file": "x"}], "links": []})
    onto.classes["concept"].description = "Mine."
    with_kb_vocabulary(onto)
    assert onto.classes["concept"].description == "Mine."
    assert onto.classes["document.page"].parent == "document"
    assert onto.relations["mentions"].origin == "declared" and onto.relations["mentions"].domain == ["document"]


# -- storage and report shape --------------------------------------------


def test_validate_respects_limit():
    graph = {"nodes": [{"id": f"kb{i}", "label": f"Idea {i}", "file_type": "concept", "source_file": "kb", "_origin": "kb"} for i in range(10)], "links": []}
    report = validate_graph(graph, infer_ontology(graph), limit=3)
    assert len(report["findings"]) == 3
    assert report["truncated"] is True and report["total"] == 10


def test_ontology_round_trips():
    nodes = [_fn(f"f{i}", owner="x") for i in range(60)]
    onto = infer_ontology({"nodes": nodes, "links": [_edge(f"f{i}", f"f{i + 1}", "calls") for i in range(59)]})
    again = Ontology.from_dict(json.loads(json.dumps(onto.to_dict())))
    assert again.to_dict() == onto.to_dict()
    assert onto.to_dict()["version"] == 2


def test_version_one_file_loads_with_defaults():
    # S8 review: v0.9.93-blockether.11 saved data-mined rules as version 1.
    # They load as inferred, so they never give errors.
    onto = Ontology.from_dict({
        "version": 1,
        "classes": [{"name": "code", "properties": [{"name": "owner", "required": True}]}],
        "relations": [{"name": "calls", "domain": ["code"], "cardinality": "one"}],
    })
    prop = onto.classes["code"].properties[0]
    assert (prop.origin, prop.severity, onto.classes["code"].edges) == ("inferred", None, [])
    rel = onto.relations["calls"]
    assert (rel.origin, rel.in_cardinality, rel.acyclic, rel.rule_severity("domain")) == ("inferred", "many", False, "warning")
    assert onto.check_edge("concept", "calls", "code") == []  # no support: open
    assert Ontology.from_dict({"version": 2, "relations": [{"name": "calls"}]}).relations["calls"].origin == "declared"


@pytest.mark.parametrize(
    "bad",
    [
        {"classes": [{"name": "code", "properties": [{"name": "x", "severity": "fatal"}]}]},
        {"classes": [{"name": "code", "edges": [{"relation": "calls", "direction": "sideways"}]}]},
        {"classes": [{"name": "code", "origin": "guessed"}]},
        {"relations": [{"name": "calls", "severity": {"colour": "error"}}]},
        {"relations": [{"name": "calls", "in_cardinality": "two"}]},
    ],
)
def test_from_dict_rejects_bad_values(bad):
    with pytest.raises(OntologyError):
        Ontology.from_dict(bad)


def test_empty_graph_has_only_root_classes():
    onto = infer_ontology({"nodes": [], "links": []})
    assert set(onto.classes) == VALID_FILE_TYPES and not onto.relations
    assert validate_graph({"nodes": [], "links": []}, onto)["ok"] is True
