"""Ontology for a graphify knowledge graph.

An ontology names the node classes and relation types that a graph may use.

Classes follow graphify's own taxonomy. The root classes are exactly the
``file_type`` values of the extractor contract (``graphify.validate``). The
subclasses come from attributes that graphify writes (``metadata.kind``,
``type``, ``_callable``, ``node_kind`` and others), so the knowledge base and
the extractor share one class system. See ``Ontology.node_class``.

Each rule has an origin:

- ``declared``: a person wrote it. A declared rule gives an error by default,
  as ``sh:Violation`` is the SHACL default severity (W3C SHACL, 2017).
- ``inferred``: ``infer_ontology`` mined it from the graph. An inferred rule
  never gives an error, only a warning or an info. It stores its support and
  confidence, so a reader can see how strong the evidence is.

Inference follows QSE (Rabbani, Lissandrini, Hose, PVLDB 16(5), 2023): support
is the number of entities that satisfy a shape and confidence is support
divided by the instances of the target class. An edge end outside the mined
domain or range is scored with the statistical type distribution of
SDType/SDValidate (Paulheim and Bizer, 2013, 2014): ``p = P(class | relation,
end)``, the share of the relation's edges whose end has that class. A class
joins a domain or range when the QSE confidence or the SDType share is high
enough. This OR is our own combination of the two papers, not a rule of
either. The numeric thresholds below are our choices, not paper values.

``validate_graph`` checks a node-link graph against an ontology and returns a
consistency report. The ontology is stored as ``ontology.json`` beside
``graph.json``.
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

from graphify.build import _is_external_stub, _is_file_node_label
from graphify.export import _strip_diacritics
from graphify.paths import _is_test_path
from graphify.validate import VALID_CONFIDENCES

# Version 2 adds rule origins, statistics, edge shapes, inbound cardinality,
# acyclicity and severity overrides. Version 1 files load with the defaults.
ONTOLOGY_VERSION = 2
PROPERTY_TYPES = ("string", "integer", "number", "boolean", "url", "date", "list", "object", "any")
CARDINALITIES = ("many", "one")
SEVERITIES = ("error", "warning", "info")
ORIGINS = ("declared", "inferred")
DIRECTIONS = ("out", "in")
# The rule keys of a relation that accept a severity override.
RELATION_RULES = ("domain", "range", "cardinality", "in_cardinality", "acyclic")

# Thresholds for inferred rules. QSE keeps a shape at ``supp > omega`` and
# ``conf > epsilon`` and lets the user lower the minCount confidence for an
# incomplete graph. The values are the research memo's choices (R2, R3), not
# values from a paper. The OR of the QSE confidence and the SDType share is our
# combination of the two papers.
MIN_CLASS_SUPPORT = 10  # distinct nodes at one end, before a class joins a domain or range
MIN_CLASS_CONFIDENCE = 0.01  # QSE share of the class's nodes, or SDType share of the relation's edges
ROLLUP_COVERAGE = 0.95  # qualifying subclasses cover this share of the parent
MIN_RELATION_SUPPORT = 30  # a smaller relation stays open: no domain or range
UNLIKELY_SCORE = 0.01  # an edge end below this score gives a warning
RARE_SCORE = 0.05  # an edge end below this score gives an info; at or above it, nothing
REQUIRED_CONFIDENCE = 0.99  # minCount 1 as a warning
OPTIONAL_CONFIDENCE = 0.95  # minCount 1 as an info
MIN_SHAPE_SUPPORT = 50  # members of a class before a node shape is inferred
TYPE_AGREEMENT = 0.95  # below this agreement an inferred property type is "any"
MAX_CARDINALITY_SUPPORT = 50  # edges before an inferred maxCount 1

# Containment and inheritance cannot loop in any language that graphify parses.
# infer_ontology marks these relations acyclic, with an error severity.
ACYCLIC_RELATIONS = frozenset({"contains", "inherits"})

# The only (relation, direction) pairs that get an inferred node shape
# ("at least one such edge"). They are structural: graphify writes them for
# every member of a class. A missing behaviour edge (calls, imports) is not
# evidence of an error under the open-world assumption.
SHAPE_RELATIONS = frozenset({("contains", "in"), ("method", "in"), ("rationale_for", "out"), ("defines", "in")})

# Relations that count as a kind of another relation when a node shape counts
# edges, like SHACL sh:alternativePath. A dynamic import is still an import.
RELATION_FAMILY = {"imports_from": "imports", "dynamic_import": "imports"}

# graphify's confidence vocabulary
# (graphify/skills/<agent>/references/extraction-spec.md): EXTRACTED edges
# score 1.0, INFERRED edges pick one value of a fixed set and AMBIGUOUS edges
# score 0.1 to 0.3. An INFERRED edge at 0.4 or below must be AMBIGUOUS.
INFERRED_SCORES = frozenset({0.55, 0.65, 0.75, 0.85, 0.95})
INFERRED_FLOOR = 0.4
AMBIGUOUS_SCORE_RANGE = (0.1, 0.3)

# The finding code that the LLM ingest gives to an extracted edge or node that
# the ontology does not accept. See ``nonconforming_extraction``.
NONCONFORMING_EXTRACTION = "nonconforming_extraction"

# Node attributes that graphify itself maintains. They are never treated as
# ontology properties and never inferred as required. The classifier keys
# (metadata, type, node_kind, external) stay out too: a property that defines
# the class would otherwise be "required" by definition.
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
        "metadata",
        "type",
        "node_kind",
        "external",
        "confidence",
        "confidence_score",
        "version",
        "ecosystem",
        "weight",
    }
)

# graphify's class taxonomy: name -> (parent, description). The roots are the
# file_type values of graphify.validate. Every subclass is derived from an
# attribute that graphify writes; ``Ontology.node_class`` has the rules.
TAXONOMY: dict[str, tuple[str | None, str]] = {
    "code": (None, "Source code that graphify extracted."),
    "document": (None, "A document or a part of one."),
    "paper": (None, "A research paper."),
    "image": (None, "An image."),
    "rationale": (None, "A comment or docstring that explains code."),
    "concept": (None, "A named idea."),
    "code.file": ("code", "A source file."),
    "code.module": ("code", "A module that code imports (graphify type module)."),
    "code.package": ("code", "A package (graphify type package)."),
    "code.namespace": ("code", "A Clojure namespace."),
    "code.class": ("code", "A class, interface, struct or other type."),
    "code.function": ("code", "A function."),
    "code.method": ("code.function", "A function that a class owns: the target of a method edge."),
    "code.test": ("code.function", "A test definition."),
    "code.macro": ("code.function", "A macro."),
    "code.variable": ("code", "A named value that is not a function."),
    "code.entrypoint": ("code", "The top-level body of a script."),
    "code.unresolved": ("code", "A name that code uses, but graphify found no source for it."),
    "document.page": ("document", "A whole document."),
    "document.section": ("document", "A heading and its text."),
    "concept.external": ("concept", "An external dependency that graphify knows only from imports."),
}

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
    origin: str = "declared"
    # None: the origin default (error when declared, warning when inferred).
    severity: str | None = None
    support: int | None = None
    confidence: float | None = None


@dataclass
class EdgeShapeSpec:
    """A node shape on edges: each node of the class has at least ``min_count``
    edges of ``relation`` in ``direction`` (SHACL ``sh:minCount`` on a path)."""

    relation: str
    direction: str = "out"
    min_count: int = 1
    origin: str = "declared"
    severity: str | None = None
    support: int | None = None
    confidence: float | None = None
    description: str = ""


@dataclass
class ClassSpec:
    name: str
    parent: str | None = None
    description: str = ""
    color: str | None = None
    properties: list[PropertySpec] = field(default_factory=list)
    edges: list[EdgeShapeSpec] = field(default_factory=list)
    origin: str = "declared"
    # Nodes of exactly this class when the ontology was inferred.
    support: int | None = None


@dataclass
class RelationSpec:
    name: str
    domain: list[str] = field(default_factory=list)
    range: list[str] = field(default_factory=list)
    # "one": at most one outgoing edge per source node.
    cardinality: str = "many"
    # "one": at most one incoming edge per target node.
    in_cardinality: str = "many"
    symmetric: bool = False
    acyclic: bool = False
    inverse: str | None = None
    description: str = ""
    origin: str = "declared"
    # Severity overrides per rule key (RELATION_RULES).
    severity: dict[str, str] = field(default_factory=dict)
    # Inferred statistics: the edge count and the exact class at each end.
    support: int | None = None
    domain_profile: dict[str, int] = field(default_factory=dict)
    range_profile: dict[str, int] = field(default_factory=dict)

    def rule_severity(self, rule: str) -> str:
        return self.severity.get(rule) or _origin_severity(self.origin)


def _origin_severity(origin: str) -> str:
    return "error" if origin == "declared" else "warning"


def method_targets(graph: dict[str, Any]) -> set[str]:
    """Return the ids of nodes that a ``method`` edge points to."""
    return {str(x.get("target")) for x in _links(graph) if x.get("relation") == "method"}


def method_owners(graph: dict[str, Any]) -> set[str]:
    """Return the ids of nodes that a ``method`` edge starts at."""
    return {str(x.get("source")) for x in _links(graph) if x.get("relation") == "method"}


def _metadata_kind(node: dict[str, Any]) -> str | None:
    meta = node.get("metadata")
    return meta.get("kind") if isinstance(meta, dict) else None


def taxonomy_class(node: dict[str, Any], method_ids: set[str] | None = None, owner_ids: set[str] | None = None) -> str | None:
    """Return the most specific graphify class of ``node``, or None.

    ``method_ids`` holds the targets of ``method`` edges (``method_targets``).
    Without it, graphify's method label shape ``.name()`` decides.
    ``owner_ids`` holds the sources of ``method`` edges (``method_owners``):
    a node that owns methods is a class, also a Clojure protocol or record.
    """
    ft = node.get("file_type")
    if ft in (None, ""):
        return None
    ft = str(ft)
    if ft == "concept":
        return "concept.external" if _is_external_stub(node) else "concept"
    if ft == "document":
        kind = node.get("node_kind")
        return "document.page" if kind == "page" else "document.section" if kind == "heading" else "document"
    if ft != "code":
        return ft
    label = str(node.get("label") or "")
    kind = _metadata_kind(node)
    if method_ids is not None:
        if str(node.get("id")) in method_ids:
            return "code.method"
    elif label.startswith(".") and label.endswith("()"):
        return "code.method"
    if kind == "namespace":
        return "code.namespace"
    # A file node starts at a location; a bare import target has none.
    if kind == "file" or (node.get("source_location") and _is_file_node_label(label, node.get("source_file"))):
        return "code.file"
    if kind == "bash_entrypoint":
        return "code.entrypoint"
    if node.get("type") == "module":
        return "code.module"
    if node.get("type") == "package":
        return "code.package"
    if node.get("_callable_class") or (owner_ids is not None and str(node.get("id")) in owner_ids):
        return "code.class"
    if kind == "bash_function":
        return "code.function"
    if kind == "var":
        # The Clojure extractor encodes the definer in the label
        # (extractors/clojure.py): "name()" for a function, "name (test)",
        # "name (macro)", and a bare name for a value.
        if label.endswith(" (test)"):
            return "code.test"
        if label.endswith(" (macro)"):
            return "code.macro"
        return "code.function" if label.endswith("()") else "code.variable"
    if node.get("_callable"):
        return "code.function"
    # A name with no kind and no source: an unresolved import or call target.
    # A person writes KB nodes, so they are never unresolved.
    if not node.get("source_file") and not node.get("source_location") and node.get("_origin") != "kb":
        return "code.unresolved"
    return "code"


@dataclass
class Ontology:
    """Classes and relation types, keyed by name."""

    classes: dict[str, ClassSpec] = field(default_factory=dict)
    relations: dict[str, RelationSpec] = field(default_factory=dict)
    # Node attribute that names a node's class when it has no ``class`` key.
    # "file_type" means graphify's taxonomy (see ``taxonomy_class``).
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
        # Version 1 had no rule origins, and ``kb infer --save`` wrote its
        # rules from the data. Load them as inferred, so they never give errors.
        version = data.get("version")
        origin = "inferred" if isinstance(version, int) and version < 2 else "declared"
        for raw in _as_list(data.get("classes")):
            spec = class_from_dict(raw, default_origin=origin)
            onto.classes[spec.name] = spec
        for raw in _as_list(data.get("relations")):
            spec = relation_from_dict(raw, default_origin=origin)
            onto.relations[spec.name] = spec
        return onto

    # -- queries -------------------------------------------------------
    def node_class(self, node: dict[str, Any], *, method_targets: set[str] | None = None, method_owners: set[str] | None = None) -> str | None:
        """Return the class of ``node``: the explicit ``class`` key wins.

        Otherwise the class comes from ``class_key``. For the default
        ``file_type`` it is the most specific class of graphify's taxonomy.
        Pass ``method_targets`` and ``method_owners`` (see the module
        functions) to find methods and their classes by the ``method`` edge;
        without them the label shape decides.
        """
        value = node.get("class")
        if value not in (None, ""):
            return str(value)
        if self.class_key != "file_type":
            value = node.get(self.class_key)
            return str(value) if value not in (None, "") else None
        return taxonomy_class(node, method_targets, method_owners)

    def ancestors(self, name: str) -> list[str]:
        """Return ``name`` and its parent chain, nearest first (cycle-safe)."""
        chain: list[str] = []
        current: str | None = name
        while current and current not in chain:
            chain.append(current)
            spec = self.classes.get(current)
            current = spec.parent if spec else _taxonomy_parent(current)
        return chain

    def is_a(self, name: str | None, target: str) -> bool:
        return bool(name) and target in self.ancestors(name)  # type: ignore[arg-type]

    def knows(self, name: str) -> bool:
        """Return True for a class of the ontology or of graphify's taxonomy."""
        return name in self.classes or (self.class_key == "file_type" and name in TAXONOMY)

    def root(self, name: str) -> str:
        return self.ancestors(name)[-1]

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

    def edge_shapes_for(self, name: str) -> list[EdgeShapeSpec]:
        """Return the edge shapes of a class and its ancestors (nearest wins)."""
        seen: dict[tuple[str, str], EdgeShapeSpec] = {}
        for cls_name in self.ancestors(name):
            spec = self.classes.get(cls_name)
            if not spec:
                continue
            for shape in spec.edges:
                seen.setdefault((shape.relation, shape.direction), shape)
        return list(seen.values())

    def check_edge(self, source_class: str | None, relation: str, target_class: str | None) -> list[dict[str, Any]]:
        """Return the findings for one edge from its end classes and relation.

        The severities are the same as in ``validate_graph``. The LLM ingest
        uses this as a conformance gate before it writes an edge.
        """
        out: list[dict[str, Any]] = []
        unknown_sev = "error" if self.strict else "warning"
        for end_cls in (source_class, target_class):
            if end_cls and not self.knows(end_cls):
                out.append(_finding(unknown_sev, "unknown_class", f"Class {end_cls} is not in the ontology.", ontology_class=end_cls))
        spec = self.relations.get(relation)
        if not relation:
            out.append(_finding("error", "missing_relation", "The edge has no relation."))
            return out
        if spec is None:
            out.append(_finding(unknown_sev, "unknown_relation", f"Relation {relation} is not in the ontology.", ontology_relation=relation))
            return out
        for end, cls_name in (("domain", source_class), ("range", target_class)):
            item = self._check_end(spec, end, cls_name)
            if item:
                out.append(item)
        return out

    def _check_end(self, spec: RelationSpec, end: str, cls_name: str | None) -> dict[str, Any] | None:
        allowed = spec.domain if end == "domain" else spec.range
        verb, must = ("starts at", "start at") if end == "domain" else ("ends at", "end at")
        shown = cls_name or "an unclassified node"
        if spec.origin == "declared":
            if allowed and not _matches(cls_name, allowed, self):
                return _finding(
                    spec.rule_severity(end),
                    f"{end}_violation",
                    f"{spec.name} {verb} {shown}; it must {must} {', '.join(allowed)}.",
                    ontology_relation=spec.name,
                    rule_origin="declared",
                )
            return None
        # An inferred relation: open below the support floor, and quiet for a
        # class inside the mined domain or range (QSE). Outside it, the score
        # p = P(class | relation, end) sets the severity (SDValidate).
        if not spec.support or spec.support < MIN_RELATION_SUPPORT or not allowed:
            return None
        if _matches(cls_name, allowed, self):
            return None
        profile = spec.domain_profile if end == "domain" else spec.range_profile
        p = profile.get(cls_name or "", 0) / spec.support
        if p >= RARE_SCORE:
            return None
        severity = spec.severity.get(end) or ("warning" if p < UNLIKELY_SCORE else "info")
        return _finding(
            severity,
            f"unlikely_{end}",
            f"{spec.name} {verb} {shown}, which {p:.1%} of its {spec.support} edges do; most use {', '.join(allowed)}.",
            ontology_relation=spec.name,
            rule_origin="inferred",
            score=round(p, 6),
            support=spec.support,
        )

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
            for shape in spec.edges:
                if shape.relation not in self.relations:
                    out.append(_finding("warning", "unknown_shape_relation", f"Class {name} has an edge shape on an unknown relation {shape.relation}.", ontology_class=name))
        for name, rel in self.relations.items():
            for cls_name in list(rel.domain) + list(rel.range):
                if cls_name not in self.classes:
                    out.append(_finding("warning", "unknown_relation_class", f"Relation {name} names an unknown class {cls_name}.", ontology_relation=name))
            if rel.inverse and rel.inverse not in self.relations:
                out.append(_finding("warning", "unknown_inverse", f"Relation {name} has an unknown inverse {rel.inverse}.", ontology_relation=name))
        return out


def _taxonomy_parent(name: str) -> str | None:
    entry = TAXONOMY.get(name)
    return entry[0] if entry else None


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


def _choice(raw: dict[str, Any], key: str, allowed: Iterable[str], default: str) -> str:
    value = str(raw.get(key) or default)
    allowed = tuple(allowed)
    if value not in allowed:
        raise OntologyError(f"unknown {key} {value!r}; use one of {', '.join(allowed)}")
    return value


def _severity(raw: dict[str, Any]) -> str | None:
    value = raw.get("severity")
    if value in (None, ""):
        return None
    if value not in SEVERITIES:
        raise OntologyError(f"unknown severity {value!r}; use one of {', '.join(SEVERITIES)}")
    return str(value)


def _stat_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _stat_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _profile(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(k): int(v) for k, v in value.items() if isinstance(v, int) and not isinstance(v, bool)}


def class_from_dict(raw: Any, *, default_origin: str = "declared") -> ClassSpec:
    if not isinstance(raw, dict):
        raise OntologyError("a class must be an object")
    props = []
    for p in raw.get("properties") or []:
        if isinstance(p, str):
            p = {"name": p}
        if not isinstance(p, dict):
            raise OntologyError("a property must be an object or a name")
        props.append(
            PropertySpec(
                name=check_name(p.get("name"), "property"),
                type=_choice(p, "type", PROPERTY_TYPES, "string"),
                required=bool(p.get("required", False)),
                description=str(p.get("description") or ""),
                origin=_choice(p, "origin", ORIGINS, default_origin),
                severity=_severity(p),
                support=_stat_int(p.get("support")),
                confidence=_stat_float(p.get("confidence")),
            )
        )
    shapes = []
    for s in raw.get("edges") or []:
        if not isinstance(s, dict):
            raise OntologyError("an edge shape must be an object")
        min_count = s.get("min_count", 1)
        if not isinstance(min_count, int) or isinstance(min_count, bool) or min_count < 0:
            raise OntologyError("an edge shape needs a min_count of 0 or more")
        shapes.append(
            EdgeShapeSpec(
                relation=check_name(s.get("relation"), "relation"),
                direction=_choice(s, "direction", DIRECTIONS, "out"),
                min_count=min_count,
                origin=_choice(s, "origin", ORIGINS, default_origin),
                severity=_severity(s),
                support=_stat_int(s.get("support")),
                confidence=_stat_float(s.get("confidence")),
                description=str(s.get("description") or ""),
            )
        )
    parent = raw.get("parent")
    return ClassSpec(
        name=check_name(raw.get("name"), "class"),
        parent=str(parent) if parent else None,
        description=str(raw.get("description") or ""),
        color=str(raw["color"]) if raw.get("color") else None,
        properties=props,
        edges=shapes,
        origin=_choice(raw, "origin", ORIGINS, default_origin),
        support=_stat_int(raw.get("support")),
    )


def relation_from_dict(raw: Any, *, default_origin: str = "declared") -> RelationSpec:
    if not isinstance(raw, dict):
        raise OntologyError("a relation must be an object")

    def names(value: Any) -> list[str]:
        if value in (None, "", []):
            return []
        if isinstance(value, str):
            value = [value]
        return [check_name(v, "class") for v in value]

    severity = raw.get("severity") or {}
    if not isinstance(severity, dict):
        raise OntologyError("a relation severity must be an object of rule names")
    for rule, value in severity.items():
        if rule not in RELATION_RULES:
            raise OntologyError(f"unknown rule {rule!r}; use one of {', '.join(RELATION_RULES)}")
        if value not in SEVERITIES:
            raise OntologyError(f"unknown severity {value!r}; use one of {', '.join(SEVERITIES)}")
    inverse = raw.get("inverse")
    return RelationSpec(
        name=check_name(raw.get("name"), "relation"),
        domain=names(raw.get("domain")),
        range=names(raw.get("range")),
        cardinality=_choice(raw, "cardinality", CARDINALITIES, "many"),
        in_cardinality=_choice(raw, "in_cardinality", CARDINALITIES, "many"),
        symmetric=bool(raw.get("symmetric", False)),
        acyclic=bool(raw.get("acyclic", False)),
        inverse=str(inverse) if inverse else None,
        description=str(raw.get("description") or ""),
        origin=_choice(raw, "origin", ORIGINS, default_origin),
        severity={str(k): str(v) for k, v in severity.items()},
        support=_stat_int(raw.get("support")),
        domain_profile=_profile(raw.get("domain_profile")),
        range_profile=_profile(raw.get("range_profile")),
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


def _is_property(key: str, value: Any) -> bool:
    return key not in RESERVED_NODE_KEYS and not key.startswith("_") and value not in (None, "", [])


def _min_count_severity(confidence: float, support: int) -> str | None:
    """Return the severity of an inferred minCount 1, or None for no rule (R3)."""
    if support < MIN_SHAPE_SUPPORT:
        return None
    if confidence >= REQUIRED_CONFIDENCE:
        return "warning"
    if confidence >= OPTIONAL_CONFIDENCE:
        return "info"
    return None


def _induce_end(nodes_at_end: Counter[str], edges_at_end: Counter[str], members: Counter[str], total: int, onto: Ontology) -> list[str]:
    """Return the classes of one relation end.

    QSE (PVLDB 16(5), 2023, Def. 2.4/2.5) counts entities: the support of class
    C is the number of distinct C nodes at this end. A class qualifies at
    ``MIN_CLASS_SUPPORT`` nodes when one of two confidences reaches
    ``MIN_CLASS_CONFIDENCE``:

    - the QSE confidence: the support divided by the instances of C;
    - the SDType share: the edges at this end that have class C, divided by
      all edges of the relation. A small part of a big class can do most of
      the work of a relation (``uses`` from functions).

    The OR of the two is our combination of QSE and SDType, not a rule of
    either paper. A class with an SDType share of ``RARE_SCORE`` or more also
    qualifies below ``MIN_CLASS_SUPPORT`` nodes: the edge-end score would not
    flag it anyway (``dynamic_import`` ends at a few modules).

    Two or more qualifying classes under one parent roll up to the parent when
    they cover ``ROLLUP_COVERAGE`` of the parent's nodes at this end.
    """
    if total < MIN_RELATION_SUPPORT:
        return []
    chosen = {
        c
        for c, n in nodes_at_end.items()
        if c
        and (
            (n >= MIN_CLASS_SUPPORT and (n >= MIN_CLASS_CONFIDENCE * members[c] or edges_at_end[c] >= MIN_CLASS_CONFIDENCE * total))
            or edges_at_end[c] >= RARE_SCORE * total
        )
    }
    inclusive: Counter[str] = Counter()
    for c, n in nodes_at_end.items():
        for a in onto.ancestors(c):
            inclusive[a] += n
    while True:
        groups: dict[str, set[str]] = defaultdict(set)
        for c in chosen:
            chain = onto.ancestors(c)
            if len(chain) > 1:
                groups[chain[1]].add(c)
        # The deepest parent first, so a roll-up climbs one level at a time.
        for parent in sorted(groups, key=lambda p: (-len(onto.ancestors(p)), p)):
            group = groups[parent] | ({parent} & chosen)
            if len(group) >= 2 and sum(inclusive[c] for c in group) >= ROLLUP_COVERAGE * inclusive[parent]:
                chosen = (chosen - group) | {parent}
                break
        else:
            break
    return sorted(c for c in chosen if not any(o != c and onto.is_a(c, o) for o in chosen))


def infer_ontology(graph: dict[str, Any], *, class_key: str = "file_type") -> Ontology:
    """Derive an ontology from the classes and relations in ``graph``.

    With the default ``class_key`` the classes are graphify's taxonomy: every
    root, and each subclass that has nodes. Every rule is ``inferred``:

    - a property is required (minCount 1) at ``REQUIRED_CONFIDENCE`` as a
      warning, and at ``OPTIONAL_CONFIDENCE`` as an info;
    - an edge shape (at least one edge of a relation) uses the same bands,
      only for the structural relations of ``SHAPE_RELATIONS``;
    - a relation gets a domain and a range from QSE support and confidence
      over distinct nodes, and the edge count per class at each end for the
      edge-end score;
    - a relation gets cardinality "one" when no node has two such edges.

    ``contains`` and ``inherits`` are acyclic in every language that graphify
    parses, so a cycle there is an error.
    """
    onto = Ontology(class_key=class_key, description="Inferred from the current graph.")
    nodes = graph.get("nodes", [])
    links = _links(graph)
    targets = method_targets(graph) if class_key == "file_type" else None
    owners = method_owners(graph) if class_key == "file_type" else None
    cls_of: dict[str, str | None] = {}
    members: Counter[str] = Counter()
    for node in nodes:
        cls_name = onto.node_class(node, method_targets=targets, method_owners=owners)
        cls_of[str(node.get("id"))] = cls_name
        if cls_name:
            members[cls_name] += 1
    _infer_classes(onto, members)
    inclusive: Counter[str] = Counter()
    for c, n in members.items():
        for a in onto.ancestors(c):
            inclusive[a] += n
    _infer_properties(onto, nodes, cls_of, inclusive)
    _infer_relations(onto, links, cls_of, members)
    _infer_edge_shapes(onto, links, cls_of, inclusive)
    return onto


def _infer_classes(onto: Ontology, members: Counter[str]) -> None:
    if onto.class_key == "file_type":
        used = {a for c in members for a in onto.ancestors(c)}
        for name, (parent, description) in TAXONOMY.items():
            if parent is None or name in used:
                onto.classes[name] = ClassSpec(name, parent=parent, description=description, origin="inferred", support=members.get(name, 0))
    for name in sorted(members):
        if name in onto.classes:
            continue
        try:
            onto.classes[name] = ClassSpec(check_name(name, "class"), origin="inferred", support=members[name])
        except OntologyError:
            continue


def _infer_properties(onto: Ontology, nodes: list[dict[str, Any]], cls_of: dict[str, str | None], inclusive: Counter[str]) -> None:
    seen: dict[str, Counter[str]] = defaultdict(Counter)
    types: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for node in nodes:
        cls_name = cls_of.get(str(node.get("id")))
        if not cls_name or cls_name not in onto.classes:
            continue
        keys = [(k, v) for k, v in node.items() if _is_property(k, v)]
        if not keys:
            continue
        for a in onto.ancestors(cls_name):
            for key, value in keys:
                seen[a][key] += 1
                types[(a, key)][value_type(value)] += 1
    # Roots first, so a property sits on the highest class that has it.
    for name in sorted(onto.classes, key=lambda c: (len(onto.ancestors(c)), c)):
        spec = onto.classes[name]
        inherited = {p.name: p for p in onto.properties_for(name)}
        for key, count in sorted(seen[name].items()):
            confidence = count / inclusive[name] if inclusive[name] else 0.0
            severity = _min_count_severity(confidence, inclusive[name])
            have = inherited.get(key)
            if have and (have.severity == severity or not severity):
                continue
            ptype, agree = types[(name, key)].most_common(1)[0]
            spec.properties.append(
                PropertySpec(
                    name=key,
                    type=ptype if agree >= TYPE_AGREEMENT * count else "any",
                    required=severity is not None,
                    origin="inferred",
                    severity=severity,
                    support=count,
                    confidence=round(confidence, 4),
                )
            )


def _infer_relations(onto: Ontology, links: list[dict[str, Any]], cls_of: dict[str, str | None], members: Counter[str]) -> None:
    totals: Counter[str] = Counter()
    domains: dict[str, Counter[str]] = defaultdict(Counter)
    ranges: dict[str, Counter[str]] = defaultdict(Counter)
    out_n: Counter[tuple[str, str]] = Counter()
    in_n: Counter[tuple[str, str]] = Counter()
    for link in links:
        rel = str(link.get("relation") or "")
        s, t = str(link.get("source")), str(link.get("target"))
        if not rel or s not in cls_of or t not in cls_of:
            continue
        totals[rel] += 1
        domains[rel][cls_of[s] or ""] += 1
        ranges[rel][cls_of[t] or ""] += 1
        out_n[(s, rel)] += 1
        in_n[(t, rel)] += 1
    # Distinct nodes per relation end and class, for the QSE support.
    src_nodes: dict[str, Counter[str]] = defaultdict(Counter)
    dst_nodes: dict[str, Counter[str]] = defaultdict(Counter)
    max_out: Counter[str] = Counter()
    max_in: Counter[str] = Counter()
    for (s, rel), n in out_n.items():
        src_nodes[rel][cls_of[s] or ""] += 1
        max_out[rel] = max(max_out[rel], n)
    for (t, rel), n in in_n.items():
        dst_nodes[rel][cls_of[t] or ""] += 1
        max_in[rel] = max(max_in[rel], n)
    for rel in sorted(totals):
        try:
            name = check_name(rel, "relation")
        except OntologyError:
            continue
        total = totals[rel]
        # QSE adds sh:maxCount 1 when every instance has at most one value.
        big = total >= MAX_CARDINALITY_SUPPORT
        acyclic = rel in ACYCLIC_RELATIONS
        onto.relations[name] = RelationSpec(
            name=name,
            domain=_induce_end(src_nodes[rel], domains[rel], members, total, onto),
            range=_induce_end(dst_nodes[rel], ranges[rel], members, total, onto),
            cardinality="one" if big and max_out[rel] <= 1 else "many",
            in_cardinality="one" if big and max_in[rel] <= 1 else "many",
            acyclic=acyclic,
            origin="inferred",
            severity={"acyclic": "error"} if acyclic else {},
            support=total,
            domain_profile={c: n for c, n in sorted(domains[rel].items()) if c},
            range_profile={c: n for c, n in sorted(ranges[rel].items()) if c},
        )


def _infer_edge_shapes(onto: Ontology, links: list[dict[str, Any]], cls_of: dict[str, str | None], inclusive: Counter[str]) -> None:
    has: dict[tuple[str, str], set[str]] = defaultdict(set)
    for link in links:
        rel = str(link.get("relation") or "")
        s, t = str(link.get("source")), str(link.get("target"))
        if rel in onto.relations and s in cls_of and t in cls_of:
            if (rel, "out") in SHAPE_RELATIONS:
                has[(rel, "out")].add(s)
            if (rel, "in") in SHAPE_RELATIONS:
                has[(rel, "in")].add(t)
    counts: Counter[tuple[str, str, str]] = Counter()
    for (rel, direction), node_ids in has.items():
        for nid in node_ids:
            cls_name = cls_of.get(nid)
            if cls_name and cls_name in onto.classes:
                for a in onto.ancestors(cls_name):
                    counts[(a, rel, direction)] += 1
    for name in sorted(onto.classes, key=lambda c: (len(onto.ancestors(c)), c)):
        spec = onto.classes[name]
        inherited = {(s.relation, s.direction): s for s in onto.edge_shapes_for(name)}
        for (cls_name, rel, direction), count in sorted(counts.items()):
            # A method is the target of a method edge by definition.
            if cls_name != name or (name == "code.method" and rel == "method" and direction == "in"):
                continue
            confidence = count / inclusive[name] if inclusive[name] else 0.0
            severity = _min_count_severity(confidence, inclusive[name])
            have = inherited.get((rel, direction))
            if not severity or (have and have.severity == severity):
                continue
            spec.edges.append(
                EdgeShapeSpec(
                    relation=rel,
                    direction=direction,
                    origin="inferred",
                    severity=severity,
                    support=count,
                    confidence=round(confidence, 4),
                )
            )


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


def graph_norm_label(label: Any) -> str:
    """Return graphify's ``norm_label`` for a label."""
    return _strip_diacritics(str(label or "")).lower()


def _compatible(onto: Ontology, a: str | None, b: str | None) -> bool:
    """Two classes can name the same entity: one is a subclass of the other,
    or one is a concept and the other is code or a concept."""
    if not a or not b:
        return False
    if onto.is_a(a, b) or onto.is_a(b, a):
        return True
    roots = {onto.root(a), onto.root(b)}
    return "concept" in roots and roots <= {"concept", "code"}


def _candidate(onto: Ontology, node: dict[str, Any], cls_name: str | None) -> bool:
    """Return True when ``node`` can be the entity that a KB node names."""
    if not cls_name or onto.is_a(cls_name, "code.variable") or onto.is_a(cls_name, "code.test"):
        return False
    return not _is_test_path(str(node.get("source_file") or ""))


def _confidence_findings(link: dict[str, Any], where: dict[str, Any]) -> list[dict[str, Any]]:
    """Check an edge's confidence and confidence_score against graphify's rubric."""
    conf = link.get("confidence")
    score = link.get("confidence_score")
    out: list[dict[str, Any]] = []
    if conf not in (None, "") and conf not in VALID_CONFIDENCES:
        out.append(_finding("warning", "invalid_confidence", f"Confidence {conf} is not one of {', '.join(sorted(VALID_CONFIDENCES))}.", **where))
        return out
    if score is None:
        return out
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0.0 <= score <= 1.0:
        out.append(_finding("warning", "invalid_confidence", f"Confidence score {score!r} is not a number from 0 to 1.", **where))
        return out
    low, high = AMBIGUOUS_SCORE_RANGE
    if conf == "EXTRACTED" and score != 1.0:
        out.append(_finding("warning", "confidence_mismatch", f"An EXTRACTED edge scores 1.0, not {score}.", **where))
    elif conf == "AMBIGUOUS" and not low <= score <= high:
        out.append(_finding("warning", "confidence_mismatch", f"An AMBIGUOUS edge scores {low} to {high}, not {score}.", **where))
    elif conf == "INFERRED":
        # The spec names 0.5 as the one default that is never allowed, and
        # an edge at 0.4 or below must be AMBIGUOUS.
        if not INFERRED_FLOOR < score < 1.0 or score == 0.5:
            out.append(_finding("warning", "confidence_mismatch", f"An INFERRED edge scores above {INFERRED_FLOOR} and below 1.0, and never 0.5; not {score}. Mark it AMBIGUOUS or pick a rubric value.", **where))
        elif round(score, 2) not in INFERRED_SCORES:
            scores = ", ".join(str(s) for s in sorted(INFERRED_SCORES))
            out.append(_finding("info", "confidence_mismatch", f"An INFERRED edge uses one of {scores}, not {score}.", **where))
    return out


def _cycles(edges: list[tuple[str, str]], limit: int = 50) -> list[list[str]]:
    """Return up to ``limit`` cycles of a directed edge list (iterative DFS)."""
    adj: dict[str, list[str]] = defaultdict(list)
    for s, t in edges:
        adj[s].append(t)
    state: dict[str, int] = {}  # 1 on the stack, 2 done
    found: list[list[str]] = []
    for start in sorted(adj):
        if start in state:
            continue
        path = [start]
        stack = [iter(adj[start])]
        state[start] = 1
        while stack and len(found) < limit:
            nxt = next(stack[-1], None)
            if nxt is None:
                state[path.pop()] = 2
                stack.pop()
                continue
            mark = state.get(nxt)
            if mark == 1:
                found.append(path[path.index(nxt):] + [nxt])
            elif mark is None:
                state[nxt] = 1
                path.append(nxt)
                stack.append(iter(adj.get(nxt, ())))
    return found


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
    Inferred node shapes apply to extracted nodes only: the ontology was mined
    from them. A knowledge-base node meets the declared shapes.
    """
    findings: list[dict[str, Any]] = list(onto.problems())
    unknown_sev = "error" if onto.strict else "warning"
    nodes = graph.get("nodes", [])
    links = _links(graph)
    targets = method_targets(graph) if onto.class_key == "file_type" else None
    owners = method_owners(graph) if onto.class_key == "file_type" else None
    ids: dict[str, dict[str, Any]] = {}
    cls_of: dict[str, str | None] = {}
    raw_cls: dict[str, str | None] = {}
    kb_ids: set[str] = set()
    for node in nodes:
        nid = str(node.get("id"))
        if nid in ids:
            findings.append(_finding("error", "duplicate_id", f"Node id {nid} appears more than once.", node=nid))
        ids[nid] = node
        if node.get("_origin") == "kb":
            kb_ids.add(nid)
        cls_name = onto.node_class(node, method_targets=targets, method_owners=owners)
        raw_cls[nid] = cls_name
        label = str(node.get("label") or "")
        if not label:
            findings.append(_finding("warning", "missing_label", f"Node {nid} has no label.", node=nid))
        if not cls_name:
            findings.append(_finding(unknown_sev, "missing_class", f"Node {label or nid} has no class.", node=nid))
            cls_of[nid] = None
            continue
        if not onto.knows(cls_name):
            findings.append(_finding(unknown_sev, "unknown_class", f"Node {label or nid} uses class {cls_name}, which the ontology does not define.", node=nid, ontology_class=cls_name))
            cls_of[nid] = None
            continue
        cls_of[nid] = cls_name
        findings.extend(_property_findings(onto, node, nid, cls_name, label, nid in kb_ids))
    degree: Counter[str] = Counter()
    out_n: Counter[tuple[str, str]] = Counter()
    in_n: Counter[tuple[str, str]] = Counter()
    kb_neighbours: dict[str, set[str]] = defaultdict(set)
    acyclic_edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    pair_seen: set[tuple[str, str, str]] = set()
    for link in links:
        s, t = str(link.get("source")), str(link.get("target"))
        rel = str(link.get("relation") or "")
        where = {"edge": {"source": s, "target": t, "relation": rel}}
        degree[s] += 1
        degree[t] += 1
        if s not in ids or t not in ids:
            missing = s if s not in ids else t
            findings.append(_finding("error", "dangling_edge", f"Edge {rel} points to a missing node {missing}.", **where))
            continue
        if s in kb_ids:
            kb_neighbours[s].add(t)
        if t in kb_ids:
            kb_neighbours[t].add(s)
        score = link.get("confidence_score")
        # An EXTRACTED edge at 1.0 or without a score has nothing to report.
        if link.get("confidence") != "EXTRACTED" or not (score is None or (type(score) is float and score == 1.0)):
            findings.extend(_confidence_findings(link, where))
        if not rel:
            findings.append(_finding("error", "missing_relation", f"Edge {s} to {t} has no relation.", **where))
            continue
        out_n[(s, rel)] += 1
        in_n[(t, rel)] += 1
        spec = onto.relations.get(rel)
        if spec is None:
            findings.append(_finding(unknown_sev, "unknown_relation", f"Relation {rel} is not in the ontology.", ontology_relation=rel, **where))
            continue
        for end, node_id in (("domain", s), ("range", t)):
            item = onto._check_end(spec, end, raw_cls[node_id])
            if item:
                item.update(node=node_id, **where)
                findings.append(item)
        if spec.acyclic:
            acyclic_edges[rel].append((s, t))
        if spec.symmetric:
            pair_seen.add((s, t, rel))
    findings.extend(_cardinality_findings(onto, ids, out_n, in_n))
    findings.extend(_edge_shape_findings(onto, ids, cls_of, kb_ids, out_n, in_n))
    for rel, edges in sorted(acyclic_edges.items()):
        spec = onto.relations[rel]
        for cycle in _cycles(edges):
            findings.append(_finding(spec.rule_severity("acyclic"), "cycle", f"{rel} edges form a cycle: {' -> '.join(cycle)}.", node=cycle[0], nodes=cycle, ontology_relation=rel))
    for s, t, rel in pair_seen:
        if (t, s, rel) not in pair_seen and s != t:
            findings.append(_finding("info", "asymmetric_edge", f"{rel} is symmetric, but only {s} to {t} exists.", node=s, ontology_relation=rel, edge={"source": s, "target": t, "relation": rel}))
    findings.extend(_duplicate_findings(onto, ids, cls_of, kb_ids, kb_neighbours))
    for nid in sorted(kb_ids):
        if degree[nid] == 0:
            findings.append(_finding("info", "orphan_node", f"{ids[nid].get('label') or nid} has no edges.", node=nid))
    if not include_info:
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
        "edges": len(links),
        "findings": findings[:limit],
        "truncated": len(findings) > limit,
        "total": len(findings),
    }


def _property_findings(onto: Ontology, node: dict[str, Any], nid: str, cls_name: str, label: str, is_kb: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for prop in onto.properties_for(cls_name):
        if prop.origin == "inferred" and is_kb:
            continue
        value = node.get(prop.name)
        stats = {"rule_origin": prop.origin, "support": prop.support, "confidence": prop.confidence}
        if value in (None, "", []):
            if prop.required:
                out.append(_finding(prop.severity or _origin_severity(prop.origin), "missing_property", f"{cls_name} {label or nid} has no {prop.name}.", node=nid, property=prop.name, ontology_class=cls_name, **stats))
            continue
        # Only a declared type is a rule; an inferred type describes the data.
        if prop.origin == "declared" and not _type_ok(value, prop.type):
            out.append(_finding(prop.severity or "error", "property_type", f"{cls_name} {label or nid}: {prop.name} must be {prop.type}, not {value_type(value)}.", node=nid, property=prop.name, ontology_class=cls_name, **stats))
    return out


def _cardinality_findings(onto: Ontology, ids: dict[str, dict[str, Any]], out_n: Counter[tuple[str, str]], in_n: Counter[tuple[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for (s, rel), count in out_n.items():
        spec = onto.relations.get(rel)
        if spec and spec.cardinality == "one" and count > 1:
            out.append(_finding(spec.rule_severity("cardinality"), "cardinality", f"{ids[s].get('label') or s} has {count} outgoing {rel} edges; the ontology allows one.", node=s, ontology_relation=rel, rule_origin=spec.origin))
    for (t, rel), count in in_n.items():
        spec = onto.relations.get(rel)
        if spec and spec.in_cardinality == "one" and count > 1:
            out.append(_finding(spec.rule_severity("in_cardinality"), "in_cardinality", f"{ids[t].get('label') or t} has {count} incoming {rel} edges; the ontology allows one.", node=t, ontology_relation=rel, rule_origin=spec.origin))
    return out


def _edge_shape_findings(onto: Ontology, ids: dict[str, dict[str, Any]], cls_of: dict[str, str | None], kb_ids: set[str], out_n: Counter[tuple[str, str]], in_n: Counter[tuple[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    shapes_of: dict[str, list[EdgeShapeSpec]] = {}
    # A shape on a relation also counts the edges of its kinds (RELATION_FAMILY).
    kinds: dict[str, list[str]] = defaultdict(list)
    for child, parent in RELATION_FAMILY.items():
        kinds[parent].append(child)
    for nid, cls_name in cls_of.items():
        if not cls_name:
            continue
        if cls_name not in shapes_of:
            shapes_of[cls_name] = onto.edge_shapes_for(cls_name)
        for shape in shapes_of[cls_name]:
            if shape.origin == "inferred" and nid in kb_ids:
                continue
            counts = out_n if shape.direction == "out" else in_n
            have = counts[(nid, shape.relation)] + sum(counts[(nid, k)] for k in kinds.get(shape.relation, ()))
            if have < shape.min_count:
                way = "outgoing" if shape.direction == "out" else "incoming"
                out.append(
                    _finding(
                        shape.severity or _origin_severity(shape.origin),
                        "missing_edge",
                        f"{cls_name} {ids[nid].get('label') or nid} has {have} {way} {shape.relation} edges; the ontology expects at least {shape.min_count}.",
                        node=nid,
                        ontology_class=cls_name,
                        ontology_relation=shape.relation,
                        direction=shape.direction,
                        rule_origin=shape.origin,
                        support=shape.support,
                        confidence=shape.confidence,
                    )
                )
    return out


def _duplicate_findings(onto: Ontology, ids: dict[str, dict[str, Any]], cls_of: dict[str, str | None], kb_ids: set[str], kb_neighbours: dict[str, set[str]]) -> list[dict[str, Any]]:
    """Report a knowledge-base node that names the same entity as another node.

    The match is graphify's norm_label with a compatible class. Two nodes that
    an edge already joins are linked, not duplicated. Variables, tests and
    nodes in test files are never candidates: a local name in a test is not
    the entity that a person describes. One candidate gives a warning; more
    candidates give an info with the list, as the name alone is weak evidence.
    """
    if not kb_ids:
        return []
    norms = {nid: graph_norm_label(node.get("label")) for nid, node in ids.items()}
    wanted = {norms[nid] for nid in kb_ids} - {""}
    by_norm: dict[str, list[str]] = defaultdict(list)
    for nid, key in norms.items():
        if key in wanted:
            by_norm[key].append(nid)
    out: list[dict[str, Any]] = []
    reported: set[frozenset[str]] = set()
    for nid in sorted(kb_ids):
        key = norms[nid]
        others = [o for o in by_norm.get(key, ()) if o != nid and o not in kb_neighbours[nid] and _candidate(onto, ids[o], cls_of[o]) and _compatible(onto, cls_of[nid], cls_of[o])]
        group = frozenset([nid, *others])
        if not others or group in reported:
            continue
        reported.add(group)
        label, cls_name = ids[nid].get("label"), cls_of[nid]
        if len(others) == 1:
            severity = "warning"
            message = f"{label} ({cls_name}) has the same name as {others[0]} ({cls_of[others[0]]}). Link or merge them."
        else:
            severity = "info"
            shown = ", ".join(f"{o} ({cls_of[o]})" for o in others[:5])
            more = f" and {len(others) - 5} more" if len(others) > 5 else ""
            message = f"{label} ({cls_name}) has the same name as {len(others)} nodes: {shown}{more}. Link it to the one that it means."
        out.append(_finding(severity, "duplicate_entity", message, node=nid, nodes=[nid, *others[:20]], candidates=len(others), ontology_class=cls_name))
    return out


def nonconforming_extraction(item: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Wrap the findings of one extracted node or edge into one finding.

    The LLM ingest calls ``Ontology.check_edge`` for each extracted edge and
    passes the result here. Return None when no finding is a warning or an
    error. The new finding has the worst severity and keeps the details.
    """
    serious = [f for f in findings if f.get("severity") in ("error", "warning")]
    if not serious:
        return None
    severity = "error" if any(f["severity"] == "error" for f in serious) else "warning"
    codes = ", ".join(sorted({str(f.get("code")) for f in serious}))
    where: dict[str, Any] = {}
    if "source" in item and "target" in item:
        where["edge"] = {"source": item.get("source"), "target": item.get("target"), "relation": item.get("relation")}
    elif item.get("id") is not None:
        where["node"] = str(item.get("id"))
    return _finding(severity, NONCONFORMING_EXTRACTION, f"The extraction does not fit the ontology: {codes}.", findings=serious, **where)


# The relation that a knowledge-base document writes to each node that it names.
MENTIONS = "mentions"


def with_kb_vocabulary(onto: Ontology) -> Ontology:
    """Add the classes and the relation that the knowledge base itself writes.

    It extends the ontology and never overrides a class or a relation that is
    already there. A KB document box is a ``document.page``; a KB concept is a
    ``concept``.
    """
    for name in ("document", "concept", "document.page"):
        if name not in onto.classes:
            parent, description = TAXONOMY[name]
            onto.classes[name] = ClassSpec(name, parent=parent, description=description)
    rel = onto.relations.get(MENTIONS)
    if rel is None:
        onto.relations[MENTIONS] = RelationSpec(MENTIONS, domain=["document"], description="A document names this node.")
    elif rel.domain and not _matches("document.page", rel.domain, onto):
        rel.domain.append("document")
    return onto
