"""The SQLite store of the knowledge base (graphify.kb_store) and the move of old stores into it."""
import json

from graphify import kb as kb_module
from graphify import kb_store
from graphify.kb import KnowledgeBase, read_overlay
from graphify.kb_store import STORE_FILE, Store, dumps, loads, read_store_overlay
from tests.kb_fixtures import make_graph


def test_json_helpers_keep_the_data_of_json():
    value = {"label": "Zażółć", "n": [1, 2.5, None, True], 3: "key"}
    assert loads(dumps(value)) == json.loads(json.dumps(value))
    assert loads(b'{"x": NaN}')["x"] != loads(b'{"x": NaN}')["x"]  # NaN reads as NaN
    assert dumps("\ud800")  # a lone surrogate goes to json


def test_overlay_table_keeps_order_and_pending_writes(tmp_path):
    store = Store(tmp_path / STORE_FILE)
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
    store, other = Store(path), Store(path)
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
    store = Store(tmp_path / STORE_FILE)
    store.overlay()["edges"]["e"] = {"source": "a", "target": "b", "relation": "calls"}
    store.commit({"seq": 1, "op": "add_edge", "summary": "Add", "undo": []})
    store.backup(tmp_path / "copy.sqlite")
    copy = Store(tmp_path / "copy.sqlite")
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
    store = Store(path)
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
