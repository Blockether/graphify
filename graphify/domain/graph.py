"""Merge a domain model into a graphify extraction (``graph.json``).

Domain nodes carry ``_origin: "domain"``. Each run removes the old domain
nodes and edges first, so the domain layer always matches the current code.
Each element links to the code node of its handler (``handled_by``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

from graphify.domain.model import DomainModel, Ref

DOMAIN_ORIGIN = "domain"
ENV_SWITCH = "GRAPHIFY_DOMAIN"
_RELATION_CONFIDENCE = "EXTRACTED"


def node_id(ref: Ref) -> str:
    return f"domain:{ref.kind}:{ref.service}:{ref.name}"


def service_node_id(service: str) -> str:
    return f"domain:service:{service}"


def enabled(env: dict | None = None) -> bool:
    value = (env if env is not None else os.environ).get(ENV_SWITCH, "").strip().lower()
    return value not in ("0", "false", "no", "off")


def strip_domain(result: dict) -> None:
    """Remove the domain nodes and edges of an earlier run."""
    ids = {n.get("id") for n in result.get("nodes", []) if n.get("_origin") == DOMAIN_ORIGIN}
    result["nodes"] = [n for n in result.get("nodes", []) if n.get("_origin") != DOMAIN_ORIGIN]
    result["edges"] = [e for e in result.get("edges", [])
                       if e.get("_origin") != DOMAIN_ORIGIN and e.get("source") not in ids and e.get("target") not in ids]


def _code_index(nodes: Iterable[dict]) -> dict[tuple[str, str], str]:
    """Map (source file, function name) to a code node id."""
    index: dict[tuple[str, str], str] = {}
    for n in nodes:
        if n.get("_origin") == DOMAIN_ORIGIN or not n.get("source_file"):
            continue
        label = str(n.get("label", ""))
        name = label[:-2] if label.endswith("()") else label
        index.setdefault((str(n["source_file"]).replace("\\", "/"), name), n["id"])
    return index


def to_graph(model: DomainModel, code_nodes: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Return the domain nodes and edges for ``model``."""
    index = _code_index(code_nodes)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add_node(ref: Ref, file: str | None, line: int | None, **extra) -> str:
        nid = node_id(ref)
        if nid not in nodes:
            spec = model.kind(ref.kind)
            nodes[nid] = {
                "id": nid, "label": f"{spec.label} {ref.name}", "file_type": "concept",
                "source_file": file or "", "source_location": f"L{line}" if line else None,
                "_origin": DOMAIN_ORIGIN, "domain_kind": ref.kind, "domain_service": ref.service,
                "domain_name": ref.name, **extra,
            }
        return nid

    def edge(source: str, target: str, relation: str, file: str | None, line: int | None, **extra) -> None:
        edges.append({"source": source, "target": target, "relation": relation,
                      "confidence": _RELATION_CONFIDENCE, "source_file": file or "",
                      "source_location": f"L{line}" if line else None, "_origin": DOMAIN_ORIGIN, **extra})

    for service, info in sorted(model.services.items()):
        nodes[service_node_id(service)] = {
            "id": service_node_id(service), "label": f"Service {service}", "file_type": "concept",
            "source_file": info.get("file", ""), "source_location": None, "_origin": DOMAIN_ORIGIN,
            "domain_kind": "service", "domain_service": service, "domain_name": service,
        }
    for e in sorted(model.elements.values(), key=lambda e: e.ref):
        nid = add_node(e.ref, e.file, e.line, domain_plugin=e.plugin or "",
                       domain_schemas=sorted(e.schemas))
        edge(service_node_id(e.service), nid, "contains", e.file, e.line)
        handler_file = e.attrs.get("handler_file")
        if e.handler and handler_file:
            code = index.get((handler_file, e.handler.rsplit("/", 1)[-1]))
            if code:
                edge(nid, code, "handled_by", handler_file, e.attrs.get("handler_line"))
    for r in model.relations:
        source = add_node(r.source, r.file, r.line)
        if r.target not in model.elements:
            missing = r.target.service in model.services
            add_node(r.target, r.file, r.line, domain_missing=missing, domain_external=not missing)
        edge(source, node_id(r.target), r.relation, r.file, r.line,
             domain_missing=r.target not in model.elements)
    return list(nodes.values()), edges


def augment(result: dict, root: str | Path, paths: Iterable[str | Path] | None = None,
            out_dir: str | Path | None = None, specs: list[str] | None = None):
    """Run the domain plugins and merge their model into ``result``.

    Write ``DOMAIN.md`` and the schemas to ``out_dir`` when it is given.
    Return the :class:`~graphify.domain.DomainResult`, or None when no plugin
    applies. The graph build never fails because of a plugin error.
    """
    strip_domain(result)
    if not enabled():
        return None
    from graphify.domain import analyze, write_outputs

    try:
        res = analyze(root, paths, specs=specs)
    except Exception as e:  # a broken plugin must not break the code graph
        print(f"[graphify domain] skipped: {e}", file=sys.stderr)
        return None
    for err in res.plugin_errors:
        print(f"[graphify domain] plugin error: {err}", file=sys.stderr)
    if res.empty or not res.model.elements:
        return None
    nodes, edges = to_graph(res.model, result.get("nodes", []))
    result.setdefault("nodes", []).extend(nodes)
    result.setdefault("edges", []).extend(edges)
    if out_dir is not None:
        write_outputs(res.model, Path(out_dir))
    counts = res.model.finding_counts()
    print(f"[graphify domain] {', '.join(res.model.plugins)}: {len(res.model.elements)} elements, "
          f"{len(res.model.relations)} links, {counts['error']} errors, {counts['warning']} warnings",
          file=sys.stderr)
    return res
