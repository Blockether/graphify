"""``graphify kb`` - the knowledge base commands.

    graphify kb serve    [graph.json] [--host H] [--port P] [--token T] [--allow-host NAME] [--read-only] [--open]
    graphify kb export   [graph.json] [--out kb.html] [--name NAME]
    graphify kb validate [graph.json] [--limit N] [--json] [--strict]
    graphify kb infer    [graph.json] [--save] [--json]
    graphify kb ingest   <file|url>... [--graph graph.json] [--llm] [--title T] [--tag T] [--no-link]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_GRAPH = Path("graphify-out") / "graph.json"


def _graph(value: str | None) -> Path:
    path = Path(value) if value else DEFAULT_GRAPH
    if path.is_dir():
        nested = path / "graphify-out" / "graph.json"
        path = nested if nested.is_file() else path / "graph.json"
    return path


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="graphify kb", description="Knowledge base on top of graph.json.")
    sub = p.add_subparsers(dest="action", required=True)

    s = sub.add_parser("serve", help="run the HTTP API and the admin UI")
    s.add_argument("graph", nargs="?")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=7878)
    s.add_argument("--token", default=os.environ.get("GRAPHIFY_KB_TOKEN"),
                   help="bearer token (default $GRAPHIFY_KB_TOKEN); required off loopback")
    s.add_argument("--read-only", action="store_true")
    s.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="also accept this Host header name (repeat it); needed for a DNS name on a 0.0.0.0 bind")
    s.add_argument("--open", action="store_true", help="open the browser")

    e = sub.add_parser("export", help="write a read-only HTML page")
    e.add_argument("graph", nargs="?")
    e.add_argument("--out", help="output file (default graphify-out/kb.html)")
    e.add_argument("--name", help="title of the page")

    v = sub.add_parser("validate", help="check the graph against the ontology")
    v.add_argument("graph", nargs="?")
    v.add_argument("--limit", type=int, default=50)
    v.add_argument("--no-info", action="store_true", help="hide info findings")
    v.add_argument("--json", action="store_true")
    v.add_argument("--strict", action="store_true", help="exit 1 on any error finding")

    i = sub.add_parser("infer", help="infer an ontology from the graph")
    i.add_argument("graph", nargs="?")
    i.add_argument("--save", action="store_true", help="write graphify-out/ontology.json")
    i.add_argument("--json", action="store_true")

    g = sub.add_parser("ingest", help="add files or URLs as documents")
    g.add_argument("sources", nargs="+")
    g.add_argument("--graph")
    g.add_argument("--llm", action="store_true", help="extract nodes and edges with the configured LLM")
    g.add_argument("--backend")
    g.add_argument("--model")
    g.add_argument("--title")
    g.add_argument("--tag", action="append", default=[])
    g.add_argument("--no-link", action="store_true", help="do not link mentions of known nodes")
    g.add_argument("--json", action="store_true")
    return p


def _print_report(report: dict, limit: int) -> None:
    c = report["counts"]
    print(f"{c.get('error', 0)} errors, {c.get('warning', 0)} warnings, {c.get('info', 0)} info "
          f"({report['nodes']} nodes, {report['edges']} edges)")
    for code, n in sorted(report.get("by_code", {}).items(), key=lambda kv: -kv[1]):
        print(f"  {code:<22} {n}")
    for f in report.get("findings", [])[:limit]:
        where = f.get("node") or f.get("edge") or ""
        print(f"  [{f.get('severity')}] {f.get('code')}: {f.get('message')} {where}".rstrip())
    if report.get("truncated"):
        print(f"  ... {report['total']} findings in total")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from graphify.kb import KBError, KnowledgeBase

    try:
        if args.action == "serve":
            from graphify.kb_server import serve

            serve(_graph(args.graph), host=args.host, port=args.port, token=args.token,
                  read_only=args.read_only, allowed_hosts=args.allow_host or None,
                  open_browser=args.open)
            return 0
        if args.action == "export":
            from graphify.kb_html import write_static_html

            kb = KnowledgeBase(_graph(args.graph), create=False)
            target = write_static_html(kb, args.out, name=args.name)
            print(f"Wrote {target} ({target.stat().st_size // 1024} KiB)")
            return 0
        if args.action == "validate":
            kb = KnowledgeBase(_graph(args.graph), create=False)
            report = kb.validate(limit=max(args.limit, 1), include_info=not args.no_info)
            if args.json:
                print(json.dumps(report, indent=2, default=str))
            else:
                _print_report(report, args.limit)
            return 1 if args.strict and report["counts"].get("error", 0) else 0
        if args.action == "infer":
            kb = KnowledgeBase(_graph(args.graph), create=False)
            onto = kb.infer_ontology(save=args.save)
            if args.json:
                print(json.dumps(onto, indent=2, default=str))
            else:
                print(f"{len(onto['classes'])} classes, {len(onto['relations'])} relations")
                if args.save:
                    print(f"Saved {kb.onto_path}")
            return 0
        if args.action == "ingest":
            kb = KnowledgeBase(_graph(args.graph), create=True)
            results = []
            for source in args.sources:
                common = dict(mode="llm" if args.llm else "text", title=args.title,
                              tags=args.tag or None, link=not args.no_link,
                              backend=args.backend, model=args.model, actor="cli")
                if source.startswith(("http://", "https://")):
                    res = kb.ingest(source.rsplit("/", 1)[-1] or "page", url=source, **common)
                else:
                    path = Path(source)
                    data = path.read_bytes()
                    if not data:
                        raise KBError(f"{source} is empty; an empty file is not a document")
                    res = kb.ingest(path.name, data=data, **common)
                results.append(res)
                if not args.json:
                    doc = res.get("document") or {}
                    print(f"Ingested {source} -> document {doc.get('id')} "
                          f"({res.get('linked', 0)} links, {len(res.get('nodes_added') or [])} nodes, "
                          f"{res.get('edges_added', 0)} edges)")
                    for note in res.get("notes") or []:
                        print(f"  note: {note}")
            kb.flush()
            if args.json:
                print(json.dumps(results, indent=2, default=str))
            return 0
    except (KBError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2
