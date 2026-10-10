"""The generic domain model that every plugin fills.

A plugin adds :class:`Element` values (for example commands, queries and
events), :class:`Relation` values between them, and :class:`Finding` values.
The model checks the references, traces flows and serializes itself. It has
no knowledge of a specific framework.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

ERROR = "error"
WARNING = "warning"
INFO = "info"
SEVERITIES = (ERROR, WARNING, INFO)


@dataclass(frozen=True, order=True)
class Ref:
    """The identity of an element: its kind, its service and its name."""

    kind: str
    service: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.service}:{self.name}"

    def __str__(self) -> str:
        return f"{self.service}/{self.kind}/{self.name}"


@dataclass
class Element:
    ref: Ref
    file: str | None = None
    line: int | None = None
    handler: str | None = None
    description: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    schemas: dict[str, dict] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    plugin: str | None = None

    @property
    def kind(self) -> str:
        return self.ref.kind

    @property
    def service(self) -> str:
        return self.ref.service

    @property
    def name(self) -> str:
        return self.ref.name

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ref"] = asdict(self.ref)
        d["id"] = self.ref.key
        return d


@dataclass
class Relation:
    """A directed link, such as "command emits event".

    ``required`` means that the target must exist in the model. The check
    reports a missing target as a finding.
    """

    source: Ref
    target: Ref
    relation: str
    file: str | None = None
    line: int | None = None
    required: bool = True
    attrs: dict[str, Any] = field(default_factory=dict)
    plugin: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source"] = self.source.key
        d["target"] = self.target.key
        return d


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    file: str | None = None
    line: int | None = None
    ref: Ref | None = None
    plugin: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ref"] = self.ref.key if self.ref else None
        return d


@dataclass(frozen=True)
class KindSpec:
    """How the report and the diagrams show one element kind."""

    label: str
    plural: str
    shape: str = "rect"  # rect, round, stadium, hexagon, parallelogram, subroutine, circle
    entry: bool = False  # a flow can start at this kind


DEFAULT_KIND = KindSpec("Element", "Elements")


class DomainModel:
    def __init__(self, root: str = "."):
        self.root = root
        self.elements: dict[Ref, Element] = {}
        self.relations: list[Relation] = []
        self.findings: list[Finding] = []
        self.kinds: dict[str, KindSpec] = {}
        self.services: dict[str, dict[str, Any]] = {}
        self.plugins: list[str] = []

    # -- building ------------------------------------------------------------
    def add_service(self, name: str, **attrs: Any) -> None:
        self.services.setdefault(name, {}).update({k: v for k, v in attrs.items() if v is not None})

    def add(self, element: Element) -> Element:
        """Add ``element``, or merge it into the element with the same ref."""
        self.add_service(element.service)
        current = self.elements.get(element.ref)
        if current is None:
            self.elements[element.ref] = element
            return element
        for name in ("file", "line", "handler", "description", "plugin"):
            if getattr(current, name) is None:
                setattr(current, name, getattr(element, name))
        for k, v in element.attrs.items():
            current.attrs.setdefault(k, v)
        current.schemas.update(element.schemas)
        current.sources.update(element.sources)
        return current

    def relate(self, relation: Relation) -> Relation:
        for r in self.relations:
            if (r.source, r.target, r.relation) == (relation.source, relation.target, relation.relation):
                return r
        self.relations.append(relation)
        return relation

    def report(self, finding: Finding) -> None:
        self.findings.append(finding)

    def kind(self, name: str) -> KindSpec:
        return self.kinds.get(name, KindSpec(name.replace("-", " ").capitalize(), name.replace("-", " ").capitalize() + "s"))

    # -- queries -------------------------------------------------------------
    def of_kind(self, kind: str, service: str | None = None) -> list[Element]:
        return sorted(
            (e for e in self.elements.values() if e.kind == kind and (service is None or e.service == service)),
            key=lambda e: (e.service, e.name),
        )

    def outgoing(self, ref: Ref) -> list[Relation]:
        return [r for r in self.relations if r.source == ref]

    def incoming(self, ref: Ref) -> list[Relation]:
        return [r for r in self.relations if r.target == ref]

    # -- checks --------------------------------------------------------------
    def check_references(self) -> None:
        """Report every required relation whose target does not exist."""
        for rel in self.relations:
            if not rel.required or rel.target in self.elements:
                continue
            spec = self.kind(rel.target.kind)
            where = f" in {rel.file}:{rel.line}" if rel.file else ""
            if rel.target.service not in self.services:
                self.report(Finding(
                    WARNING, "unknown-service",
                    f"{rel.source} {rel.relation} {spec.label.lower()} `{rel.target.name}` of service "
                    f"`{rel.target.service}`, which is not in this corpus{where}.",
                    rel.file, rel.line, rel.target, rel.plugin))
                continue
            self.report(Finding(
                ERROR, f"missing-{rel.target.kind}",
                f"{rel.source} {rel.relation} {spec.label.lower()} `{rel.target.name}`, but service "
                f"`{rel.target.service}` does not register it{where}.",
                rel.file, rel.line, rel.target, rel.plugin))

    # -- flows ---------------------------------------------------------------
    def flow(self, start: Ref, max_depth: int = 12) -> list[Relation]:
        """Return the relations that ``start`` reaches, in breadth-first order."""
        seen_refs = {start}
        out: list[Relation] = []
        queue = deque([(start, 0)])
        by_source: dict[Ref, list[Relation]] = defaultdict(list)
        for r in self.relations:
            by_source[r.source].append(r)
        while queue:
            ref, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for r in by_source.get(ref, []):
                out.append(r)
                if r.target not in seen_refs:
                    seen_refs.add(r.target)
                    queue.append((r.target, depth + 1))
        return out

    def entry_points(self) -> list[Element]:
        """Return elements of entry kinds that no other element triggers."""
        targeted = {r.target for r in self.relations}
        entries = [e for e in self.elements.values() if self.kind(e.kind).entry and e.ref not in targeted]
        return sorted(entries, key=lambda e: (e.service, e.kind, e.name))

    # -- output --------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        c: dict[str, int] = defaultdict(int)
        for e in self.elements.values():
            c[e.kind] += 1
        return dict(sorted(c.items()))

    def finding_counts(self) -> dict[str, int]:
        return {s: sum(1 for f in self.findings if f.severity == s) for s in SEVERITIES}

    def sorted_findings(self) -> list[Finding]:
        order = {s: i for i, s in enumerate(SEVERITIES)}
        return sorted(self.findings, key=lambda f: (order.get(f.severity, 9), f.code, f.file or "", f.line or 0, f.message))

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "plugins": self.plugins,
            "services": {k: self.services[k] for k in sorted(self.services)},
            "kinds": {k: asdict(v) for k, v in sorted(self.kinds.items())},
            "counts": self.counts(),
            "elements": [e.to_dict() for e in sorted(self.elements.values(), key=lambda e: e.ref)],
            "relations": [r.to_dict() for r in sorted(self.relations, key=lambda r: (r.source, r.relation, r.target))],
            "findings": [f.to_dict() for f in self.sorted_findings()],
        }


def unique(items: Iterable[Any]) -> list[Any]:
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out
