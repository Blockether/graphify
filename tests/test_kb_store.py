"""The stores of the knowledge base (graphify.kb_store), the move of old stores into SQLite and the export import."""
import io
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from graphify import kb as kb_module
from graphify import kb_store
from graphify.kb import KBError, KnowledgeBase, import_export, read_overlay
from graphify.kb_store import (
    STORE_ENV,
    STORE_FILE,
    STORE_MARKER,
    MemoryStore,
    SQLiteStore,
    dumps,
    loads,
    open_store,
    read_store_overlay,
    register_store,
    store_factory,
    store_names,
)
from tests.kb_fixtures import make_graph


def test_json_helpers_keep_the_data_of_json():
    value = {"label": "Zażółć", "n": [1, 2.5, None, True], 3: "key"}
    assert loads(dumps(value)) == json.loads(json.dumps(value))
    assert loads(b'{"x": NaN}')["x"] != loads(b'{"x": NaN}')["x"]  # NaN reads as NaN
    assert dumps("\ud800")  # a lone surrogate goes to json


def test_overlay_table_keeps_order_and_pending_writes(tmp_path):
    store = SQLiteStore(tmp_path / STORE_FILE)
    nodes = store.overlay()["nodes"]
    nodes["x"] = {"id": "x"}
    nodes["y"] = {"id": "y"}
    assert list(nodes) == ["x", "y"] and store.pending()
    store.commit()
    nodes["x"] = {"id": "x", "label": "X"}
    nodes["z"] = {"id": "z"}
    del nodes["y"]
    assert list(nodes) == ["x", "z"] and "y" not in nodes and len(nodes) == 2
    store.discard()
    assert dict(nodes.items()) == {"x": {"id": "x"}, "y": {"id": "y"}}
    # A read decodes a new copy, so a change needs a new assignment.
    nodes["x"]["label"] = "X"
    assert "label" not in nodes["x"]
    store.close()
    saved = read_store_overlay(tmp_path / STORE_FILE)
    assert saved is not None and saved["nodes"] == {"x": {"id": "x"}, "y": {"id": "y"}}
    assert read_store_overlay(tmp_path / "missing.sqlite") is None


def test_a_small_table_answers_missing_ids_without_a_query(tmp_path):
    path = tmp_path / STORE_FILE
    store, other = SQLiteStore(path), SQLiteStore(path)
    nodes = store.overlay()["nodes"]
    nodes["a"] = {"id": "a"}
    store.commit()
    assert nodes.get("a") == {"id": "a"}
    db = store.db()
    assert db is not None
    queries: list[str] = []
    db.set_trace_callback(queries.append)
    assert "b" not in nodes and nodes.get("b") is None
    db.set_trace_callback(None)
    assert queries == []
    # The ids follow the commits of this store.
    nodes["b"] = {"id": "b"}
    del nodes["a"]
    store.commit()
    assert "a" not in nodes and nodes["b"] == {"id": "b"}
    # The commit of another process shows after discard, as a KB reload does it.
    other.overlay()["nodes"]["c"] = {"id": "c"}
    other.commit()
    store.discard()
    assert nodes["c"] == {"id": "c"}
    other.overlay()["nodes"]["d"] = {"id": "d"}
    other.commit()
    store.close()
    assert nodes["d"] == {"id": "d"}
    store.close()
    other.close()


def test_backup_copies_the_store(tmp_path):
    store = SQLiteStore(tmp_path / STORE_FILE)
    store.overlay()["edges"]["e"] = {"source": "a", "target": "b", "relation": "calls"}
    store.commit({"seq": 1, "op": "add_edge", "summary": "Add", "undo": []})
    store.backup(tmp_path / "copy.sqlite")
    copy = SQLiteStore(tmp_path / "copy.sqlite")
    assert copy.overlay()["edges"].to_dict() == store.overlay()["edges"].to_dict()
    assert copy.history_count() == 1 and copy.seq() == store.seq()


def test_history_view_reads_the_entries_on_demand(tmp_path):
    kb = KnowledgeBase(make_graph(tmp_path))
    first = kb.add_node("Gateway", cls="concept")["id"]
    kb.update_node(first, {"owner": "core"})
    count, newest = kb.history_state()
    assert len(kb.history) == count >= 2 and kb.history[-1]["seq"] == newest
    assert [e["seq"] for e in kb.history[-2:]] == [e["seq"] for e in list(kb.history)[-2:]]
    assert [e["seq"] for e in reversed(kb.history)][0] == newest


def test_a_change_reaches_a_new_process_before_the_flush(tmp_path):
    graph = make_graph(tmp_path)
    kb = KnowledgeBase(graph, autoflush=False)
    nid = kb.add_node("Gateway", cls="concept")["id"]
    kb.add_edge(nid, "a", "references")
    assert "Gateway" not in graph.read_text(encoding="utf-8")
    other = KnowledgeBase(graph, autoflush=False)
    assert other.get_node(nid)["node"]["label"] == "Gateway"
    assert other.history_state() == kb.history_state()


def test_a_flushed_store_opens_without_apply_overlay(tmp_path, monkeypatch):
    graph = make_graph(tmp_path)
    kb = KnowledgeBase(graph)
    nid = kb.add_node("Gateway", cls="concept")["id"]
    kb.flush()
    stats = {k: v for k, v in kb.stats().items() if k != "version"}
    kb.close()
    calls = []
    real = kb_module.apply_overlay
    monkeypatch.setattr(kb_module, "apply_overlay", lambda *a, **k: calls.append(1) or real(*a, **k))
    again = KnowledgeBase(graph)
    assert again.get_node(nid)["node"]["label"] == "Gateway" and calls == []
    assert {k: v for k, v in again.stats().items() if k != "version"} == stats
    again.close()
    # A rebuild writes a new graph.json without the overlay: the next load applies it again.
    data = json.loads(graph.read_text(encoding="utf-8"))
    data["nodes"] = [n for n in data["nodes"] if n["id"] != nid]
    graph.write_text(json.dumps(data), encoding="utf-8")
    rebuilt = KnowledgeBase(graph)
    assert rebuilt.get_node(nid)["node"]["label"] == "Gateway" and calls


def test_a_legacy_store_moves_into_sqlite(tmp_path):
    graph = make_graph(tmp_path)
    kb = KnowledgeBase(graph)
    nid = kb.add_node("Gateway", cls="concept")["id"]
    kb.update_node(nid, {"owner": "core"})
    overlay = read_overlay(graph.parent)
    entries = list(kb._store.history_entries())
    kb.close()
    folder = graph.parent / "kb"
    for name in (STORE_FILE, f"{STORE_FILE}-wal", f"{STORE_FILE}-shm"):
        (folder / name).unlink(missing_ok=True)
    (folder / "overlay.json").write_text(json.dumps(overlay), encoding="utf-8")
    (folder / "history.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    again = KnowledgeBase(graph)
    assert again.get_node(nid)["node"]["owner"] == "core"
    assert again.history_state() == (len(entries), entries[-1]["seq"])
    assert (folder / STORE_FILE).is_file() and read_overlay(graph.parent) == overlay
    assert not (folder / "overlay.json").exists() and (folder / "overlay.json.migrated").is_file()
    assert not (folder / "history.jsonl").exists() and (folder / "history.jsonl.migrated").is_file()
    again.undo()
    assert "owner" not in again.get_node(nid)["node"]


def test_export_json_is_the_export_store_answer(tmp_path):
    kb = KnowledgeBase(make_graph(tmp_path))
    kb.add_node("Gateway", cls="concept")
    data = json.loads(kb.export_json())
    assert data["files"][0]["path"] == "graph.json"
    assert len(data["files"][0]["json"]["nodes"]) == kb.stats()["nodes"]
    store = kb.export_store()
    assert {k: v for k, v in data.items() if k != "exported_at"} == {k: v for k, v in store.items() if k != "exported_at"}


def test_a_large_overlay_compresses_its_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_store, "ZDICT_ROWS", 50)
    path = tmp_path / STORE_FILE
    store = SQLiteStore(path)
    nodes = store.overlay()["nodes"]

    def record(i):
        return {"id": f"n{i}", "label": f"Node {i} payment", "file_type": "concept", "source_file": "kb"}

    def query(sql):
        db = store.db()
        assert db is not None
        return db.execute(sql)

    def rows():
        return [raw for (raw,) in query("SELECT data FROM ov_nodes")]

    def zdict():
        return query("SELECT value FROM meta WHERE key = 'zdict'").fetchone()

    for i in range(30):
        nodes[f"n{i}"] = record(i)
    store.commit()
    assert zdict() is None and not any(raw.startswith(kb_store._ZSTD_MAGIC) for raw in rows())
    for i in range(30, 60):
        nodes[f"n{i}"] = record(i)
    store.commit()
    # The commit past ZDICT_ROWS trains the dictionary and compresses the older rows too.
    trained = zdict()
    assert trained is not None and all(raw.startswith(kb_store._ZSTD_MAGIC) for raw in rows())
    nodes["n0"] = {**record(0), "owner": "core"}
    store.commit()
    assert zdict() == trained and nodes["n0"]["owner"] == "core"
    store.close()
    saved = read_store_overlay(path)
    assert saved is not None
    assert saved["nodes"] == {f"n{i}": {**record(i), **({"owner": "core"} if i == 0 else {})} for i in range(60)}


def test_an_import_applies_its_overlay_once(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    graph = make_graph(tmp_path / "a")
    kb = KnowledgeBase(graph)
    nid = kb.add_node("Gateway", cls="concept")["id"]
    kb.flush()
    exported = json.loads(kb.export_json())
    kb.close()
    out = tmp_path / "b" / "graphify-out"
    for item in exported["files"]:
        target = out / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(item["json"]) if "json" in item else item["text"], encoding="utf-8")
    calls = []
    real = kb_module.apply_overlay
    monkeypatch.setattr(kb_module, "apply_overlay", lambda *a, **k: calls.append(1) or real(*a, **k))
    imported = KnowledgeBase(out / "graph.json")
    assert imported.get_node(nid)["node"]["label"] == "Gateway" and len(calls) == 1
    assert (out / "kb" / "overlay.json.migrated").is_file() and not (out / "kb" / "overlay.json").exists()
    assert read_overlay(out) == read_overlay(graph.parent)
    imported.close()
    again = KnowledgeBase(out / "graph.json")
    assert again.get_node(nid)["node"]["label"] == "Gateway" and len(calls) == 1


@pytest.mark.parametrize("name", ["sqlite", "memory"])
def test_each_store_keeps_order_pending_writes_and_history(tmp_path, name):
    store = open_store(tmp_path, name)
    nodes = store.overlay()["nodes"]
    nodes["x"] = {"id": "x"}
    nodes["y"] = {"id": "y"}
    assert list(nodes) == ["x", "y"] and store.pending()
    store.commit({"seq": 1, "op": "add_node", "summary": "Add", "undo": [["drop_node", "x"]]})
    assert not store.pending() and store.seq() == 1 and store.history_count() == 1
    nodes["x"] = {"id": "x", "label": "X"}
    nodes["z"] = {"id": "z"}
    del nodes["y"]
    assert list(nodes) == ["x", "z"] and "y" not in nodes and len(nodes) == 2
    store.discard()
    assert nodes.to_dict() == {"x": {"id": "x"}, "y": {"id": "y"}}
    nodes["x"]["label"] = "X"
    assert "label" not in nodes["x"]
    entry = next(store.history_entries())
    assert entry["seq"] == 1 and entry["undo"] == [("drop_node", "x")]
    assert store.read_overlay()["nodes"] == {"x": {"id": "x"}, "y": {"id": "y"}}
    store.close()


def test_a_knowledge_base_runs_on_the_memory_store(tmp_path):
    graph = make_graph(tmp_path)
    kb = KnowledgeBase(graph, store="memory")
    nid = kb.add_node("Gateway", cls="concept")["id"]
    kb.update_node(nid, {"owner": "core"})
    assert kb.history_state()[0] >= 2
    kb.undo()
    assert "owner" not in kb.get_node(nid)["node"]
    kb.flush()
    kb.close()
    folder = graph.parent / "kb"
    assert not (folder / STORE_FILE).exists() and not (folder / STORE_MARKER).exists()
    assert "Gateway" in graph.read_text(encoding="utf-8")


def test_a_custom_store_marks_its_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_store, "_FACTORIES", dict(kb_store._FACTORIES))
    opened: list[Path] = []

    class SavedStore(MemoryStore):
        persistent = True

    def factory(kb_dir):
        opened.append(Path(kb_dir))
        return SavedStore(kb_dir)

    register_store("saved", factory)
    assert "saved" in store_names()
    with pytest.raises(ValueError, match="without ':'"):
        register_store("a:b", factory)
    graph = make_graph(tmp_path)
    kb = KnowledgeBase(graph, store="saved")
    kb.add_node("Gateway", cls="concept")
    kb.close()
    folder = graph.parent / "kb"
    assert json.loads((folder / STORE_MARKER).read_text(encoding="utf-8")) == {"store": "saved"}
    assert not (folder / STORE_FILE).exists()
    # The marker picks the store of the folder, and another name cannot hide its edits.
    again = KnowledgeBase(graph)
    assert isinstance(again._store, SavedStore) and opened == [folder, folder]
    again.close()
    with pytest.raises(KBError, match="saved"):
        KnowledgeBase(graph, store="sqlite")


def test_a_saved_module_store_needs_an_explicit_name(tmp_path, monkeypatch):
    monkeypatch.delenv(STORE_ENV, raising=False)
    spec = "graphify.kb_store:MemoryStore"
    assert store_factory(spec) is MemoryStore
    with pytest.raises(ValueError, match="unknown knowledge base store"):
        store_factory("no-such-store")
    with pytest.raises(ValueError, match="cannot load"):
        store_factory("graphify.no_such_module:Store")
    graph = make_graph(tmp_path)
    folder = graph.parent / "kb"
    folder.mkdir()
    # A cloned folder can name any module: only the caller can allow its code.
    (folder / STORE_MARKER).write_text(json.dumps({"store": spec}), encoding="utf-8")
    with pytest.raises(KBError, match="loads code"):
        KnowledgeBase(graph)
    KnowledgeBase(graph, store=spec).close()
    monkeypatch.setenv(STORE_ENV, spec)
    kb = KnowledgeBase(graph)
    assert isinstance(kb._store, MemoryStore)
    kb.close()


def test_import_export_writes_a_store_that_opens_without_apply_overlay(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    graph = make_graph(tmp_path / "a")
    kb = KnowledgeBase(graph)
    nid = kb.add_node("Gateway", cls="concept")["id"]
    kb.add_edge(nid, "a", "references")
    doc = kb.create_document("Design", body="# Design\n", nodes=[nid])["id"]
    kb.add_asset(doc, "chart.png", b"\x89PNG\r\n")
    kb.flush()
    # An export holds no history.
    stats = {k: v for k, v in kb.stats().items() if k not in ("version", "graph", "history")}
    exported = kb.export_json(assets=True)
    kb.close()
    out = tmp_path / "b" / "graphify-out"
    counts = import_export(out, exported)
    assert counts == {"nodes": stats["nodes"], "edges": stats["edges"], "documents": 1, "assets": 1}
    calls = []
    real = kb_module.apply_overlay
    monkeypatch.setattr(kb_module, "apply_overlay", lambda *a, **k: calls.append(1) or real(*a, **k))
    imported = KnowledgeBase(out / "graph.json")
    assert calls == [] and imported.get_node(nid)["node"]["label"] == "Gateway"
    assert {k: v for k, v in imported.stats().items() if k not in ("version", "graph", "history")} == stats
    assert read_overlay(out) == read_overlay(graph.parent)
    assert imported.get_document(doc)["body"] == "# Design\n"
    assert imported.asset_path(doc, "chart.png").read_bytes() == b"\x89PNG\r\n"
    imported.close()


def test_an_open_store_reads_a_store_that_replaced_its_folder(tmp_path):
    (tmp_path / "source").mkdir()
    kb = KnowledgeBase(make_graph(tmp_path / "source"))
    gateway = kb.add_node("Gateway", cls="concept")["id"]
    first = kb.export_json()
    invoices = kb.add_node("Invoices", cls="concept")["id"]
    second = kb.export_json()
    kb.close()
    place = tmp_path / "copy"
    import_export(place, first)
    reader = KnowledgeBase(place / "graph.json")
    assert reader.get_node(gateway)["node"]["label"] == "Gateway"
    with pytest.raises(KBError):
        reader.get_node(invoices)
    # A new copy replaces the folder, as a snapshot does, while the reader keeps its connection.
    import_export(tmp_path / "staging", second)
    place.rename(tmp_path / "old")
    (tmp_path / "staging").rename(place)
    shutil.rmtree(tmp_path / "old")
    assert reader.refresh_if_changed()
    assert reader.get_node(invoices)["node"]["label"] == "Invoices"
    reader.close()


def test_import_export_refuses_other_data(tmp_path):
    kb = KnowledgeBase(make_graph(tmp_path))
    kb.add_node("Gateway", cls="concept")
    exported = json.loads(kb.export_json())
    kb.close()
    out = tmp_path / "copy"
    for name, match in [
        ("../graph.json", "unsafe"),
        ("/etc/graph.json", "unsafe"),
        ("kb/.lock", "unsafe"),
        ("kb/store.sqlite", "does not hold"),
        ("kb/store.json", "does not hold"),
        ("assets/chart.png", "does not hold"),
    ]:
        bad = {**exported, "files": [*exported["files"], {"path": name, "text": "x"}]}
        with pytest.raises(KBError, match=match):
            import_export(out, bad)
    assert not out.exists()
    with pytest.raises(KBError, match="not JSON"):
        import_export(out, b"{")
    with pytest.raises(KBError, match="not a knowledge base export"):
        import_export(out, {"files": []})
    with pytest.raises(KBError, match="no graph.json"):
        import_export(out, {**exported, "files": [f for f in exported["files"] if f["path"] != "graph.json"]})
    assert import_export(out, exported)["nodes"] == len(exported["files"][0]["json"]["nodes"])
    with pytest.raises(KBError, match="already holds"):
        import_export(out, exported)


def test_validate_counts_every_finding_past_the_kept_head(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_module, "_REPORT_KEEP", 2)
    graph = make_graph(tmp_path)
    kb = KnowledgeBase(graph)
    ids = [kb.add_node(f"Concept {i}", cls="concept")["id"] for i in range(4)]
    kb.add_edge(ids[0], "a", "references")
    kb.add_edge(ids[1], "a", "mentions")
    kb.flush()
    kb.close()
    # A rebuild without the node a leaves two overlay edges out of the graph.
    data = json.loads(graph.read_text(encoding="utf-8"))
    data["nodes"] = [n for n in data["nodes"] if n["id"] != "a"]
    data["links"] = [link for link in data["links"] if "a" not in (link["source"], link["target"])]
    graph.write_text(json.dumps(data), encoding="utf-8")
    kb = KnowledgeBase(graph)
    # A limit past the kept head runs the full check again.
    every = kb.validate(limit=1000)["findings"]
    assert {f["severity"] for f in every} == {"warning", "info"}
    for include_info in (True, False):
        rows = every if include_info else [f for f in every if f["severity"] != "info"]
        counts, codes = Counter(f["severity"] for f in rows), Counter(f["code"] for f in rows)
        for limit in (0, 1, 2, 3, 1000):
            report = kb.validate(limit=limit, include_info=include_info)
            assert report["counts"] == {s: counts.get(s, 0) for s in kb_module.SEVERITIES}
            assert list(report["by_code"].items()) == codes.most_common()
            assert report["findings"] == rows[:limit]
            assert (report["total"], report["truncated"]) == (len(rows), len(rows) > limit)
    kb.close()


def test_a_graph_without_communities_votes_again_when_one_comes(tmp_path):
    def run(folder, *, reset):
        folder.mkdir()
        kb = KnowledgeBase(make_graph(folder))

        def flush():
            if reset:
                kb._seedless = None
            kb.flush()

        x = kb.add_node("Gateway", cls="concept")["id"]
        y = kb.add_node("Router", cls="concept")["id"]
        kb.add_edge(x, y, "references")
        flush()
        skipped = kb._seedless
        states = []
        kb.update_node("a", {"community": 7})
        kb.add_edge(y, "a", "references")
        for attrs in ({}, {"community": None}):
            if attrs:
                kb.update_node("a", attrs)
            flush()
            states.append({n["label"]: (n.get("community"), n.get("_kb_community")) for n in kb.data["nodes"]})
        kb.close()
        return skipped, states

    skipped, fast = run(tmp_path / "flag", reset=False)
    _skipped, full = run(tmp_path / "full", reset=True)
    assert skipped is True and fast == full
    assert fast[0]["Gateway"] == fast[0]["Router"] == (7, True)
    assert fast[1]["Gateway"] == (None, None)


def test_the_compact_writer_writes_the_data_of_json_dumps(monkeypatch):
    monkeypatch.setattr(kb_module, "_JSON_SLICE", 3)
    data = {
        "directed": True,
        "graph": {"name": "Zażółć"},
        "nodes": [{"id": f"n{i}", "label": f"Node {i} ✓", "w": i / 7} for i in range(50)],
        "links": [{"source": f"n{i}", "target": f"n{i + 1}"} for i in range(13)],
        "hyperedges": [],
    }
    for value in (data, {**data, "nodes": data["nodes"][:5]}, [1, 2], {1: "x"}):
        buf = io.BytesIO()
        kb_module._write_compact_json(buf, value)
        assert json.loads(buf.getvalue()) == json.loads(json.dumps(value))

    class Full(io.BytesIO):
        def write(self, part):
            raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        kb_module._write_compact_json(Full(), data)
