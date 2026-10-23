"""Entity linking for knowledge base documents (graphify.kb_link).

The cases come from E2E bug 7 (wrong links from bare words) and memo F5 / R4
(candidates by shape, Milne-Witten ranking, Fellegi-Sunter review band).
"""

import dataclasses
import gzip
import json
from pathlib import Path

from graphify import kb_link as KL


def _node(nid, label, src, **extra):
    node = {"id": nid, "label": label, "source_file": src, "file_type": "code"}
    node.update(extra)
    return node


def _graph():
    nodes = [
        # A Clojure namespace with its file node and two vars.
        _node(
            "gw_ns",
            "com.acme.gateway.client",
            "src/com/acme/gateway/client.clj",
            metadata={"kind": "namespace"},
            community=1,
        ),
        _node("gw_file", "gateway/client.clj", "src/com/acme/gateway/client.clj", community=1),
        _node("gw_req", "request!()", "src/com/acme/gateway/client.clj", community=1),
        _node("gw_lease", "acquire-lease()", "src/com/acme/gateway/client.clj", community=1),
        # A second namespace with a var of the same name.
        _node(
            "co_ns",
            "com.acme.council.core",
            "src/com/acme/council/core.clj",
            metadata={"kind": "namespace"},
            community=2,
        ),
        _node("co_req", "request!()", "src/com/acme/council/core.clj", community=2),
        _node("co_members", "members()", "src/com/acme/council/core.clj", community=2),
        # Two classes with the same name in two languages.
        _node("ts_file", "gateway.ts", "app/src/lib/gateway.ts", community=3),
        _node("ts_client", "GatewayClient", "app/src/lib/gateway.ts", community=3),
        _node("py_file", "_client.py", "sdk/acme/engine/_client.py", community=4),
        _node("py_client", "GatewayClient", "sdk/acme/engine/_client.py", community=4),
        # A Python module that a dotted name reaches.
        _node("ext_file", "extension.py", "sdk/src/acme/vis/extension.py", community=4),
        _node("ext_core", "extension", "src/other/extension_core.clj", community=5),
        # Bare words that many files use as labels (bug 7).
        _node("run_a", "run()", "a/x.py"),
        _node("run_b", "run()", "b/y.py"),
        _node("run_c", "run()", "c/z.py"),
        _node("status", "status", "a/x.py"),
        _node("ios", "ios", "scripts/ios.sh"),
        # A manual heading and a page.
        _node(
            "h_deploy",
            "Continuous deployment",
            "docs/ci.md",
            file_type="document",
            node_kind="heading",
        ),
        _node(
            "rat", "Why the lease exists", "src/com/acme/gateway/client.clj", file_type="rationale"
        ),
        # A multi-word label in one file.
        _node("hub", "Session subscription hub", "app/src/lib/subscriptions.ts", community=3),
    ]
    links = [
        ("gw_ns", "gw_req"),
        ("gw_ns", "gw_lease"),
        ("gw_file", "gw_ns"),
        ("gw_req", "gw_lease"),
        ("co_ns", "co_req"),
        ("co_ns", "co_members"),
        ("ts_file", "ts_client"),
        ("ts_file", "hub"),
        ("py_file", "py_client"),
    ]
    return nodes, [{"source": s, "target": t, "relation": "contains"} for s, t in links]


def _index():
    nodes, links = _graph()
    return KL.LinkIndex.build(nodes, links)


def _ids(text, **kw):
    return KL.link_text(text, _index(), **kw).ids


def test_keys_fold_case_parens_and_separators():
    assert KL.label_key(" .Request!() ") == "request!"
    assert KL.label_key("fork-boundary-test (test)") == "fork-boundary-test"
    assert KL.loose_key("Shopping cart") == KL.loose_key("ShoppingCart") == "shoppingcart"


def test_identifier_shapes():
    for name in (
        "snake_case",
        "GatewayClient",
        "parseJSON",
        "a.b",
        "a::b",
        "ns/var",
        "kebab-case",
        "run()",
    ):
        assert KL.is_identifier(name), name
    # memo R4: plain words and product names are not code names.
    for word in ("status", "Status", "iOS", "client"):
        assert not KL.is_identifier(word), word


def test_bare_common_words_never_link():
    # Bug 7: "run", "status" and "client" linked to random code nodes.
    text = "We run the release, check the status and restart the client on iOS."
    assert _ids(text) == []


def test_rationale_nodes_are_not_targets():
    assert "rat" not in _ids("Why the lease exists is a long story.")


def test_unique_code_span_links_with_code_score():
    dec = KL.link_text("Call `acquire-lease` first.", _index())
    assert dec.ids == ["gw_lease"]
    link = dec.links[0]
    assert link["confidence_score"] == KL.SCORE_CODE and link["method"] == "unique"


def test_common_label_in_many_files_is_not_a_name():
    # Mihalcea-Csomai keyphraseness: run() is in three files, so a code span
    # still sees three candidates and goes to review, never a guess.
    dec = KL.link_text("Then `run()` starts.", _index())
    assert dec.ids == []
    assert dec.possible and dec.possible[0]["candidates_total"] == 3


def test_phrase_of_two_or_more_words_links():
    dec = KL.link_text("The Session subscription hub keeps a cursor.", _index())
    assert dec.ids == ["hub"]
    assert dec.links[0]["confidence_score"] == KL.SCORE_PHRASE


def test_heading_links_as_a_name_or_as_a_unique_label():
    # #8070 item 3: a unique heading label of two or more words links at 0.65, also at a sentence start.
    dec = KL.link_text("Continuous deployment is on for main.", _index())
    assert dec.ids == ["h_deploy"] and dec.links[0]["confidence_score"] == KL.SCORE_HEADING
    named = KL.link_text("We keep Continuous deployment on for main.", _index())
    assert named.links[0]["confidence_score"] == KL.SCORE_PHRASE


def test_heading_with_a_tag_needs_the_whole_label():
    # #8070 eval FP: "Companion app" is not the heading "Companion app (iPhone / Android)".
    nodes, links = _graph()
    nodes.append(
        _node(
            "h_comp",
            "Companion app (iPhone / Android)",
            "README.md",
            file_type="document",
            node_kind="heading",
        )
    )
    assert (
        KL.link_text("Companion app opens one client.", KL.LinkIndex.build(nodes, links)).ids == []
    )


def test_scope_picks_the_member_of_the_named_namespace():
    # Memo R4: the document names its anchor, then "Its `request!`".
    dec = KL.link_text("Use `com.acme.gateway.client`. Its `request!` adds auth.", _index())
    assert set(dec.ids) == {"gw_ns", "gw_req"}
    req = next(x for x in dec.links if x["id"] == "gw_req")
    assert req["method"] == "scope" and req["confidence_score"] == KL.SCORE_SCOPE


def test_one_surface_can_name_two_entities():
    text = (
        "The app opens one `GatewayClient` (see app/src/lib/gateway.ts). "
        "The SDK has a different GatewayClient in sdk/acme/engine/_client.py."
    )
    assert {"ts_client", "py_client"} <= set(_ids(text))


def test_ambiguous_without_context_goes_to_review():
    # Fellegi-Sunter middle band: no anchor, so no guess.
    dec = KL.link_text("Then call `request!` again.", _index())
    assert dec.ids == []
    review = dec.possible[0]
    assert review["reason"] == "no_context" and review["candidates_total"] == 2
    assert {c["id"] for c in review["candidates"]} == {"gw_req", "co_req"}


def test_ambiguous_ranked_by_anchor_relatedness():
    # Milne-Witten: the anchor acquire-lease shares neighbours with gw_req only.
    dec = KL.link_text("`request!` and `acquire-lease` work together.", _index())
    assert set(dec.ids) == {"gw_req", "gw_lease"}
    req = next(x for x in dec.links if x["id"] == "gw_req")
    assert req["confidence_score"] == KL.SCORE_CONTEXT and req["margin"] >= KL.LINK_MARGIN


def test_caller_anchors_rank_candidates():
    dec = KL.link_text("Then call `request!` again.", _index(), anchors=["co_ns"])
    assert dec.ids == ["co_req"]


def test_qualified_alias_resolves_through_namespace():
    assert _ids("See `gateway-client/request!`.") == ["gw_req"]
    assert _ids("See council.core/request! for peers.") == ["co_req"]


def test_dotted_module_resolves_to_file_not_to_a_part():
    # A dotted Python module reaches its file; "extension" alone is not enough.
    assert _ids("Import acme.vis.extension in the hook.") == ["ext_file"]


def test_path_resolves_to_graphify_file_node():
    assert _ids("Read src/com/acme/gateway/client.clj first.") == ["gw_file"]


def test_camel_case_matches_the_label_case_only():
    # "iOS" must not reach the script label "ios".
    assert "ios" not in _ids("On iOS the GatewayClient waits.", anchors=["ts_file"])


def test_code_fences_links_and_urls_are_skipped():
    text = "```\nacquire-lease\n```\nSee [x](src/com/acme/gateway/client.clj) and https://e.com/acquire-lease"
    assert _ids(text) == []


def test_exclude_and_limit():
    text = "`acquire-lease`, `members` and the Session subscription hub."
    assert "gw_lease" not in _ids(text, exclude=["gw_lease"])
    assert len(_ids(text, limit=1)) == 1


def test_wiki_links_parse_outside_fences():
    text = "See [[Gateway lease]] and [[GatewayClient|code]].\n```\n[[not a link]]\n```"
    assert KL.wiki_links(text) == [("Gateway lease", None, 4), ("GatewayClient", "code", 26)]


def test_resolve_wiki_never_guesses():
    # Memo R4: a wiki link is EXTRACTED only when it fits exactly one node.
    index = _index()
    assert KL.resolve_wiki("gw_req", index).status == "id"
    assert KL.resolve_wiki("acquire-lease", index).ids == ["gw_lease"]
    amb = KL.resolve_wiki("GatewayClient", index, anchors=["py_file"])
    assert amb.status == "ambiguous" and amb.ids[0] == "py_client"
    assert KL.resolve_wiki("Nothing here", index).status == "missing"
    by_class = KL.resolve_wiki(
        "GatewayClient",
        index,
        cls="py",
        class_of=lambda n: "py" if n["source_file"].endswith(".py") else "ts",
    )
    assert by_class.status == "unique" and by_class.ids == ["py_client"]


# -- cases from the independent evaluation (#8065, #8070) ------------------


def test_contrastive_qualifiers_give_nil():
    # Two places with their own words in the sentence ("app" and "engine"): the text is ambiguous.
    dec = KL.link_text("The app and the engine each keep a `GatewayClient` open.", _index())
    assert "ts_client" not in dec.ids and "py_client" not in dec.ids
    assert dec.possible and dec.possible[0]["reason"] == "ambiguous_context"


def test_qualifier_in_the_sentence_decides():
    dec = KL.link_text("The engine `GatewayClient` retries a request.", _index())
    assert "py_client" in dec.ids
    assert next(x for x in dec.links if x["id"] == "py_client")["method"] == "qualifier"


def test_contested_words_qualify_nothing_else_in_the_paragraph():
    # "On iOS and Android": both platform words name alternatives for one name (#8070 FP OAuthLoopback).
    nodes = [
        _node("j", "Loop", "native/android/Loop.java"),
        _node("s", "Loop", "native/ios/Loop.swift"),
        _node("jc", ".callback()", "native/android/Loop.java"),
        _node("sc", ".callback()", "native/oauth/Loop.swift"),
    ]
    index = KL.LinkIndex.build(
        nodes, [{"source": "j", "target": "jc"}, {"source": "s", "target": "sc"}]
    )
    dec = KL.link_text(
        "On iOS and Android the flow uses the native Loop plugin. Its `.callback()` gets the redirect.",
        index,
    )
    assert dec.ids == []
    assert {p["mention"] for p in dec.possible} == {"Loop", ".callback()"}


def test_the_prior_alone_never_decides():
    # run() in a/x.py has many edges; degree is no evidence without context.
    nodes, links = _graph()
    nodes += [_node(f"u{i}", f"user_{i}()", "a/x.py") for i in range(30)]
    links += [{"source": f"u{i}", "target": "run_a", "relation": "calls"} for i in range(30)]
    assert (
        "run_a" not in KL.link_text("Call `run()` to start.", KL.LinkIndex.build(nodes, links)).ids
    )


def test_one_sense_per_paragraph():
    # Each paragraph has its own anchor, so one name links to two nodes.  A
    # plain word such as `members` is a weak name, so the anchor is `members()`.
    text = (
        "`acquire-lease` keeps the lease. `request!` sends it.\n\n"
        "`members()` lists the peers. `request!` asks them."
    )
    dec = KL.link_text(text, _index())
    assert "gw_req" in dec.ids  # the first paragraph's sense
    # The second paragraph ranks its own sense first; one document-wide context would tie both.
    second = [x for x in dec.links if x["id"] == "co_req"] or [
        p for p in dec.possible if p["offset"] > text.index("\n\n")
    ]
    top = second[0]["id"] if "id" in second[0] else second[0]["candidates"][0]["id"]
    assert top == "co_req"


def test_overlap_longest_label_wins():
    nodes = [
        _node("long", "Session subscription hub", "a/hub.ts"),
        _node("short", "subscription hub", "b/sub.ts"),
    ]
    dec = KL.link_text(
        "The Session subscription hub keeps a cursor.", KL.LinkIndex.build(nodes, [])
    )
    assert dec.ids == ["long"]


def test_offsets_count_unicode_characters():
    text = "Zażółć gęślą jaźń — then `acquire-lease` runs."
    dec = KL.link_text(text, _index())
    rec = next(x for x in dec.links if x["id"] == "gw_lease")
    assert text[rec["offset"] : rec["offset"] + len("acquire-lease")] == "acquire-lease"


def test_generic_surface_is_nil():
    # 80 files define __init__: no context can make one of them the right one.
    nodes = [_node(f"i{k}", "__init__()", f"pkg{k}/mod.py") for k in range(80)]
    dec = KL.link_text("Each class sets fields in `__init__()`.", KL.LinkIndex.build(nodes, []))
    assert dec.ids == []


def test_performance_on_a_large_graph():
    # #8070 item 1: the overlap test was quadratic in the text size.
    import random
    import time

    rng = random.Random(5)
    nodes = [
        _node(f"n{i}", f"name_{i % 9000}_{rng.choice('abcdef')}()", f"d{i % 700}/f{i % 3000}.py")
        for i in range(50_000)
    ]
    links = [{"source": f"n{i}", "target": f"n{rng.randrange(50_000)}"} for i in range(80_000)]
    index = KL.LinkIndex.build(nodes, links)
    labels = [n["label"] for n in nodes]
    text = " ".join(
        f"See `{rng.choice(labels)}` and the {rng.choice(['cache', 'lease', 'queue'])}."
        for _ in range(400)
    )
    big = (text + "\n\n") * 10
    # CPU time, not wall time: a busy machine must not fail the test.
    start = time.process_time()
    KL.link_text(big, index)
    assert time.process_time() - start < 5.0, "linking a long document must stay near linear"


def test_external_text_gives_no_false_links():
    # Bug 7 on long external text (an arXiv paper, a GitHub README).  Code is
    # case-sensitive, a bare suffix is no path, "How it works" has one content
    # word, "code-only corpus" is English, and "Long Beach" is a place name.
    nodes = [
        _node("task", "task()", "tests/perf.py"),
        _node("pas", "PAs", "java/Formatter.java"),
        _node(
            "how", "How it works", "examples/README.md", file_type="document", node_kind="heading"
        ),
        _node("co", "code-only()", "test/policy_test.clj"),
        _node("beach", "Beach", "a/beach.py"),
    ]
    text = (
        "Factory Droid uses the `Task` tool. It reads `.pas` files.\n\n"
        "See **[How it works](https://example.com/how)**. A code-only corpus needs no key.\n\n"
        "The paper was shown in Long Beach last year."
    )
    dec = KL.link_text(text, KL.LinkIndex.build(nodes, []))
    assert dec.ids == [] and dec.possible == []


def test_slash_acronyms_are_prose():
    # A GitHub README says "TLS/SSL"; that is no ``ns/var`` name of the stub ``ssl``.
    nodes = [_node("ssl", "ssl", "", external=True), _node("cd", "CD", "ci/cd.py")]
    assert not KL.is_identifier("TLS/SSL") and not KL.is_identifier("CI/CD")
    assert KL.link_text("It supports TLS/SSL and CI/CD.", KL.LinkIndex.build(nodes, [])).ids == []


def test_code_names_do_not_link_headings():
    nodes = [_node("h", "grep", "docs/tools.md", file_type="document", node_kind="heading")]
    assert KL.link_text("Run `grep` on the logs.", KL.LinkIndex.build(nodes, [])).ids == []


def test_sentence_start_article_keeps_the_name():
    # "The Spinner shows progress": "The" does not make a title run.
    nodes = [_node("sp", "Spinner()", "app/ui.tsx"), _node("btn", "IconButton", "app/ui.tsx")]
    index = KL.LinkIndex.build(nodes, [{"source": "btn", "target": "sp"}])
    assert "sp" in KL.link_text("Use `IconButton` here. The Spinner shows progress.", index).ids


def test_plain_word_with_two_senses_goes_to_review():
    # memo R4 and Fellegi-Sunter: a plain word in a code span never links by
    # itself when several nodes share it; qualifier evidence makes it a review item.
    nodes = [
        _node("w", "ask()", "src/python/worker.clj"),
        _node("c", "ask()", "src/council/core.clj"),
    ]
    dec = KL.link_text("The worker waits: `ask` sends one request.", KL.LinkIndex.build(nodes, []))
    assert dec.ids == []
    assert [(p["mention"], p["reason"], p["candidates"][0]["id"]) for p in dec.possible] == [
        ("ask", "weak_name", "w")
    ]


# -- Regression sets (review 2, item 10) ------------------------------------
# tests/fixtures/kb_link/graph.json.gz is a subgraph of the Vis monorepo graph
# (47,820 nodes): every node whose label occurs in a document, plus its
# neighbours (a neighbour that is not a file or module keeps only a short id).
# ``total`` keeps the full node count, so the Milne-Witten scores
# equal the ones on the full graph. docs.json holds the labelled documents:
# set "eval" is the evaluator set that tuned the constants, set "hard" is the
# reviewer's hard set (Polish text, short names). Gold ids name graph nodes.

_FIXTURES = Path(__file__).parent / "fixtures" / "kb_link"


def _regression(name):
    data = json.loads(gzip.decompress((_FIXTURES / "graph.json.gz").read_bytes()))
    index = dataclasses.replace(
        KL.LinkIndex.build(data["nodes"], data["links"]), total=data["total"]
    )
    by_id = {str(n["id"]): n for n in data["nodes"]}
    docs = [d for d in json.loads((_FIXTURES / "docs.json").read_text()) if d["set"] == name]
    tp = fp = fn = nil_ok = nil_all = 0
    for doc in docs:
        anchors, got = [], set()
        for target, _cls, _at in KL.wiki_links(doc["text"]):
            res = KL.resolve_wiki(target, index, anchors=anchors)
            if res.status in ("id", "unique"):
                anchors.extend(res.ids)
                got.add(res.ids[0])
        got |= set(KL.link_text(doc["text"], index, anchors=anchors).ids)
        gold, optional = set(doc["gold"]), set(doc["optional"])
        tp, fp, fn = tp + len(got & gold), fp + len(got - gold - optional), fn + len(gold - got)
        wrong = {KL.label_key(by_id[n].get("label")) for n in got - gold - optional}
        nil_all += len(doc["nil"])
        nil_ok += sum(KL.label_key(s) not in wrong for s in doc["nil"])
    return tp / max(tp + fp, 1), tp / max(tp + fn, 1), nil_ok, nil_all


def test_regression_evaluator_set():
    # The set that tunes the policy constants: no wrong link, no missed link.
    p, r, nil_ok, nil_all = _regression("eval")
    assert (p, r) == (1.0, 1.0) and nil_ok == nil_all == 19


def test_regression_hard_set():
    # Review 2 hard set.  Before the review fixes: P 0.778, R 0.778, NIL 13/15.
    # The one link left outside the gold, run() by its scope, was judged correct.
    p, r, nil_ok, nil_all = _regression("hard")
    assert p >= 0.9 and r == 1.0 and nil_ok >= 14 and nil_all == 15


def test_common_name_with_many_candidates_needs_near_context():
    # Review 2, item 5 (hard set get()): 20 get() methods and only far context.
    nodes = [_node(f"g{k}", "get()", f"pkg{k}/store.py") for k in range(20)]
    nodes.append(_node("job", "JobRunner", "pkg3/store.py", _callable_class=True))
    text = "The JobRunner starts it.\n\nLater, unrelated.\n\nThen `get()` the result."
    dec = KL.link_text(text, KL.LinkIndex.build(nodes, []))
    assert "g3" not in dec.ids and not any(i.startswith("g") for i in dec.ids)
    assert any(p.get("reason") == "no_context" for p in dec.possible)


def test_capitalised_page_title_links():
    # Review 2, item 6 (hard set Drafts/Automations): a unique page title, not at a sentence start.
    nodes = [
        _node(
            "d",
            "Drafts",
            "docs/drafts.md",
            file_type="document",
            node_kind="heading",
            source_location="L1",
        ),
        _node("a", "Automations", "docs/automations.md", file_type="document", node_kind="page"),
    ]
    text = "Szkice opisuje strona Drafts. Zobacz też stronę Automations."
    dec = KL.link_text(text, KL.LinkIndex.build(nodes, []))
    assert {"d", "a"} <= set(dec.ids)
    assert all(x["confidence_score"] == KL.SCORE_HEADING for x in dec.links)


def test_non_ascii_identifiers_are_names():
    # Review 2, item 11: Unicode identifiers (Polish, Cyrillic) are code names.
    nodes = [
        _node("pl", "zażółćGęśląJaźń()", "pl/mod.py", _callable=True),
        _node("ru", "получитьДанные()", "ru/mod.py", _callable=True),
    ]
    assert KL.is_identifier("zażółćGęśląJaźń") and KL.is_identifier("получить_данные")
    text = "Wywołaj `zażółćGęśląJaźń()`, potem получитьДанные() zwraca dane…"
    assert set(KL.link_text(text, KL.LinkIndex.build(nodes, [])).ids) == {"pl", "ru"}


def test_code_span_with_two_candidates_links_by_a_clear_word_majority():
    # Follow-up 2 (C2 #8161 item 3): `GatewayClient` exists in the Companion app
    # (TS) and in the Python engine.  "companion lib" outweighs one "engine" word.
    nodes = [
        _node("ts", "GatewayClient", "apps/vis-companion/src/lib/gateway.ts", _callable_class=True),
        _node(
            "py",
            "GatewayClient",
            "packages/vis-agent/src/vis/engine/_client.py",
            _callable_class=True,
        ),
    ]
    text = "The companion lib talks to the engine through `GatewayClient`."
    dec = KL.link_text(text, KL.LinkIndex.build(nodes, []))
    assert dec.ids == ["ts"] and dec.links[0]["method"] == "qualifier"
    # Equal evidence on both sides stays ambiguous.
    tied = "The companion talks to the engine through `GatewayClient`."
    assert KL.link_text(tied, KL.LinkIndex.build(nodes, [])).ids == []


def test_clojure_alias_qualifier_matches_the_file_path():
    # Follow-up 2: `gateway-client/request!` names request! in gateway/client.clj,
    # also when no namespace node is in the graph.  -, ., / and _ are one separator.
    nodes = [
        _node("ok", "request!()", "src/com/acme/gateway/client.clj", _callable=True),
        _node("tui", "request!()", "apps/tui/src/com/acme/tui/client.clj", _callable=True),
        _node("rooms", "request!()", "test/com/acme/rooms_test.clj", _callable=True),
        _node("snake", "fetch_all()", "src/gateway_client.py", _callable=True),
        _node("other", "fetch_all()", "src/store.py", _callable=True),
    ]
    index = KL.LinkIndex.build(nodes, [])
    dec = KL.link_text("Call `gateway-client/request!` for each route.", index)
    assert dec.ids == ["ok"] and dec.links[0]["confidence_score"] == KL.SCORE_CODE
    assert KL.link_text("Use `gateway.client/fetch_all` here.", index).ids == ["snake"]


def test_test_files_rank_lower():
    # Follow-up 2: a note rarely means a test.  The test-file candidate loses a
    # tie, and a lone test-file candidate of a weak name is only possible.
    assert KL.is_test_file({"source_file": "test-native/com/acme/native_rooms_test.clj"})
    assert KL.is_test_file({"source_file": "tests/test_kb.py"})
    assert KL.is_test_file({"source_file": "web/src/app.spec.ts"})
    assert KL.is_test_file({"source_file": "pkg/foo_test.go"})
    assert not KL.is_test_file({"source_file": "src/contest/latest.py"})
    nodes = [
        _node("src", "Lease", "src/acme/lease.py", _callable_class=True, community=1),
        _node("tst", "Lease", "tests/acme/test_lease.py", _callable_class=True, community=1),
        _node("hub", "LeaseHub", "src/acme/hub.py", _callable_class=True, community=1),
        _node("solo", "Rooms", "test/acme/rooms_test.clj", _callable_class=True, community=1),
    ]
    links = [
        {"source": "hub", "target": "src", "relation": "uses"},
        {"source": "hub", "target": "tst", "relation": "uses"},
        {"source": "hub", "target": "solo", "relation": "uses"},
    ]
    index = KL.LinkIndex.build(nodes, links)
    dec = KL.link_text("The `LeaseHub` holds a Lease. Then Rooms opens.", index)
    assert "src" in dec.ids and "tst" not in dec.ids and "solo" not in dec.ids
    [item] = [p for p in dec.possible if p["reason"] == "test_file"]
    assert item["mention"] == "Rooms" and item["candidates"][0]["id"] == "solo"


# -- Code documents (D11) -----------------------------------------------------

_RELAY = """(ns com.blockether.vis.internal.gateway.relay
  (:require [com.blockether.vis.contract.wire :as wire]
            [com.blockether.vis.internal.gateway.push :as push]))

(defn send! [grant payload]
  ;; See the Gateway client concept.
  (wire/json-str (push/deliver payload)))
"""


def _fixture_index():
    data = json.loads(gzip.decompress((_FIXTURES / "graph.json.gz").read_bytes()))
    return dataclasses.replace(
        KL.LinkIndex.build(data["nodes"], data["links"]), total=data["total"]
    )


def _code_doc(src, name):
    from graphify import kb_ingest

    return kb_ingest.convert(src.encode(), name).body


def test_code_document_links_the_file_of_the_same_name():
    # D11: an upload of relay.clj was one fenced block, so it got no link and was an orphan.
    index = _fixture_index()
    dec = KL.link_text(_code_doc(_RELAY, "relay.clj"), index)
    first = dec.links[0]
    assert (first["id"], first["method"], first["confidence_score"]) == (
        "src_com_blockether_vis_internal_gateway_relay",
        "same_file",
        0.95,
    )
    ids = set(dec.ids)
    # The required namespaces and the alias-qualified var link as code names.
    assert "packages_vis_contract_src_com_blockether_vis_contract_wire_json_str" in ids
    # Names that relay.clj defines itself (send!) are inside the file link already.
    assert not any(x["mention"] == "send!" for x in dec.links)
    # The title also gives the file name when the fence has no title attribute.
    bare = "```clojure\n" + _RELAY + "```\n"
    assert KL.link_text(bare, index, title="relay.clj").links[0]["method"] == "same_file"


def test_code_document_with_several_files_of_its_name_asks_for_review():
    # D11 a: three files are called client.clj; one review item lists them, no link.
    index = _fixture_index()
    dec = KL.link_text(_code_doc(_RELAY, "client.clj"), index)
    assert not any(x["method"] == "same_file" for x in dec.links)
    items = [p for p in dec.possible if p["reason"] == "same_file_name"]
    assert len(items) == 1 and items[0]["candidates_total"] >= 3
    assert "src_com_blockether_vis_internal_gateway_client" in {
        c["id"] for c in items[0]["candidates"]
    }


def test_code_document_without_a_matching_file_links_its_unique_code_names():
    # D11 b: no graph file has the name, the unique code names still link.
    dec = KL.link_text(_code_doc(_RELAY, "notes_tool.clj"), _fixture_index())
    ids = set(dec.ids)
    assert not any(x["method"] == "same_file" for x in dec.links)
    # The namespace in the ns form is a code name, not the same file.
    assert any(i.startswith("src_com_blockether_vis_internal_gateway_relay_com") for i in ids)
    assert "packages_vis_contract_src_com_blockether_vis_contract_wire_json_str" in ids


def test_ambiguous_code_names_give_no_review_item_in_a_code_document():
    # D11 b: a code file would flood the review queue; the stats count the open names.
    nodes = [
        _node("a", "lease_for()", "pkg_a/lease.py"),
        _node("b", "lease_for()", "pkg_b/lease.py"),
        _node("u", "unique_helper()", "pkg_c/helper.py"),
    ]
    index = KL.LinkIndex.build(nodes, [])
    body = "```python\nx = lease_for(1)\ny = unique_helper()\n```\n"
    dec = KL.link_text(body, index)
    assert dec.ids == ["u"] and dec.possible == []
    assert dec.stats["ambiguous_code_names"] == 1
    # In prose the same ambiguous name is a review item.
    prose = KL.link_text("Call `lease_for` and `unique_helper` here.", index)
    assert [p["mention"] for p in prose.possible] == ["lease_for"]
    # A fenced block in a prose document stays unlinked (D11 c, FENCED_CODE_NAMES).
    mixed = KL.link_text("# Notes\n\nSome text.\n\n" + body, index)
    assert mixed.ids == [] and not KL.FENCED_CODE_NAMES
