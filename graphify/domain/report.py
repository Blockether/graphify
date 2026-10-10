"""Render a :class:`DomainModel` as Markdown, Mermaid, JSON and JSON Schema files."""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from pathlib import Path

from graphify.domain.model import ERROR, INFO, WARNING, DomainModel, Element, Ref, Relation

SEVERITY_ICON = {ERROR: "🔴 error", WARNING: "🟠 warning", INFO: "🔵 info"}
_SHAPES = {
    "rect": ("[\"", "\"]"), "round": ("(\"", "\")"), "stadium": ("([\"", "\"])"),
    "hexagon": ("{{\"", "\"}}"), "parallelogram": ("[/\"", "\"/]"), "subroutine": ("[[\"", "\"]]"),
    "circle": ("((\"", "\"))"),
}
_EDGE_STYLE = {"emits": "-->", "triggers": "==>", "invokes": "==>", "reads": "-.->"}
MAX_OVERVIEW_NODES = 400


def _mid(prefix: str, text: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_]", "_", text)[:48]
    return f"{prefix}_{clean}_{hashlib.sha1(text.encode()).hexdigest()[:6]}"


def _label(text: str) -> str:
    return text.replace("\"", "#quot;").replace("<", "#lt;").replace(">", "#gt;")


def _anchor(text: str) -> str:
    return re.sub(r"[^a-z0-9 -]", "", text.lower()).strip().replace(" ", "-")


def schema_path(ref: Ref, part: str) -> str:
    safe = lambda s: re.sub(r"[^A-Za-z0-9._-]", "_", s)  # noqa: E731
    return f"schemas/{safe(ref.service)}/{safe(ref.kind)}.{safe(ref.name)}.{safe(part)}.json"


class _Diagram:
    """One Mermaid flowchart with service subgraphs."""

    def __init__(self, model: DomainModel, direction: str = "LR"):
        self.model = model
        self.direction = direction
        self.nodes: "OrderedDict[Ref, None]" = OrderedDict()
        self.edges: list[Relation] = []

    def add_relation(self, r: Relation) -> None:
        self.nodes.setdefault(r.source)
        self.nodes.setdefault(r.target)
        self.edges.append(r)

    def add_ref(self, ref: Ref) -> None:
        self.nodes.setdefault(ref)

    def render(self) -> str:
        m = self.model
        lines = [f"flowchart {self.direction}"]
        by_service: "OrderedDict[str, list[Ref]]" = OrderedDict()
        for ref in sorted(self.nodes):
            by_service.setdefault(ref.service, []).append(ref)
        missing, external = [], []
        for service, refs in by_service.items():
            lines.append(f"  subgraph {_mid('svc', service)}[\"{_label(service)}\"]")
            for ref in refs:
                spec = m.kind(ref.kind)
                left, right = _SHAPES.get(spec.shape, _SHAPES["rect"])
                text = _label(f"{spec.label}: {ref.name}" if ref.kind not in ("function",) else ref.name)
                lines.append(f"    {_mid('n', ref.key)}{left}{text}{right}")
                if ref not in m.elements:
                    (external if service not in m.services else missing).append(ref)
            lines.append("  end")
        for r in self.edges:
            arrow = _EDGE_STYLE.get(r.relation, "-->")
            lines.append(f"  {_mid('n', r.source.key)} {arrow}|{_label(r.relation)}| {_mid('n', r.target.key)}")
        lines.append("  classDef missing fill:#ffe0e0,stroke:#c62828,stroke-width:2px,stroke-dasharray:5 3")
        lines.append("  classDef external fill:#eeeeee,stroke:#757575,stroke-dasharray:3 3")
        if missing:
            lines.append("  class " + ",".join(_mid("n", r.key) for r in missing) + " missing")
        if external:
            lines.append("  class " + ",".join(_mid("n", r.key) for r in external) + " external")
        return "\n".join(lines)


def _sequence(model: DomainModel, start: Element, relations: list[Relation]) -> str:
    participants: "OrderedDict[str, None]" = OrderedDict()
    participants["Client"] = None
    participants[start.service] = None
    for r in relations:
        participants.setdefault(r.source.service)
        participants.setdefault(r.target.service)
    alias = {p: f"P{i}" for i, p in enumerate(participants)}
    lines = ["sequenceDiagram", "  autonumber"]
    for p in participants:
        lines.append(f"  participant {alias[p]} as {_label(p)}")
    label = model.kind(start.kind).label
    lines.append(f"  {alias['Client']}->>{alias[start.service]}: {_label(label)} {_label(start.name)}")
    for r in relations:
        target_spec = model.kind(r.target.kind)
        text = f"{r.source.name} {r.relation} {target_spec.label.lower()} {r.target.name}"
        if r.target not in model.elements and r.required:
            text += " (MISSING)" if r.target.service in model.services else " (unknown service)"
        arrow = "-->>" if r.relation == "reads" else "->>"
        lines.append(f"  {alias[r.source.service]}{arrow}{alias[r.target.service]}: {_label(text)}")
    return "\n".join(lines)


def _code(text: str, lang: str = "") -> list[str]:
    return [f"```{lang}", text, "```"]


def _loc(file: str | None, line: int | None) -> str:
    if not file:
        return ""
    return f"`{file}:{line}`" if line else f"`{file}`"


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _names(refs: list[Ref], current_service: str) -> str:
    out = []
    for r in refs:
        out.append(f"`{r.name}`" if r.service == current_service else f"`{r.service}/{r.name}`")
    return ", ".join(out) or "—"


def render_markdown(model: DomainModel) -> str:
    out: list[str] = []
    w = out.append
    counts = model.finding_counts()
    w("# Domain model")
    w("")
    w(f"Plugins: {', '.join(model.plugins) or 'none'}. Services: {len(model.services)}. "
      f"Findings: {counts[ERROR]} errors, {counts[WARNING]} warnings, {counts[INFO]} notes.")
    w("")
    w("## Contents")
    w("")
    for title in ("Summary", "Findings", "System overview", "Services", "Flows", "Schemas"):
        w(f"- [{title}](#{_anchor(title)})")
    w("")

    # Summary
    kinds = sorted({e.kind for e in model.elements.values()})
    w("## Summary")
    w("")
    w("| Service | " + " | ".join(model.kind(k).plural for k in kinds) + " |")
    w("|---|" + "---|" * len(kinds))
    for service in sorted(model.services):
        w(f"| [{service}](#{_anchor('service ' + service)}) | "
          + " | ".join(str(len(model.of_kind(k, service))) for k in kinds) + " |")
    w("")

    # Findings
    w("## Findings")
    w("")
    findings = model.sorted_findings()
    if not findings:
        w("No findings.")
    else:
        w("| Severity | Check | Finding | Location |")
        w("|---|---|---|---|")
        for f in findings:
            w(f"| {SEVERITY_ICON.get(f.severity, f.severity)} | `{f.code}` | {_cell(f.message)} | {_loc(f.file, f.line)} |")
    w("")

    # Overview
    w("## System overview")
    w("")
    w("Solid arrows emit events, thick arrows start commands, dotted arrows read queries. "
      "A red dashed node is called but not registered. A grey node belongs to a service outside this corpus.")
    w("")
    overview = _Diagram(model)
    for e in sorted(model.elements.values(), key=lambda e: e.ref)[:MAX_OVERVIEW_NODES]:
        overview.add_ref(e.ref)
    for r in model.relations:
        overview.add_relation(r)
    out += _code(overview.render(), "mermaid")
    w("")

    # Services
    w("## Services")
    w("")
    for service in sorted(model.services):
        info = model.services[service]
        w(f"### Service {service}")
        w("")
        if info.get("namespace"):
            w(f"Namespace `{info['namespace']}` in {_loc(info.get('file'), None)}.")
            w("")
        diagram = _Diagram(model)
        for e in model.elements.values():
            if e.service == service:
                diagram.add_ref(e.ref)
        for r in model.relations:
            if r.source.service == service or r.target.service == service:
                diagram.add_relation(r)
        out += _code(diagram.render(), "mermaid")
        w("")
        for kind in kinds:
            elements = model.of_kind(kind, service)
            if not elements:
                continue
            spec = model.kind(kind)
            w(f"#### {spec.plural}")
            w("")
            w("| Name | Handler | Reads | Emits | Starts | Started by | Details |")
            w("|---|---|---|---|---|---|---|")
            for e in elements:
                outgoing = model.outgoing(e.ref)
                reads = [r.target for r in outgoing if r.relation == "reads"]
                emits = [r.target for r in outgoing if r.relation == "emits"]
                starts = [r.target for r in outgoing if r.relation in ("triggers", "invokes")]
                by = [r.source for r in model.incoming(e.ref) if r.relation != "reads"]
                details = []
                if e.attrs.get("auth"):
                    details.append("roles " + ", ".join(f"`{a}`" for a in e.attrs["auth"]))
                if e.attrs.get("deps"):
                    details.append("deps " + ", ".join(f"`{d}`" for d in e.attrs["deps"]))
                for key in ("apply", "fx"):
                    if e.attrs.get(key):
                        details.append(f"{key} `{e.attrs[key]}`")
                for part in e.schemas:
                    details.append(f"[{part} schema](#{_anchor(f'{e.service} {e.kind} {e.name} {part}')})")
                handler = f"`{e.handler}`" if e.handler else "—"
                w(f"| `{e.name}` {_loc(e.file, e.line)} | {handler} | {_names(reads, service)} | "
                  f"{_names(emits, service)} | {_names(starts, service)} | {_names(by, service)} | "
                  f"{_cell('; '.join(details)) or '—'} |")
            w("")
        if info.get("schema"):
            w("#### Service schema")
            w("")
            out += _code(json.dumps(info["schema"], indent=2), "json")
            w("")

    # Flows
    w("## Flows")
    w("")
    w("Each flow starts at an entry point: an element that no other element in the corpus starts.")
    w("")
    entries = model.entry_points()
    if not entries:
        w("No entry points.")
        w("")
    for e in entries:
        relations = model.flow(e.ref)
        if not relations:
            continue
        w(f"### {e.service}: {model.kind(e.kind).label.lower()} {e.name}")
        w("")
        out += _code(_sequence(model, e, relations), "mermaid")
        w("")
    cycles = _cycles(model)
    if cycles:
        w("### Cycles")
        w("")
        w("These flows return to an element that started them. Check that each loop has a stop condition.")
        w("")
        for c in cycles:
            w("- " + " → ".join(f"`{r}`" for r in c))
        w("")

    # Schemas
    w("## Schemas")
    w("")
    any_schema = False
    for e in sorted(model.elements.values(), key=lambda e: e.ref):
        for part, schema in e.schemas.items():
            any_schema = True
            w(f"### {e.service} {e.kind} {e.name} {part}")
            w("")
            w(f"File: [`{schema_path(e.ref, part)}`]({schema_path(e.ref, part)})")
            w("")
            problems = [f for f in model.sorted_findings() if f.ref == e.ref and (
                (f.code.startswith("schema-") and f.message.startswith(f"{e.ref} {part}:"))
                or (part == "consumes" and f.code == "payload-invalid"))]
            if problems:
                w("Problems:")
                w("")
                for f in problems:
                    loc = f" (`{f.file}:{f.line}`)" if f.file else ""
                    w(f"- **{f.severity}** `{f.code}`: {f.message}{loc}")
                w("")
            if part in e.sources:
                w("<details><summary>Malli source</summary>")
                w("")
                out += _code(e.sources[part], "clojure")
                w("")
                w("</details>")
                w("")
            out += _code(json.dumps(schema, indent=2), "json")
            w("")
    if not any_schema:
        w("No schemas.")
        w("")
    return "\n".join(out).rstrip() + "\n"


def _cycles(model: DomainModel) -> list[list[Ref]]:
    graph: dict[Ref, list[Ref]] = {}
    for r in model.relations:
        if r.relation != "reads":
            graph.setdefault(r.source, []).append(r.target)
    seen_cycles: set[tuple[Ref, ...]] = set()
    cycles: list[list[Ref]] = []

    def visit(node: Ref, path: list[Ref], on_path: set[Ref]) -> None:
        for nxt in graph.get(node, []):
            if nxt in on_path:
                cycle = path[path.index(nxt):]
                i = cycle.index(min(cycle))
                key = tuple(cycle[i:] + cycle[:i])
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    cycles.append(list(key) + [key[0]])
            elif len(path) < 12:
                visit(nxt, path + [nxt], on_path | {nxt})

    for start in sorted(graph):
        visit(start, [start], {start})
    return cycles


def write_outputs(model: DomainModel, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    schemas_dir = out_dir / "schemas"
    if schemas_dir.exists():
        for old in schemas_dir.rglob("*.json"):
            old.unlink()
    written: dict[str, Path] = {}
    for e in model.elements.values():
        for part, schema in e.schemas.items():
            path = out_dir / schema_path(e.ref, part)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
            written[f"{e.ref.key}:{part}"] = path
    md = out_dir / "DOMAIN.md"
    md.write_text(render_markdown(model), encoding="utf-8")
    js = out_dir / "domain.json"
    js.write_text(json.dumps(model.to_dict(), indent=2) + "\n", encoding="utf-8")
    written["markdown"], written["json"] = md, js
    return written
