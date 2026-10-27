"""`_same_graph_for_compare` decides whether an update rewrites graph.json.

It must treat two payloads as equal exactly when they differ only in item
order, `built_at_commit` and a missing `directed` key.
"""
from __future__ import annotations

import copy

from graphify.watch import _same_graph_for_compare


def _graph() -> dict:
    return {
        "directed": False,
        "built_at_commit": "abc",
        "graph": {"name": "g"},
        "nodes": [{"id": "a", "label": "a()"}, {"id": "b", "label": "b()"}],
        "links": [{"source": "a", "target": "b", "relation": "calls"}],
        "hyperedges": [],
    }


def test_item_order_commit_and_missing_directed_do_not_count():
    left = _graph()
    right = copy.deepcopy(left)
    right["nodes"].reverse()
    right["built_at_commit"] = "def"
    del right["directed"]
    assert _same_graph_for_compare(left, right)
    assert _same_graph_for_compare(right, left)


def test_any_content_difference_counts():
    base = _graph()
    changed_item = copy.deepcopy(base)
    changed_item["nodes"][0]["label"] = "z()"
    extra_item = copy.deepcopy(base)
    extra_item["links"].append({"source": "b", "target": "a", "relation": "calls"})
    duplicate_swap = copy.deepcopy(base)
    duplicate_swap["nodes"] = [base["nodes"][0], base["nodes"][0]]
    metadata = copy.deepcopy(base)
    metadata["graph"] = {"name": "h"}
    directed = copy.deepcopy(base)
    directed["directed"] = True
    missing_list = copy.deepcopy(base)
    del missing_list["hyperedges"]
    for other in (changed_item, extra_item, duplicate_swap, metadata, directed, missing_list):
        assert not _same_graph_for_compare(base, other)
        assert not _same_graph_for_compare(other, base)
