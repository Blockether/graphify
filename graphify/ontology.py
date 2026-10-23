"""Ontology for a graphify knowledge graph.

An ontology names the node classes and relation types that a graph may use:

- a class has an optional parent class and typed properties (some required);
- a relation type has a domain and a range (lists of classes; subclasses
  match) and a cardinality ("many", or "one" outgoing edge per source node).

``validate_graph`` checks a node-link graph against an ontology and returns a
consistency report: the findings (errors, warnings, notes) and their counts.
``infer_ontology`` derives a permissive starting ontology from the classes and
relations that a graph already uses, so a fresh graph validates cleanly and you
can tighten the rules from there.

The ontology is stored as ``ontology.json`` beside ``graph.json``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

ONTOLOGY_VERSION = 1
PROPERTY_TYPES = ("string", "integer", "number", "boolean", "url", "date", "list", "object")
CARDINALITIES = ("many", "one")
SEVERITIES = ("error", "warning", "info")

# Node attributes that graphify itself maintains. They are never treated as
# ontology properties and never inferred as required.
RESERVED_NODE_KEYS = frozenset(
    {
        "id",
        "label",
        "class",
        "file_type",
        "source_file",
        "source_location",
        "community",
        "community_name",
        "norm_label",
        "_origin",
        "_src",
        "_tgt",
        "doc_id",
        "created_at",
        "updated_at",
        "actor",
    }
)

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-. ]{0,79}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][0-9:.+\-Z]*)?$")


class OntologyError(ValueError):
    """Raised for an ontology that cannot be loaded or saved."""


@dataclass
class PropertySpec:
    name: str
    type: str = "string"
    required: bool = False
    description: str = ""


@dataclass
class ClassSpec:
    name: str
    parent: str | None = None
    description: str = ""
    color: str | None = None
    properties: list[PropertySpec] = field(default_factory=list)


@dataclass
class RelationSpec:
    name: str
    domain: list[str] = field(default_factory=list)
    range: list[str] = field(default_factory=list)
    cardinality: str = "many"
    symmetric: bool = False
    inverse: str | None = None
    description: str = ""


@dataclass
class Ontology:
    """Classes and relation types, keyed by name."""

    classes: dict[str, ClassSpec] = field(default_factory=dict)
    relations: dict[str, RelationSpec] = field(default_factory=dict)
    # Node attribute that names a node's class when it has no ``class`` key.
    class_key: str = "file_type"
    # In strict mode, unknown classes and relations are errors, not warnings.
    strict: bool = False
    description: str = ""

    # -- serialisation -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": ONTOLOGY_VERSION,
            "description": self.description,
            "class_key": self.class_key,
            "strict": self.strict,
            "classes": [asdict(c) for c in self.classes.values()],
            "relations": [asdict(r) for r in self.relations.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ontology":
        if not isinstance(data, dict):
            raise OntologyError("ontology must be a JSON object")
        onto = cls(
            class_key=str(data.get("class_key") or "file_type"),
            strict=bool(data.get("strict", False)),
            description=str(data.get("description") or ""),
        )
        for raw in _as_list(data.get("classes")):
            spec = class_from_dict(raw)
            onto.classes[spec.name] = spec
        for raw in _as_list(data.get("relations")):
            spec = relation_from_dict(raw)
            onto.relations[spec.name] = spec
        return onto

    # -- queries -------------------------------------------------------
    def node_class(self, node: dict[str, Any]) -> str | None:
        value = node.get("class") or node.get(self.class_key)
        return str(value) if value not in (None, "") else None

    def ancestors(self, name: str) -> list[str]:
        """Return ``name`` and its parent chain, nearest first (cycle-safe)."""
        chain: list[str] = []
        current: str | None = name
        while current and current not in chain:
            chain.append(current)
            spec = self.classes.get(current)
            current = spec.parent if spec else None
        return chain

    def is_a(self, name: str | None, target: str) -> bool:
        return bool(name) and target in self.ancestors(name)  # type: ignore[arg-type]

    def properties_for(self, name: str) -> list[PropertySpec]:
        """Return the properties of a class and its ancestors (nearest wins)."""
        seen: dict[str, PropertySpec] = {}
        for cls_name in self.ancestors(name):
            spec = self.classes.get(cls_name)
            if not spec:
                continue
            for prop in spec.properties:
                seen.setdefault(prop.name, prop)
        return list(seen.values())

    def problems(self) -> list[dict[str, Any]]:
        """Return findings about the ontology itself (not the graph)."""
        out: list[dict[str, Any]] = []
        for name, spec in self.classes.items():
            if spec.parent and spec.parent not in self.classes:
                out.append(_finding("error", "unknown_parent", f"Class {name} has an unknown parent {spec.parent}.", ontology_class=name))
            chain = []
            current: str | None = name
            while current and current in self.classes:
                if current in chain:
                    out.append(_finding("error", "class_cycle", f"Class {name} is its own ancestor.", ontology_class=name))
                    break
                chain.append(current)
                current = self.classes[current].parent
        for name, rel in self.relations.items():
            for cls_name in list(rel.domain) + list(rel.range):
                if cls_name not in self.classes:
                    out.append(_finding("warning", "unknown_relation_class", f"Relation {name} names an unknown class {cls_name}.", ontology_relation=name))
            if rel.inverse and rel.inverse not in self.relations:
                out.append(_finding("warning", "unknown_inverse", f"Relation {name} has an unknown inverse {rel.inverse}.", ontology_relation=name))
        return out


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        # Accept {"Name": {...}} as well as [{"name": "Name", ...}].
        return [dict(v, name=k) if isinstance(v, dict) else {"name": k} for k, v in value.items()]
    if isinstance(value, list):
        return value
    raise OntologyError("classes and relations must be lists or objects")


def check_name(name: Any, what: str) -> str:
    text = str(name or "").strip()
    if not _NAME_RE.match(text):
        raise OntologyError(f"invalid {what} name {name!r}")
    return text


def class_from_dict(raw: Any) -> ClassSpec:
    if not isinstance(raw, dict):
        raise OntologyError("a class must be an object")
    props = []
    for p in raw.get("properties") or []:
        if isinstance(p, str):
            p = {"name": p}
        ptype = str(p.get("type") or "string")
        if ptype not in PROPERTY_TYPES:
            raise OntologyError(f"unknown property type {ptype!r}; use one of {', '.join(PROPERTY_TYPES)}")
        props.append(
            PropertySpec(
                name=check_name(p.get("name"), "property"),
                type=ptype,
                required=bool(p.get("required", False)),
                description=str(p.get("description") or ""),
            )
        )
    parent = raw.get("parent")
    return ClassSpec(
        name=check_name(raw.get("name"), "class"),
        parent=str(parent) if parent else None,
        description=str(raw.get("description") or ""),
        color=str(raw["color"]) if raw.get("color") else None,
        properties=props,
    )


def relation_from_dict(raw: Any) -> RelationSpec:
    if not isinstance(raw, dict):
        raise OntologyError("a relation must be an object")
    cardinality = str(raw.get("cardinality") or "many")
    if cardinality not in CARDINALITIES:
        raise OntologyError(f"unknown cardinality {cardinality!r}; use many or one")

    def names(value: Any) -> list[str]:
        if value in (None, "", []):
            return []
        if isinstance(value, str):
            value = [value]
        return [check_name(v, "class") for v in value]

    inverse = raw.get("inverse")
    return RelationSpec(
        name=check_name(raw.get("name"), "relation"),
        domain=names(raw.get("domain")),
        range=names(raw.get("range")),
        cardinality=cardinality,
        symmetric=bool(raw.get("symmetric", False)),
        inverse=str(inverse) if inverse else None,
        description=str(raw.get("description") or ""),
    )


# -- storage -----------------------------------------------------------


def ontology_path(graph_path: Path | str) -> Path:
    return Path(graph_path).with_name("ontology.json")


def load_ontology(path: Path | str) -> Ontology | None:
    """Load ``ontology.json``; return None when the file does not exist."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OntologyError(f"cannot read {p}: {exc}") from exc
    return Ontology.from_dict(data)


def save_ontology(onto: Ontology, path: Path | str) -> Path:
    p = Path(path)
    write_json_atomic(p, onto.to_dict())
    return p


def write_json_atomic(path: Path, data: Any, *, indent: int | None = 2) -> None:
    """Write JSON through a temporary file and an atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=indent)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- inference ---------------------------------------------------------


def _links(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return graph.get("links") if "links" in graph else graph.get("edges", [])  # type: ignore[return-value]


def value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, dict):
        return "object"
    text = str(value)
    if text.startswith(("http://", "https://")):
        return "url"
    if _DATE_RE.match(text):
        return "date"
    return "string"


def infer_ontology(graph: dict[str, Any], *, class_key: str = "file_type") -> Ontology:
    """Derive a permissive ontology from the classes and relations in ``graph``.

    Every class that a node uses becomes a class. A property becomes required
    only when every node of the class has it. Each relation gets the domain and
    range classes that its edges use today, so the graph validates cleanly.
    """
    onto = Ontology(class_key=class_key, description="Inferred from the current graph.")
    nodes = graph.get("nodes", [])
    by_id: dict[str, str | None] = {}
    members: Counter[str] = Counter()
    prop_seen: dict[str, Counter[str]] = defaultdict(Counter)
    prop_types: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for node in nodes:
        cls_name = onto.node_class(node)
        by_id[str(node.get("id"))] = cls_name
        if not cls_name:
            continue
        members[cls_name] += 1
        for key, value in node.items():
            if key in RESERVED_NODE_KEYS or key.startswith("_") or value in (None, ""):
                continue
            prop_seen[cls_name][key] += 1
            prop_types[(cls_name, key)][value_type(value)] += 1
    for cls_name in sorted(members):
        props = []
        for key, count in sorted(prop_seen[cls_name].items()):
            ptype = prop_types[(cls_name, key)].most_common(1)[0][0]
            props.append(PropertySpec(name=key, type=ptype, required=count == members[cls_name]))
        try:
            onto.classes[cls_name] = ClassSpec(name=check_name(cls_name, "class"), properties=props)
        except OntologyError:
            continue
    domains: dict[str, set[str]] = defaultdict(set)
    ranges: dict[str, set[str]] = defaultdict(set)
    for link in _links(graph):
        rel = str(link.get("relation") or "")
        if not rel:
            continue
        s = by_id.get(str(link.get("source")))
        t = by_id.get(str(link.get("target")))
        if s:
            domains[rel].add(s)
        if t:
            ranges[rel].add(t)
        domains.setdefault(rel, set())
    for rel in sorted(domains):
        try:
            name = check_name(rel, "relation")
        except OntologyError:
            continue
        onto.relations[name] = RelationSpec(
            name=name,
            domain=sorted(c for c in domains[rel] if c in onto.classes),
            range=sorted(c for c in ranges[rel] if c in onto.classes),
        )
    return onto


# -- validation --------------------------------------------------------


def _finding(severity: str, code: str, message: str, **where: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"severity": severity, "code": code, "message": message}
    item.update({k: v for k, v in where.items() if v is not None})
    return item


def _type_ok(value: Any, ptype: str) -> bool:
    if ptype == "string":
        return isinstance(value, str)
    if ptype == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if ptype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ptype == "boolean":
        return isinstance(value, bool)
    if ptype == "url":
        return isinstance(value, str) and value.startswith(("http://", "https://"))
    if ptype == "date":
        return isinstance(value, str) and bool(_DATE_RE.match(value))
    if ptype == "list":
        return isinstance(value, list)
    if ptype == "object":
        return isinstance(value, dict)
    return True


def _matches(cls_name: str | None, allowed: Iterable[str], onto: Ontology) -> bool:
    allowed = list(allowed)
    if not allowed:
        return True
    return any(onto.is_a(cls_name, a) for a in allowed)


def validate_graph(
    graph: dict[str, Any],
    onto: Ontology,
    *,
    limit: int = 500,
    include_info: bool = True,
) -> dict[str, Any]:
    """Check ``graph`` against ``onto`` and return a consistency report.

    The report has ``ok`` (no errors), ``counts`` per severity, ``by_code``
    counts, the first ``limit`` findings (errors first) and ``truncated``.
    """
    findings: list[dict[str, Any]] = list(onto.problems())
    unknown_sev = "error" if onto.strict else "warning"
    nodes = graph.get("nodes", [])
    ids: dict[str, dict[str, Any]] = {}
    degree: Counter[str] = Counter()
    labels: dict[tuple[str, str], list[str]] = defaultdict(list)
    for node in nodes:
        nid = str(node.get("id"))
        if nid in ids:
            findings.append(_finding("error", "duplicate_id", f"Node id {nid} appears more than once.", node=nid))
        ids[nid] = node
        cls_name = onto.node_class(node)
        label = str(node.get("label") or "")
        if not label:
            findings.append(_finding("warning", "missing_label", f"Node {nid} has no label.", node=nid))
        if not cls_name:
            findings.append(_finding(unknown_sev, "missing_class", f"Node {label or nid} has no class.", node=nid))
            continue
        if cls_name not in onto.classes:
            findings.append(_finding(unknown_sev, "unknown_class", f"Node {label or nid} uses class {cls_name}, which the ontology does not define.", node=nid, ontology_class=cls_name))
            continue
        for prop in onto.properties_for(cls_name):
            value = node.get(prop.name)
            if value in (None, "", []):
                if prop.required:
                    findings.append(_finding("error", "missing_property", f"{cls_name} {label or nid} has no {prop.name}.", node=nid, property=prop.name, ontology_class=cls_name))
                continue
            if not _type_ok(value, prop.type):
                findings.append(_finding("error", "property_type", f"{cls_name} {label or nid}: {prop.name} must be {prop.type}, not {value_type(value)}.", node=nid, property=prop.name, ontology_class=cls_name))
        if label and node.get("_origin") == "kb":
            labels[(cls_name, label.strip().lower())].append(nid)
    one_counts: Counter[tuple[str, str]] = Counter()
    pair_seen: set[tuple[str, str, str]] = set()
    for link in _links(graph):
        s, t = str(link.get("source")), str(link.get("target"))
        rel = str(link.get("relation") or "")
        where = {"edge": {"source": s, "target": t, "relation": rel}}
        degree[s] += 1
        degree[t] += 1
        if s not in ids or t not in ids:
            missing = s if s not in ids else t
            findings.append(_finding("error", "dangling_edge", f"Edge {rel} points to a missing node {missing}.", **where))
            continue
        if not rel:
            findings.append(_finding("error", "missing_relation", f"Edge {s} to {t} has no relation.", **where))
            continue
        spec = onto.relations.get(rel)
        if spec is None:
            findings.append(_finding(unknown_sev, "unknown_relation", f"Relation {rel} is not in the ontology.", ontology_relation=rel, **where))
            continue
        s_cls, t_cls = onto.node_class(ids[s]), onto.node_class(ids[t])
        if not _matches(s_cls, spec.domain, onto):
            findings.append(_finding("error", "domain_violation", f"{rel} starts at {s_cls or 'an unclassified node'}; it must start at {', '.join(spec.domain)}.", node=s, ontology_relation=rel, **where))
        if not _matches(t_cls, spec.range, onto):
            findings.append(_finding("error", "range_violation", f"{rel} ends at {t_cls or 'an unclassified node'}; it must end at {', '.join(spec.range)}.", node=t, ontology_relation=rel, **where))
        if spec.cardinality == "one":
            one_counts[(s, rel)] += 1
        if spec.symmetric:
            pair_seen.add((s, t, rel))
    for (s, rel), count in one_counts.items():
        if count > 1:
            findings.append(_finding("error", "cardinality", f"{ids[s].get('label') or s} has {count} {rel} edges; the ontology allows one.", node=s, ontology_relation=rel))
    for s, t, rel in pair_seen:
        if (t, s, rel) not in pair_seen and s != t:
            findings.append(_finding("info", "asymmetric_edge", f"{rel} is symmetric, but only {s} to {t} exists.", node=s, ontology_relation=rel, edge={"source": s, "target": t, "relation": rel}))
    for (cls_name, _label), group in labels.items():
        if len(group) > 1:
            findings.append(_finding("warning", "duplicate_label", f"{len(group)} {cls_name} nodes share the label {ids[group[0]].get('label')}.", node=group[0], nodes=group, ontology_class=cls_name))
    if include_info:
        for nid in ids:
            if degree[nid] == 0:
                findings.append(_finding("info", "orphan_node", f"{ids[nid].get('label') or nid} has no edges.", node=nid))
    else:
        findings = [f for f in findings if f["severity"] != "info"]
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (rank.get(f["severity"], 9), f["code"]))
    counts = Counter(f["severity"] for f in findings)
    by_code = Counter(f["code"] for f in findings)
    limit = max(0, int(limit))
    return {
        "ok": counts.get("error", 0) == 0,
        "counts": {s: counts.get(s, 0) for s in SEVERITIES},
        "by_code": dict(by_code.most_common()),
        "nodes": len(ids),
        "edges": len(_links(graph)),
        "findings": findings[:limit],
        "truncated": len(findings) > limit,
        "total": len(findings),
    }
