"""Shared fixture graph for the knowledge base tests."""
import json
from pathlib import Path


def make_graph(tmp_path: Path) -> Path:
    out = tmp_path / "graphify-out"
    out.mkdir()
    graph = {
        "directed": True,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {"id": "a", "label": "Alpha", "file_type": "code", "source_file": "a.py"},
            {"id": "b", "label": "Beta", "file_type": "code", "source_file": "b.py"},
            {"id": "r", "label": "Why alpha", "file_type": "rationale", "source_file": "a.py"},
        ],
        "links": [
            {"source": "a", "target": "b", "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "r", "target": "a", "relation": "rationale_for", "confidence": "EXTRACTED"},
        ],
    }
    path = out / "graph.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    return path
