"""The ``graphify domain`` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from graphify.domain import analyze, write_outputs
from graphify.domain.model import ERROR, INFO, WARNING
from graphify.domain.plugin import PluginError, describe


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="graphify domain",
                                description="Extract the domain model of a project with domain plugins.")
    p.add_argument("path", nargs="?", default=".", help="project root (default: .)")
    p.add_argument("--plugin", action="append", default=[], metavar="SPEC",
                   help="plugin name, module:attr or .py file; repeat for more (default: auto-detect)")
    p.add_argument("--out", default=None, help="output directory (default <path>/graphify-out/domain)")
    p.add_argument("--strict", action="store_true", help="exit 1 when a check reports an error")
    p.add_argument("--json", action="store_true", help="print a JSON summary")
    p.add_argument("--list-plugins", action="store_true", help="list the available plugins and exit")
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.path)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    if args.list_plugins:
        rows = describe(root)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for row in rows:
                print(f"{row['name']:<20} {row['description']}")
        return 0
    try:
        res = analyze(root, specs=args.plugin or None)
    except PluginError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for err in res.plugin_errors:
        print(f"warning: plugin error: {err}", file=sys.stderr)
    model = res.model
    out = Path(args.out) if args.out else root / "graphify-out" / "domain"
    written = write_outputs(model, out) if not res.empty else {}
    counts = model.finding_counts()
    if args.json:
        print(json.dumps({
            "root": str(root.resolve()),
            "out": str(out.resolve()) if written else None,
            "markdown": str(written["markdown"].resolve()) if written else None,
            "json": str(written["json"].resolve()) if written else None,
            "plugins": model.plugins,
            "services": sorted(model.services),
            "counts": model.counts(),
            "relations": len(model.relations),
            "schemas": sum(len(e.schemas) for e in model.elements.values()),
            "findings": [f.to_dict() for f in model.sorted_findings()],
            "finding_counts": counts,
        }, indent=2))
    elif res.empty:
        print("No domain plugin applies to this project. Use --plugin or --list-plugins.")
    else:
        kinds = ", ".join(f"{n} {model.kind(k).plural.lower()}" for k, n in model.counts().items())
        print(f"{', '.join(model.plugins)}: {len(model.services)} services, {kinds}, {len(model.relations)} links.")
        print(f"Findings: {counts[ERROR]} errors, {counts[WARNING]} warnings, {counts[INFO]} notes.")
        for f in model.sorted_findings():
            if f.severity in (ERROR, WARNING):
                loc = f" ({f.file}:{f.line})" if f.file else ""
                print(f"  {f.severity:<7} {f.code}: {f.message}{loc}")
        print(f"Wrote {written['markdown']} and {len(written) - 2} schema files.")
    if args.strict and counts[ERROR]:
        return 1
    return 0
