"""The fast sampled betweenness is bit-identical to networkx."""

import networkx as nx
import pytest

from graphify.analyze import _sampled_betweenness


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("directed", [False, True])
@pytest.mark.parametrize("k", [None, 7])
def test_matches_networkx(seed, directed, k):
    G = nx.gnm_random_graph(40 + seed * 9, 90 + seed * 30, seed=seed, directed=directed)
    G.add_edge(0, 0)
    G.add_node("isolated")
    assert _sampled_betweenness(G, k, seed=42) == nx.betweenness_centrality(G, k=k, seed=42)


def test_keeps_networkx_node_order():
    G = nx.path_graph(["c", "a", "b", "d"])
    assert list(_sampled_betweenness(G, None)) == list(nx.betweenness_centrality(G))
