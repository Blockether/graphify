"""Editable knowledge base over a graphify ``graph.json``.

The knowledge base keeps every edit in ``graphify-out/kb/overlay.json``:

- nodes and edges that it created or changed;
- attribute patches for nodes that the extractor created;
- tombstones for deleted nodes and edges.

``graph.json`` is the materialised result.  When ``graphify update`` rebuilds
the graph, the next load applies the overlay again, so manual knowledge stays.

Documents ("boxes") live in ``graphify-out/kb/documents/<id>/`` with a
markdown ``body.md``, ``meta.json`` and an ``assets/`` folder for images, PDFs
and other attachments.  Each document is also a ``document`` node in the graph
with ``mentions`` edges to the nodes that it names.

Every change is one history entry in ``kb/history.jsonl`` with the primitive
operations that undo it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from graphify.ontology import (
    ClassSpec,
    Ontology,
    OntologyError,
    class_from_dict,
    infer_ontology,
    load_ontology,
    ontology_path,
    relation_from_dict,
    RelationSpec,
    save_ontology,
    validate_graph,
)
from graphify.export import _CONFIDENCE_SCORE_DEFAULTS
from graphify.validate import VALID_CONFIDENCES, VALID_FILE_TYPES

KB_ORIGIN = "kb"
KB_SOURCE = "kb"
MENTIONS = "mentions"
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".csv", ".json", ".yaml", ".yml", ".toml", ".org"}
HTML_SUFFIXES = {".html", ".htm"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WIKI_RE = re.compile(r"\[\[([^\[\]|\n]{1,120})(?:\|([^\[\]\n]{1,80}))?\]\]")
_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
_RELATION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-. ]{0,79}$")
_MISSING = object()


class KBError(ValueError):
    """A request that the knowledge base refuses (HTTP 400)."""

    status = 400


class NotFound(KBError):
    status = 404


class Conflict(KBError):
    status = 409


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def edge_id(source: Any, target: Any, relation: Any) -> str:
    """Return the stable id of the edge ``source -relation-> target``."""
    raw = f"{source}\x00{target}\x00{relation}".encode("utf-8")
    return "e_" + hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:16]


def slugify(text: str, *, limit: int = 60) -> str:
    slug = _SLUG_RE.sub("_", str(text).lower()).strip("_")
    return slug[:limit].strip("_") or "item"


def safe_asset_name(name: str) -> str:
    """Reduce ``name`` to a plain file name that cannot leave its folder."""
    base = os.path.basename(str(name).replace("\\", "/")).strip()
    base = _SAFE_NAME_RE.sub("_", base).strip("._")
    if not base:
        raise KBError("asset name is empty")
    return base[:120]


def _dump_atomic(path: Path, data: Any, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if compact:
                json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _empty_graph() -> dict[str, Any]:
    return {
        "directed": True,
        "multigraph": False,
        "graph": {"schema_version": 1},
        "nodes": [],
        "links": [],
        "hyperedges": [],
    }


def _empty_overlay() -> dict[str, Any]:
    return {
        "version": 1,
        "nodes": {},
        "patches": {},
        "deleted_nodes": {},
        "edges": {},
        "deleted_edges": {},
    }


def _norm_label(label: str) -> str:
    text = str(label).strip().lower()
    if text.endswith("()"):
        text = text[:-2]
    return text


def _check_relation(relation: Any) -> str:
    name = str(relation or "").strip()
    if not _RELATION_RE.match(name):
        raise KBError(f"invalid relation name {relation!r}")
    return name


def _check_label(label: Any) -> str:
    text = str(label or "").strip()
    if not text:
        raise KBError("label is empty")
    if len(text) > 500 or "\n" in text:
        raise KBError("label must be one line of at most 500 characters")
    return text


def _check_attrs(attrs: Any) -> dict[str, Any]:
    if attrs is None:
        return {}
    if not isinstance(attrs, dict):
        raise KBError("attrs must be an object")
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        k = str(key)
        if not k or len(k) > 80:
            raise KBError(f"invalid attribute name {key!r}")
        out[k] = value
    return out


class KnowledgeBase:
    """A thread-safe, undoable editor for one graphify graph."""

    def __init__(self, graph_path: Path | str, *, autoflush: bool = True, create: bool = True) -> None:
        self.graph_path = Path(graph_path).resolve()
        self.out_dir = self.graph_path.parent
        self.kb_dir = self.out_dir / "kb"
        self.docs_dir = self.kb_dir / "documents"
        self.trash_dir = self.kb_dir / "trash"
        self.overlay_path = self.kb_dir / "overlay.json"
        self.history_path = self.kb_dir / "history.jsonl"
        self.onto_path = ontology_path(self.graph_path)
        self.autoflush = autoflush
        self._create = create
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._undo_log: list[tuple] | None = None
        self._tx_docs: set[str] = set()
        self._dirty = False
        self._graph_mtime: float | None = None
        self._cache: dict[str, tuple[int, Any]] = {}
        self.version = 0
        self.history: list[dict[str, Any]] = []
        self._undone: set[int] = set()
        self._load_history()
        self._load()

    # -- loading -------------------------------------------------------
    def _load(self) -> None:
        with self._lock:
            if self.graph_path.is_file():
                self._graph_mtime = self.graph_path.stat().st_mtime
                try:
                    data = json.loads(self.graph_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise KBError(f"cannot read {self.graph_path}: {exc}") from exc
            elif self._create:
                data = _empty_graph()
                self._graph_mtime = None
            else:
                raise NotFound(f"{self.graph_path} does not exist")
            if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
                raise KBError(f"{self.graph_path} is not a node-link graph")
            self.links_key = "links" if "links" in data or "edges" not in data else "edges"
            data.setdefault(self.links_key, [])
            self.data = data
            self._reindex()
            self.overlay = self._read_overlay()
            changed = self._apply_overlay()
            self.ontology_inferred = False
            onto = load_ontology(self.onto_path)
            if onto is None:
                onto = self._default_ontology()
                self.ontology_inferred = True
            self.ontology = onto
            self.version += 1
            self._cache.clear()
            if changed or not self.graph_path.is_file():
                self._dirty = True
                if self.autoflush:
                    self.flush()

    def _default_ontology(self) -> Ontology:
        """Infer the ontology from extracted knowledge only, plus the KB vocabulary.

        Manual nodes and edges stay out, so a new class or relation shows up as
        a finding until somebody adds it to the ontology.
        """
        base = {
            "nodes": [n for n in self.data["nodes"] if n.get("_origin") != KB_ORIGIN],
            "links": [x for x in self.data[self.links_key] if x.get("_origin") != KB_ORIGIN],
        }
        return with_kb_vocabulary(infer_ontology(base))

    def _reindex(self) -> None:
        self._nodes: dict[str, dict[str, Any]] = {}
        for node in self.data["nodes"]:
            self._nodes[str(node.get("id"))] = node
        self._edges: dict[str, list[dict[str, Any]]] = {}
        self._out: dict[str, list[dict[str, Any]]] = {}
        self._in: dict[str, list[dict[str, Any]]] = {}
        for link in self.data[self.links_key]:
            self._index_edge(link)

    def _index_edge(self, link: dict[str, Any]) -> None:
        s, t = str(link.get("source")), str(link.get("target"))
        self._edges.setdefault(edge_id(s, t, link.get("relation")), []).append(link)
        self._out.setdefault(s, []).append(link)
        self._in.setdefault(t, []).append(link)

    def _read_overlay(self) -> dict[str, Any]:
        overlay = _empty_overlay()
        if self.overlay_path.is_file():
            try:
                raw = json.loads(self.overlay_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise KBError(f"cannot read {self.overlay_path}: {exc}") from exc
            for key in overlay:
                if key != "version" and isinstance(raw.get(key), dict):
                    overlay[key] = raw[key]
        return overlay

    def _apply_overlay(self) -> bool:
        """Apply the overlay to the loaded graph; return True when it changed."""
        ov = self.overlay
        changed = False
        for nid in list(ov["deleted_nodes"]):
            if nid in self._nodes:
                for link in list(self._out.get(nid, [])) + list(self._in.get(nid, [])):
                    self._raw_drop_edge_record(link)
                self._raw_drop_node(nid)
                changed = True
        for nid, rec in ov["nodes"].items():
            if self._nodes.get(nid) != rec:
                self._raw_put_node(copy.deepcopy(rec))
                changed = True
        for nid, patch in ov["patches"].items():
            node = self._nodes.get(nid)
            if node is None:
                continue
            for key, value in patch.items():
                if value is None:
                    if key in node:
                        node.pop(key)
                        changed = True
                elif node.get(key, _MISSING) != value:
                    node[key] = copy.deepcopy(value)
                    changed = True
        for eid in list(ov["deleted_edges"]):
            for link in list(self._edges.get(eid, [])):
                self._raw_drop_edge_record(link)
                changed = True
        for eid, rec in ov["edges"].items():
            if str(rec.get("source")) not in self._nodes or str(rec.get("target")) not in self._nodes:
                continue
            existing = self._edges.get(eid)
            if not existing or existing[0] != rec:
                self._raw_put_edge(copy.deepcopy(rec))
                changed = True
        return changed

    def refresh_if_changed(self) -> bool:
        """Reload ``graph.json`` when another process rebuilt it."""
        with self._lock:
            if not self.graph_path.is_file():
                return False
            if self.graph_path.stat().st_mtime == self._graph_mtime:
                return False
            self._load()
            self._notify({"type": "reload", "version": self.version})
            return True

    # -- raw graph mutation (no overlay, no history) --------------------
    def _raw_put_node(self, rec: dict[str, Any]) -> None:
        nid = str(rec["id"])
        node = self._nodes.get(nid)
        if node is None:
            self.data["nodes"].append(rec)
            self._nodes[nid] = rec
        else:
            node.clear()
            node.update(rec)

    def _raw_drop_node(self, nid: str) -> None:
        node = self._nodes.pop(nid, None)
        if node is not None:
            self.data["nodes"] = [n for n in self.data["nodes"] if n is not node]
        self._out.pop(nid, None)
        self._in.pop(nid, None)

    def _raw_put_edge(self, rec: dict[str, Any]) -> None:
        eid = edge_id(rec.get("source"), rec.get("target"), rec.get("relation"))
        existing = self._edges.get(eid)
        if existing:
            existing[0].clear()
            existing[0].update(rec)
            return
        self.data[self.links_key].append(rec)
        self._index_edge(rec)

    def _raw_drop_edge_record(self, link: dict[str, Any]) -> None:
        s, t = str(link.get("source")), str(link.get("target"))
        eid = edge_id(s, t, link.get("relation"))
        key = self.links_key
        self.data[key] = [x for x in self.data[key] if x is not link]
        for index, k in ((self._edges, eid), (self._out, s), (self._in, t)):
            bucket = index.get(k)
            if bucket is not None:
                bucket[:] = [x for x in bucket if x is not link]
                if not bucket:
                    index.pop(k, None)

    # -- primitives (overlay + undo log) --------------------------------
    def _log(self, inverse: tuple) -> None:
        if self._undo_log is not None:
            self._undo_log.append(inverse)

    def _p_put_node(self, rec: dict[str, Any]) -> None:
        nid = str(rec["id"])
        before = copy.deepcopy(self._nodes.get(nid))
        self._raw_put_node(rec)
        ov = self.overlay
        ov["deleted_nodes"].pop(nid, None)
        if rec.get("_origin") == KB_ORIGIN:
            ov["nodes"][nid] = copy.deepcopy(rec)
            ov["patches"].pop(nid, None)
        else:
            ov["nodes"].pop(nid, None)
            base = before or {}
            patch = ov["patches"].setdefault(nid, {})
            for key in set(base) | set(rec):
                new = rec.get(key, _MISSING)
                if base.get(key, _MISSING) != new:
                    patch[key] = None if new is _MISSING else copy.deepcopy(new)
            if not patch:
                ov["patches"].pop(nid, None)
        self._log(("put_node", before) if before is not None else ("drop_node", nid))

    def _p_drop_node(self, nid: str) -> None:
        before = copy.deepcopy(self._nodes.get(nid))
        if before is None:
            return
        self._raw_drop_node(nid)
        ov = self.overlay
        ov["nodes"].pop(nid, None)
        ov["patches"].pop(nid, None)
        if before.get("_origin") != KB_ORIGIN:
            ov["deleted_nodes"][nid] = now_iso()
        self._log(("put_node", before))

    def _p_put_edge(self, rec: dict[str, Any]) -> None:
        eid = edge_id(rec.get("source"), rec.get("target"), rec.get("relation"))
        existing = self._edges.get(eid)
        before = copy.deepcopy(existing[0]) if existing else None
        self._raw_put_edge(rec)
        self.overlay["deleted_edges"].pop(eid, None)
        self.overlay["edges"][eid] = copy.deepcopy(rec)
        self._log(("put_edge", before) if before is not None else ("drop_edge", eid))

    def _p_drop_edge(self, eid: str) -> None:
        records = list(self._edges.get(eid, []))
        if not records:
            return
        for link in records:
            self._raw_drop_edge_record(link)
        self.overlay["edges"].pop(eid, None)
        if any(link.get("_origin") != KB_ORIGIN for link in records):
            self.overlay["deleted_edges"][eid] = now_iso()
        for link in reversed(records):
            self._log(("put_edge", copy.deepcopy(link)))

    def _p_put_ontology(self, onto: Ontology) -> None:
        before = None if self.ontology_inferred else self.ontology.to_dict()
        self.ontology = onto
        self.ontology_inferred = False
        save_ontology(onto, self.onto_path)
        self._log(("put_ontology", before))

    def _p_put_doc(self, doc_id: str, meta: dict[str, Any], body: str | None) -> None:
        folder = self._doc_dir(doc_id)
        before_meta = self._read_meta(doc_id)
        before_body = None
        if body is not None and (folder / "body.md").is_file():
            before_body = (folder / "body.md").read_text(encoding="utf-8")
        _dump_atomic(folder / "meta.json", meta)
        if body is not None:
            _write_text_atomic(folder / "body.md", body)
        if before_meta is None:
            self._log(("drop_doc", doc_id))
        else:
            self._log(("put_doc", doc_id, before_meta, before_body))

    def _p_drop_doc(self, doc_id: str) -> None:
        folder = self._doc_dir(doc_id)
        if not folder.is_dir():
            return
        dest = self._trash_slot(f"doc-{doc_id}")
        shutil.move(str(folder), str(dest))
        self._log(("restore_doc", doc_id, str(dest)))

    def _p_put_asset(self, doc_id: str, name: str, data: bytes) -> None:
        path = self._doc_dir(doc_id) / "assets" / name
        trash = None
        if path.is_file():
            trash = self._trash_slot(f"asset-{doc_id}-{name}")
            shutil.move(str(path), str(trash))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".asset.", dir=str(path.parent))
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        if trash is None:
            self._log(("drop_asset", doc_id, name))
        else:
            self._log(("restore_asset", doc_id, name, str(trash)))

    def _p_drop_asset(self, doc_id: str, name: str) -> None:
        path = self._doc_dir(doc_id) / "assets" / name
        if not path.is_file():
            return
        trash = self._trash_slot(f"asset-{doc_id}-{name}")
        shutil.move(str(path), str(trash))
        self._log(("restore_asset", doc_id, name, str(trash)))

    def _run_inverse(self, step: tuple) -> None:
        kind = step[0]
        if kind == "put_node":
            self._p_put_node(copy.deepcopy(step[1]))
        elif kind == "drop_node":
            self._p_drop_node(step[1])
        elif kind == "put_edge":
            self._p_put_edge(copy.deepcopy(step[1]))
        elif kind == "drop_edge":
            self._p_drop_edge(step[1])
        elif kind == "put_ontology":
            if step[1] is None:
                before = self.ontology.to_dict()
                try:
                    self.onto_path.unlink()
                except FileNotFoundError:
                    pass
                self.ontology = self._default_ontology()
                self.ontology_inferred = True
                self._log(("put_ontology", before))
            else:
                self._p_put_ontology(Ontology.from_dict(step[1]))
        elif kind == "put_doc":
            self._p_put_doc(step[1], step[2], step[3])
        elif kind == "drop_doc":
            self._p_drop_doc(step[1])
        elif kind == "restore_doc":
            folder = self._doc_dir(step[1])
            if folder.exists():
                self._p_drop_doc(step[1])
            shutil.move(step[2], str(folder))
            self._log(("drop_doc", step[1]))
        elif kind == "drop_asset":
            self._p_drop_asset(step[1], step[2])
        elif kind == "restore_asset":
            path = self._doc_dir(step[1]) / "assets" / step[2]
            if path.exists():
                self._p_drop_asset(step[1], step[2])
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(step[3], str(path))
            self._log(("drop_asset", step[1], step[2]))
        else:
            raise KBError(f"unknown undo step {kind!r}")

    def _trash_slot(self, stem: str) -> Path:
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        for n in range(1000):
            slot = self.trash_dir / f"{safe_asset_name(stem)}-{stamp}-{n}"
            if not slot.exists():
                return slot
        raise KBError("trash is full")

    # -- transactions ---------------------------------------------------
    @contextmanager
    def _tx(
        self,
        op: str,
        summary: str,
        actor: str | None,
        *,
        extra: dict[str, Any] | None = None,
        always: bool = False,
    ) -> Iterator[list[tuple]]:
        with self._lock:
            if self._undo_log is not None:
                yield self._undo_log
                return
            self.refresh_if_changed()
            log: list[tuple] = []
            overlay_before = copy.deepcopy(self.overlay)
            self._undo_log = log
            try:
                yield log
            except BaseException:
                self._undo_log = None
                for step in reversed(log):
                    try:
                        self._run_inverse(step)
                    except Exception:
                        pass
                self.overlay = overlay_before
                self._cache.clear()
                raise
            finally:
                self._undo_log = None
            if log or always:
                self._commit(op, summary, actor, log, extra)

    def _commit(
        self,
        op: str,
        summary: str,
        actor: str | None,
        log: list[tuple],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _dump_atomic(self.overlay_path, self.overlay)
        seq = (self.history[-1]["seq"] + 1) if self.history else 1
        entry = {"seq": seq, "ts": now_iso(), "actor": actor or "api", "op": op, "summary": summary, "undo": log}
        entry.update(extra or {})
        self.history.append(entry)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        self.version += 1
        self._cache.clear()
        self._dirty = True
        if self.autoflush:
            self.flush()
        self._notify({"type": "change", "version": self.version, "seq": seq, "op": op, "summary": summary, "actor": entry["actor"]})
        return entry

    def flush(self) -> bool:
        """Write ``graph.json`` when it has unsaved changes."""
        with self._lock:
            if not self._dirty:
                return False
            _dump_atomic(self.graph_path, self.data, compact=True)
            self._graph_mtime = self.graph_path.stat().st_mtime
            self._dirty = False
            return True

    def _load_history(self) -> None:
        if not self.history_path.is_file():
            return
        with open(self.history_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry["undo"] = [tuple(step) for step in entry.get("undo", [])]
                self.history.append(entry)
                if entry.get("op") == "undo" and entry.get("target"):
                    self._undone.add(int(entry["target"]))

    # -- events ----------------------------------------------------------
    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _notify(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass

    # -- reads -----------------------------------------------------------
    def _cached(self, key: str, build: Callable[[], Any]) -> Any:
        hit = self._cache.get(key)
        if hit and hit[0] == self.version:
            return hit[1]
        value = build()
        self._cache[key] = (self.version, value)
        return value

    def node_class(self, node: dict[str, Any]) -> str:
        return self.ontology.node_class(node) or "unclassified"

    def stats(self) -> dict[str, Any]:
        with self._lock:
            classes: dict[str, int] = {}
            for node in self.data["nodes"]:
                c = self.node_class(node)
                classes[c] = classes.get(c, 0) + 1
            relations: dict[str, int] = {}
            for link in self.data[self.links_key]:
                r = str(link.get("relation"))
                relations[r] = relations.get(r, 0) + 1
            return {
                "version": self.version,
                "graph": str(self.graph_path),
                "nodes": len(self._nodes),
                "edges": len(self.data[self.links_key]),
                "documents": len(self.list_documents()),
                "kb_nodes": len(self.overlay["nodes"]),
                "kb_edges": len(self.overlay["edges"]),
                "classes": dict(sorted(classes.items(), key=lambda kv: -kv[1])),
                "relations": dict(sorted(relations.items(), key=lambda kv: -kv[1])),
                "ontology_inferred": self.ontology_inferred,
                "history": len(self.history),
            }

    def graph_view(self) -> dict[str, Any]:
        """Return a compact graph for the viewer.

        ``nodes`` rows are ``[id, label, class, community, degree, kb]`` and
        ``edges`` rows are ``[source_index, target_index, relation_index]``.
        """
        with self._lock:
            return self._cached("view", self._build_view)

    def _build_view(self) -> dict[str, Any]:
        index: dict[str, int] = {}
        rows: list[list[Any]] = []
        for i, node in enumerate(self.data["nodes"]):
            nid = str(node.get("id"))
            index[nid] = i
            degree = len(self._out.get(nid, ())) + len(self._in.get(nid, ()))
            community = node.get("community")
            rows.append([
                nid,
                str(node.get("label") or nid),
                self.node_class(node),
                community if isinstance(community, int) else -1,
                degree,
                1 if node.get("_origin") == KB_ORIGIN else 0,
            ])
        relations: list[str] = []
        rel_index: dict[str, int] = {}
        edges: list[list[int]] = []
        for link in self.data[self.links_key]:
            s = index.get(str(link.get("source")))
            t = index.get(str(link.get("target")))
            if s is None or t is None:
                continue
            rel = str(link.get("relation"))
            r = rel_index.get(rel)
            if r is None:
                r = rel_index[rel] = len(relations)
                relations.append(rel)
            edges.append([s, t, r])
        communities: dict[str, str] = {}
        for node in self.data["nodes"]:
            c, name = node.get("community"), node.get("community_name")
            if isinstance(c, int) and name and str(c) not in communities:
                communities[str(c)] = str(name)
        return {
            "version": self.version,
            "directed": bool(self.data.get("directed", True)),
            "nodes": rows,
            "edges": edges,
            "relations": relations,
            "communities": communities,
            "classes": self._class_colors(),
        }

    def _class_colors(self) -> dict[str, str | None]:
        return {name: spec.color for name, spec in self.ontology.classes.items()}

    def _edge_view(self, link: dict[str, Any], other: str) -> dict[str, Any]:
        other_node = self._nodes.get(other) or {}
        return {
            "id": edge_id(link.get("source"), link.get("target"), link.get("relation")),
            "source": link.get("source"),
            "target": link.get("target"),
            "relation": link.get("relation"),
            "confidence": link.get("confidence"),
            "kb": link.get("_origin") == KB_ORIGIN,
            "other": other,
            "other_label": other_node.get("label") or other,
            "other_class": self.node_class(other_node) if other_node else None,
        }

    def get_node(self, nid: str, *, limit: int = 200) -> dict[str, Any]:
        with self._lock:
            node = self._nodes.get(str(nid))
            if node is None:
                raise NotFound(f"node {nid} does not exist")
            out = self._out.get(str(nid), [])
            inc = self._in.get(str(nid), [])
            return {
                "node": copy.deepcopy(node),
                "class": self.node_class(node),
                "kb": node.get("_origin") == KB_ORIGIN,
                "out": [self._edge_view(x, str(x.get("target"))) for x in out[:limit]],
                "in": [self._edge_view(x, str(x.get("source"))) for x in inc[:limit]],
                "out_total": len(out),
                "in_total": len(inc),
                "properties": [p.__dict__ for p in self.ontology.properties_for(self.node_class(node))],
            }

    def get_edge(self, eid: str) -> dict[str, Any]:
        with self._lock:
            records = self._edges.get(eid)
            if not records:
                raise NotFound(f"edge {eid} does not exist")
            rec = copy.deepcopy(records[0])
            rec["id"] = eid
            return rec

    def find_node(self, label: str) -> dict[str, Any] | None:
        """Return the node whose label matches ``label`` (case-insensitive)."""
        key = _norm_label(label)
        with self._lock:
            best = None
            for node in self.data["nodes"]:
                if _norm_label(node.get("label") or "") == key:
                    if node.get("_origin") == KB_ORIGIN:
                        return node
                    best = best or node
            return best

    def search(self, query: str, *, limit: int = 50, cls: str | None = None) -> list[dict[str, Any]]:
        q = str(query or "").strip().lower()
        if not q:
            return []
        terms = q.split()
        hits: list[tuple[float, dict[str, Any]]] = []
        with self._lock:
            for node in self.data["nodes"]:
                node_cls = self.node_class(node)
                if cls and node_cls != cls:
                    continue
                label = str(node.get("label") or "").lower()
                nid = str(node.get("id")).lower()
                src = str(node.get("source_file") or "").lower()
                score = 0.0
                if label == q:
                    score += 100
                elif label.startswith(q):
                    score += 60
                elif q in label:
                    score += 35
                elif all(t in label for t in terms):
                    score += 25
                if q in nid:
                    score += 8
                if q in src:
                    score += 4
                if score:
                    score += min(len(self._out.get(str(node.get("id")), ())), 40) / 40
                    hits.append((score, {
                        "kind": "node",
                        "id": node.get("id"),
                        "label": node.get("label"),
                        "class": node_cls,
                        "source_file": node.get("source_file"),
                        "score": round(score, 2),
                    }))
            if not cls or cls == "document":
                for meta in self.list_documents():
                    title = str(meta.get("title") or "").lower()
                    body = self._read_body(meta["id"])
                    low = body.lower()
                    score = 0.0
                    if q in title:
                        score += 50
                    pos = low.find(q)
                    if pos >= 0:
                        score += 15
                    if score:
                        snippet = body[max(0, pos - 60): pos + 120] if pos >= 0 else body[:160]
                        hits.append((score, {
                            "kind": "document",
                            "id": meta["id"],
                            "node": meta.get("node_id"),
                            "label": meta.get("title"),
                            "class": "document",
                            "snippet": " ".join(snippet.split()),
                            "score": score,
                        }))
        hits.sort(key=lambda h: -h[0])
        return [h[1] for h in hits[: max(1, int(limit))]]

    # -- node and edge writes -------------------------------------------
    def _new_node_id(self, label: str) -> str:
        base = f"kb:{slugify(label)}"
        nid, n = base, 2
        while nid in self._nodes:
            nid = f"{base}_{n}"
            n += 1
        return nid

    def add_node(
        self,
        label: str,
        *,
        cls: str | None = None,
        node_id: str | None = None,
        attrs: dict[str, Any] | None = None,
        file_type: str | None = None,
        source_file: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Create a node and return it.  Raise Conflict for a used id."""
        label = _check_label(label)
        extra = _check_attrs(attrs)
        ftype = file_type or "concept"
        if ftype not in VALID_FILE_TYPES:
            raise KBError(f"file_type must be one of {sorted(VALID_FILE_TYPES)}")
        with self._tx("add_node", f"Add node {label}", actor):
            nid = str(node_id).strip() if node_id else self._new_node_id(label)
            if not nid or len(nid) > 300:
                raise KBError("invalid node id")
            if nid in self._nodes:
                raise Conflict(f"node {nid} already exists")
            stamp = now_iso()
            rec: dict[str, Any] = {k: v for k, v in extra.items() if v is not None}
            rec.update({
                "id": nid,
                "label": label,
                "file_type": ftype,
                "source_file": source_file or KB_SOURCE,
                "norm_label": _norm_label(label),
                "_origin": KB_ORIGIN,
                "created_at": stamp,
                "updated_at": stamp,
                "actor": actor or "api",
            })
            if cls:
                rec["class"] = str(cls)
            self._p_put_node(rec)
            return copy.deepcopy(rec)

    def update_node(self, nid: str, attrs: dict[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        """Set attributes on a node.  A ``None`` value removes the attribute."""
        changes = _check_attrs(attrs)
        if "id" in changes:
            raise KBError("a node id cannot change; create a new node instead")
        with self._tx("update_node", f"Update node {nid}", actor):
            node = self._nodes.get(str(nid))
            if node is None:
                raise NotFound(f"node {nid} does not exist")
            rec = copy.deepcopy(node)
            for key, value in changes.items():
                if value is None:
                    if key in ("label", "file_type", "source_file"):
                        raise KBError(f"{key} is required")
                    rec.pop(key, None)
                else:
                    rec[key] = value
            if "label" in changes:
                rec["label"] = _check_label(rec["label"])
                rec["norm_label"] = _norm_label(rec["label"])
            if rec.get("file_type") not in VALID_FILE_TYPES:
                raise KBError(f"file_type must be one of {sorted(VALID_FILE_TYPES)}")
            rec["updated_at"] = now_iso()
            rec["actor"] = actor or "api"
            self._p_put_node(rec)
            if rec.get("doc_id") and "label" in changes:
                meta = self._read_meta(rec["doc_id"])
                if meta is not None:
                    meta["title"] = rec["label"]
                    meta["updated_at"] = rec["updated_at"]
                    self._p_put_doc(rec["doc_id"], meta, None)
            return copy.deepcopy(rec)

    def delete_node(self, nid: str, *, actor: str | None = None) -> dict[str, Any]:
        """Delete a node with its edges.  A document node deletes its document."""
        with self._tx("delete_node", f"Delete node {nid}", actor):
            node = self._nodes.get(str(nid))
            if node is None:
                raise NotFound(f"node {nid} does not exist")
            if node.get("doc_id") and self._doc_dir(str(node["doc_id"])).is_dir():
                return self.delete_document(str(node["doc_id"]), actor=actor)
            removed = 0
            for link in list(self._out.get(str(nid), [])) + list(self._in.get(str(nid), [])):
                eid = edge_id(link.get("source"), link.get("target"), link.get("relation"))
                if eid in self._edges:
                    self._p_drop_edge(eid)
                    removed += 1
            self._p_drop_node(str(nid))
            return {"deleted": str(nid), "edges_deleted": removed}

    def add_edge(
        self,
        source: str,
        target: str,
        relation: str,
        *,
        confidence: str = "EXTRACTED",
        attrs: dict[str, Any] | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Create the edge, or update it when it exists.  Return it with ``created``."""
        rel = _check_relation(relation)
        extra = _check_attrs(attrs)
        conf = str(confidence or "EXTRACTED").upper()
        if conf not in VALID_CONFIDENCES:
            raise KBError(f"confidence must be one of {sorted(VALID_CONFIDENCES)}")
        with self._tx("add_edge", f"Link {source} {rel} {target}", actor):
            s, t = str(source), str(target)
            for end in (s, t):
                if end not in self._nodes:
                    raise NotFound(f"node {end} does not exist")
            eid = edge_id(s, t, rel)
            existing = self._edges.get(eid)
            if existing:
                rec = copy.deepcopy(existing[0])
                rec.update({k: v for k, v in extra.items() if v is not None})
                rec["confidence"] = conf
                created = False
            else:
                rec = {k: v for k, v in extra.items() if v is not None}
                rec.update({
                    "source": s,
                    "target": t,
                    "relation": rel,
                    "confidence": conf,
                    "confidence_score": _CONFIDENCE_SCORE_DEFAULTS.get(conf, _CONFIDENCE_SCORE_DEFAULTS["AMBIGUOUS"]),
                    "source_file": KB_SOURCE,
                    "weight": float(extra.get("weight", 1.0) or 1.0),
                    "_origin": KB_ORIGIN,
                    "created_at": now_iso(),
                    "actor": actor or "api",
                })
                created = True
            self._p_put_edge(rec)
            out = copy.deepcopy(rec)
            out["id"] = eid
            out["created"] = created
            return out

    def update_edge(self, eid: str, attrs: dict[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        changes = _check_attrs(attrs)
        for key in ("source", "target", "relation"):
            if key in changes:
                raise KBError(f"{key} cannot change; delete the edge and add a new one")
        with self._tx("update_edge", f"Update edge {eid}", actor):
            records = self._edges.get(eid)
            if not records:
                raise NotFound(f"edge {eid} does not exist")
            rec = copy.deepcopy(records[0])
            for key, value in changes.items():
                if value is None:
                    rec.pop(key, None)
                else:
                    rec[key] = value
            if str(rec.get("confidence", "EXTRACTED")).upper() not in VALID_CONFIDENCES:
                raise KBError(f"confidence must be one of {sorted(VALID_CONFIDENCES)}")
            self._p_put_edge(rec)
            out = copy.deepcopy(rec)
            out["id"] = eid
            return out

    def delete_edge(self, eid: str, *, actor: str | None = None) -> dict[str, Any]:
        with self._tx("delete_edge", f"Delete edge {eid}", actor):
            if eid not in self._edges:
                raise NotFound(f"edge {eid} does not exist")
            self._p_drop_edge(eid)
            return {"deleted": eid}

    def batch(self, ops: list[dict[str, Any]], *, actor: str | None = None) -> list[Any]:
        """Run many operations as one undoable change.  Any failure rolls all back."""
        if not isinstance(ops, list) or not ops:
            raise KBError("ops must be a non-empty list")
        if len(ops) > 10_000:
            raise KBError("a batch holds at most 10000 operations")
        results: list[Any] = []
        with self._tx("batch", f"Batch of {len(ops)} operations", actor):
            for i, op in enumerate(ops):
                if not isinstance(op, dict):
                    raise KBError(f"op {i} is not an object")
                try:
                    results.append(self._dispatch(op, actor))
                except KBError as exc:
                    raise type(exc)(f"op {i} ({op.get('op')}): {exc}") from exc
        return results

    def _dispatch(self, op: dict[str, Any], actor: str | None) -> Any:
        kind = op.get("op")
        if kind == "add_node":
            return self.add_node(
                op.get("label", ""),
                cls=op.get("class"),
                node_id=op.get("id"),
                attrs=op.get("attrs"),
                file_type=op.get("file_type"),
                source_file=op.get("source_file"),
                actor=actor,
            )
        if kind == "update_node":
            return self.update_node(str(op.get("id")), op.get("attrs") or {}, actor=actor)
        if kind == "delete_node":
            return self.delete_node(str(op.get("id")), actor=actor)
        if kind == "add_edge":
            return self.add_edge(
                str(op.get("source")),
                str(op.get("target")),
                str(op.get("relation")),
                confidence=op.get("confidence") or "EXTRACTED",
                attrs=op.get("attrs"),
                actor=actor,
            )
        if kind == "update_edge":
            return self.update_edge(self._edge_ref(op), op.get("attrs") or {}, actor=actor)
        if kind == "delete_edge":
            return self.delete_edge(self._edge_ref(op), actor=actor)
        if kind == "add_document":
            return self.create_document(
                op.get("title", ""),
                body=op.get("body", ""),
                tags=op.get("tags"),
                nodes=op.get("nodes"),
                actor=actor,
            )
        if kind == "update_document":
            return self.update_document(
                str(op.get("id")),
                title=op.get("title"),
                body=op.get("body"),
                tags=op.get("tags"),
                nodes=op.get("nodes"),
                actor=actor,
            )
        if kind == "delete_document":
            return self.delete_document(str(op.get("id")), actor=actor)
        raise KBError(f"unknown op {kind!r}")

    def _edge_ref(self, op: dict[str, Any]) -> str:
        if op.get("id"):
            return str(op["id"])
        return edge_id(op.get("source"), op.get("target"), op.get("relation"))

    # -- ontology --------------------------------------------------------
    def ontology_view(self) -> dict[str, Any]:
        with self._lock:
            data = self.ontology.to_dict()
            data["inferred"] = self.ontology_inferred
            data["problems"] = self.ontology.problems()
            return data

    def set_ontology(self, data: dict[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        try:
            onto = Ontology.from_dict(data)
        except OntologyError as exc:
            raise KBError(str(exc)) from exc
        with self._tx("set_ontology", "Replace the ontology", actor):
            self._p_put_ontology(onto)
        return self.ontology_view()

    def infer_ontology(self, *, save: bool = False, actor: str | None = None) -> dict[str, Any]:
        with self._lock:
            onto = with_kb_vocabulary(infer_ontology(self.data, class_key=self.ontology.class_key))
        if save:
            with self._tx("infer_ontology", "Infer the ontology from the graph", actor):
                self._p_put_ontology(onto)
            return self.ontology_view()
        data = onto.to_dict()
        data["inferred"] = True
        data["problems"] = onto.problems()
        return data

    def put_class(self, raw: dict[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        try:
            spec = class_from_dict(raw)
        except OntologyError as exc:
            raise KBError(str(exc)) from exc
        with self._tx("put_class", f"Define class {spec.name}", actor):
            onto = Ontology.from_dict(self.ontology.to_dict())
            onto.classes[spec.name] = spec
            self._p_put_ontology(onto)
        return self.ontology_view()

    def delete_class(self, name: str, *, actor: str | None = None) -> dict[str, Any]:
        with self._tx("delete_class", f"Remove class {name}", actor):
            if name not in self.ontology.classes:
                raise NotFound(f"class {name} does not exist")
            onto = Ontology.from_dict(self.ontology.to_dict())
            onto.classes.pop(name)
            self._p_put_ontology(onto)
        return self.ontology_view()

    def put_relation(self, raw: dict[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        try:
            spec = relation_from_dict(raw)
        except OntologyError as exc:
            raise KBError(str(exc)) from exc
        with self._tx("put_relation", f"Define relation {spec.name}", actor):
            onto = Ontology.from_dict(self.ontology.to_dict())
            onto.relations[spec.name] = spec
            self._p_put_ontology(onto)
        return self.ontology_view()

    def delete_relation(self, name: str, *, actor: str | None = None) -> dict[str, Any]:
        with self._tx("delete_relation", f"Remove relation {name}", actor):
            if name not in self.ontology.relations:
                raise NotFound(f"relation {name} does not exist")
            onto = Ontology.from_dict(self.ontology.to_dict())
            onto.relations.pop(name)
            self._p_put_ontology(onto)
        return self.ontology_view()

    def validate(self, *, limit: int = 500, include_info: bool = True) -> dict[str, Any]:
        """Return the consistency report of the graph against the ontology."""
        with self._lock:
            key = f"validate:{int(limit)}:{int(bool(include_info))}"
            return self._cached(key, lambda: validate_graph(self.data, self.ontology, limit=limit, include_info=include_info))

    # -- history ---------------------------------------------------------
    def history_view(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            for entry in reversed(self.history[-max(1, int(limit)):]):
                rows.append({
                    "seq": entry["seq"],
                    "ts": entry.get("ts"),
                    "actor": entry.get("actor"),
                    "op": entry.get("op"),
                    "summary": entry.get("summary"),
                    "steps": len(entry.get("undo", [])),
                    "undone": entry["seq"] in self._undone,
                    "target": entry.get("target"),
                })
            return rows

    def undo(self, *, seq: int | None = None, actor: str | None = None) -> dict[str, Any]:
        """Undo the latest change (or change ``seq``) that is not undone yet."""
        with self._lock:
            target = None
            for entry in reversed(self.history):
                if entry.get("op") == "undo" or entry["seq"] in self._undone:
                    continue
                if seq is None or entry["seq"] == int(seq):
                    target = entry
                    break
            if target is None:
                raise NotFound("there is no change to undo")
            self._undone.add(target["seq"])
            try:
                with self._tx(
                    "undo",
                    f"Undo {target.get('summary')}",
                    actor,
                    extra={"target": target["seq"]},
                    always=True,
                ):
                    for step in reversed(target.get("undo", [])):
                        self._run_inverse(tuple(step))
            except BaseException:
                self._undone.discard(target["seq"])
                raise
            return {"undone": target["seq"], "summary": target.get("summary"), "version": self.version}

    # -- documents -------------------------------------------------------
    def _doc_dir(self, doc_id: str) -> Path:
        if not re.match(r"^[a-z0-9][a-z0-9_\-]{0,80}$", str(doc_id)):
            raise NotFound(f"document {doc_id} does not exist")
        return self.docs_dir / str(doc_id)

    def _read_meta(self, doc_id: str) -> dict[str, Any] | None:
        path = self._doc_dir(doc_id) / "meta.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _read_body(self, doc_id: str) -> str:
        path = self._doc_dir(doc_id) / "body.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def list_documents(self) -> list[dict[str, Any]]:
        if not self.docs_dir.is_dir():
            return []
        out = []
        for folder in sorted(self.docs_dir.iterdir()):
            if folder.is_dir() and not folder.name.startswith("."):
                meta = self._read_meta(folder.name)
                if meta:
                    out.append(meta)
        out.sort(key=lambda m: str(m.get("updated_at") or ""), reverse=True)
        return out

    def get_document(self, doc_id: str) -> dict[str, Any]:
        with self._lock:
            meta = self._read_meta(doc_id)
            if meta is None:
                raise NotFound(f"document {doc_id} does not exist")
            doc = dict(meta)
            doc["body"] = self._read_body(doc_id)
            doc["linked"] = [
                {"id": nid, "label": (self._nodes.get(nid) or {}).get("label") or nid, "exists": nid in self._nodes}
                for nid in self._doc_links(meta)
            ]
            return doc

    def _new_doc_id(self, title: str) -> str:
        base = slugify(title, limit=48).replace("_", "-")
        doc_id, n = base, 2
        while (self.docs_dir / doc_id).exists():
            doc_id = f"{base}-{n}"
            n += 1
        return doc_id

    def _doc_links(self, meta: dict[str, Any]) -> list[str]:
        seen: list[str] = []
        for key in ("nodes", "wiki_nodes", "auto_nodes"):
            for nid in meta.get(key) or []:
                if nid not in seen:
                    seen.append(str(nid))
        return seen

    def _resolve_wiki_links(self, body: str, actor: str | None) -> list[str]:
        ids: list[str] = []
        for match in _WIKI_RE.finditer(body or ""):
            label = match.group(1).strip()
            cls = (match.group(2) or "").strip() or None
            if not label:
                continue
            node = self.find_node(label)
            if node is None:
                node = self.add_node(label, cls=cls, actor=actor)
            if str(node["id"]) not in ids:
                ids.append(str(node["id"]))
        return ids

    def _sync_doc_node(self, meta: dict[str, Any], actor: str | None) -> None:
        nid = meta["node_id"]
        node = self._nodes.get(nid)
        stamp = meta.get("updated_at") or now_iso()
        rec = copy.deepcopy(node) if node else {
            "id": nid,
            "file_type": "document",
            "_origin": KB_ORIGIN,
            "created_at": stamp,
        }
        rec.update({
            "label": meta.get("title") or meta["id"],
            "norm_label": _norm_label(meta.get("title") or meta["id"]),
            "doc_id": meta["id"],
            "source_file": f"{self.out_dir.name}/kb/documents/{meta['id']}/body.md",
            "updated_at": stamp,
            "actor": actor or "api",
        })
        if meta.get("tags"):
            rec["tags"] = list(meta["tags"])
        else:
            rec.pop("tags", None)
        if node != rec:
            self._p_put_node(rec)
        wanted = set(self._doc_links(meta))
        wanted.discard(nid)
        for link in list(self._out.get(nid, [])):
            if link.get("relation") == MENTIONS and str(link.get("target")) not in wanted:
                self._p_drop_edge(edge_id(nid, link.get("target"), MENTIONS))
        auto = set(meta.get("auto_nodes") or [])
        for target in wanted:
            if target in self._nodes and edge_id(nid, target, MENTIONS) not in self._edges:
                conf = "INFERRED" if target in auto and target not in (meta.get("nodes") or []) else "EXTRACTED"
                self.add_edge(nid, target, MENTIONS, confidence=conf, actor=actor)

    def create_document(
        self,
        title: str,
        *,
        body: str = "",
        tags: list[str] | None = None,
        nodes: list[str] | None = None,
        source: str | None = None,
        doc_id: str | None = None,
        actor: str | None = None,
        auto_nodes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a document box with a markdown body and a graph node."""
        title = _check_label(title)
        body = str(body or "")
        if len(body) > MAX_TEXT_CHARS:
            raise KBError(f"body is longer than {MAX_TEXT_CHARS} characters")
        with self._tx("add_document", f"Add document {title}", actor):
            new_id = slugify(doc_id).replace("_", "-") if doc_id else self._new_doc_id(title)
            if (self.docs_dir / new_id).exists():
                raise Conflict(f"document {new_id} already exists")
            self._doc_dir(new_id)
            for nid in nodes or []:
                if str(nid) not in self._nodes:
                    raise NotFound(f"node {nid} does not exist")
            stamp = now_iso()
            meta = {
                "id": new_id,
                "title": title,
                "tags": [str(t) for t in (tags or []) if str(t).strip()],
                "nodes": [str(n) for n in (nodes or [])],
                "wiki_nodes": [],
                "auto_nodes": [str(n) for n in (auto_nodes or [])],
                "assets": [],
                "source": source,
                "node_id": f"doc:{new_id}",
                "created_at": stamp,
                "updated_at": stamp,
                "actor": actor or "api",
            }
            meta["wiki_nodes"] = self._resolve_wiki_links(body, actor)
            self._p_put_doc(new_id, meta, body)
            self._sync_doc_node(meta, actor)
            return self.get_document(new_id)

    def update_document(
        self,
        doc_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        nodes: list[str] | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        with self._tx("update_document", f"Update document {doc_id}", actor):
            meta = self._read_meta(doc_id)
            if meta is None:
                raise NotFound(f"document {doc_id} does not exist")
            if title is not None:
                meta["title"] = _check_label(title)
            if tags is not None:
                meta["tags"] = [str(t) for t in tags if str(t).strip()]
            if nodes is not None:
                for nid in nodes:
                    if str(nid) not in self._nodes:
                        raise NotFound(f"node {nid} does not exist")
                meta["nodes"] = [str(n) for n in nodes]
            if body is not None:
                body = str(body)
                if len(body) > MAX_TEXT_CHARS:
                    raise KBError(f"body is longer than {MAX_TEXT_CHARS} characters")
                meta["wiki_nodes"] = self._resolve_wiki_links(body, actor)
            meta["updated_at"] = now_iso()
            meta["actor"] = actor or "api"
            self._p_put_doc(doc_id, meta, body)
            self._sync_doc_node(meta, actor)
            return self.get_document(doc_id)

    def delete_document(self, doc_id: str, *, actor: str | None = None) -> dict[str, Any]:
        with self._tx("delete_document", f"Delete document {doc_id}", actor):
            meta = self._read_meta(doc_id)
            if meta is None:
                raise NotFound(f"document {doc_id} does not exist")
            nid = meta.get("node_id")
            removed = 0
            if nid and nid in self._nodes:
                for link in list(self._out.get(nid, [])) + list(self._in.get(nid, [])):
                    eid = edge_id(link.get("source"), link.get("target"), link.get("relation"))
                    if eid in self._edges:
                        self._p_drop_edge(eid)
                        removed += 1
                self._p_drop_node(nid)
            self._p_drop_doc(doc_id)
            return {"deleted": doc_id, "node": nid, "edges_deleted": removed}

    def add_asset(
        self,
        doc_id: str,
        name: str,
        data: bytes,
        *,
        mime: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Attach a file (image, PDF, any binary) to a document."""
        if not isinstance(data, (bytes, bytearray)):
            raise KBError("asset data must be bytes")
        if len(data) > MAX_ASSET_BYTES:
            raise KBError(f"asset is larger than {MAX_ASSET_BYTES // 1_048_576} MB")
        fname = safe_asset_name(name)
        with self._tx("add_asset", f"Attach {fname} to {doc_id}", actor):
            meta = self._read_meta(doc_id)
            if meta is None:
                raise NotFound(f"document {doc_id} does not exist")
            self._p_put_asset(doc_id, fname, bytes(data))
            info = {
                "name": fname,
                "mime": mime or mimetypes.guess_type(fname)[0] or "application/octet-stream",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "added_at": now_iso(),
            }
            meta["assets"] = [a for a in meta.get("assets", []) if a.get("name") != fname] + [info]
            meta["updated_at"] = info["added_at"]
            self._p_put_doc(doc_id, meta, None)
            return info

    def asset_path(self, doc_id: str, name: str) -> Path:
        path = self._doc_dir(doc_id) / "assets" / safe_asset_name(name)
        if not path.is_file():
            raise NotFound(f"asset {name} does not exist")
        return path

    def delete_asset(self, doc_id: str, name: str, *, actor: str | None = None) -> dict[str, Any]:
        fname = safe_asset_name(name)
        with self._tx("delete_asset", f"Remove {fname} from {doc_id}", actor):
            meta = self._read_meta(doc_id)
            if meta is None:
                raise NotFound(f"document {doc_id} does not exist")
            self.asset_path(doc_id, fname)
            self._p_drop_asset(doc_id, fname)
            meta["assets"] = [a for a in meta.get("assets", []) if a.get("name") != fname]
            meta["updated_at"] = now_iso()
            self._p_put_doc(doc_id, meta, None)
            return {"deleted": fname}

    # -- ingest ----------------------------------------------------------
    def _label_index(self) -> dict[str, str]:
        def build() -> dict[str, str]:
            index: dict[str, str] = {}
            for node in self.data["nodes"]:
                if node.get("doc_id") or node.get("file_type") == "rationale":
                    continue
                key = _norm_label(node.get("label") or "")
                if 4 <= len(key) <= 80 and any(ch.isalpha() for ch in key):
                    if key not in index or node.get("_origin") == KB_ORIGIN:
                        index[key] = str(node.get("id"))
            return index

        return self._cached("labels", build)

    def link_mentions(self, text: str, *, limit: int = 40) -> list[str]:
        """Return ids of nodes whose labels appear in ``text`` (no LLM)."""
        with self._lock:
            index = self._label_index()
        words = _WORD_RE.findall(text[:MAX_TEXT_CHARS])
        found: dict[str, int] = {}
        lowered = [w.lower().rstrip(".-") for w in words]
        for i in range(len(lowered)):
            for n in (4, 3, 2, 1):
                if i + n > len(lowered):
                    continue
                key = " ".join(lowered[i:i + n])
                nid = index.get(key)
                if nid:
                    found[nid] = found.get(nid, 0) + 1
                    break
        ranked = sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))
        return [nid for nid, _count in ranked[: max(0, int(limit))]]

    def ingest(
        self,
        name: str,
        *,
        data: bytes | None = None,
        text: str | None = None,
        url: str | None = None,
        mode: str = "text",
        title: str | None = None,
        tags: list[str] | None = None,
        link: bool = True,
        backend: str | None = None,
        model: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Turn a file, text or URL into a document box and connect it to the graph.

        ``mode="text"`` links nodes whose labels the text names.
        ``mode="llm"`` also runs graphify's semantic extraction on the text and
        adds the extracted nodes and edges.
        """
        if mode not in ("text", "llm"):
            raise KBError("mode must be text or llm")
        if url:
            from graphify.security import safe_fetch

            try:
                data = safe_fetch(url, max_bytes=MAX_ASSET_BYTES)
            except Exception as exc:
                raise KBError(f"cannot fetch {url}: {exc}") from exc
            name = name or os.path.basename(url.split("?", 1)[0]) or "page.html"
        if data is None and text is None:
            raise KBError("give data, text or url")
        fname = safe_asset_name(name or "document.md")
        suffix = Path(fname).suffix.lower()
        notes: list[str] = []
        keep_asset = False
        if text is not None:
            body = str(text)
        elif suffix in TEXT_SUFFIXES or suffix == "":
            body = bytes(data or b"").decode("utf-8", errors="replace")
        elif suffix in HTML_SUFFIXES or (url and suffix not in IMAGE_SUFFIXES | {".pdf"}):
            body = _html_to_markdown(bytes(data or b"").decode("utf-8", errors="replace"))
        elif suffix == ".pdf":
            keep_asset = True
            body, note = _pdf_to_text(bytes(data or b""))
            if note:
                notes.append(note)
            body = f"[{fname}](assets/{fname})\n\n{body}"
        elif suffix in IMAGE_SUFFIXES:
            keep_asset = True
            body = f"![{Path(fname).stem}](assets/{fname})\n"
        else:
            keep_asset = True
            body = f"[{fname}](assets/{fname})\n"
        body = body[:MAX_TEXT_CHARS]
        doc_title = title or _first_heading(body) or Path(fname).stem.replace("_", " ").replace("-", " ")
        auto = self.link_mentions(body) if link else []
        with self._tx("ingest", f"Ingest {fname}", actor):
            doc = self.create_document(
                doc_title,
                body=body,
                tags=tags,
                source=url or fname,
                actor=actor,
                auto_nodes=auto,
            )
            if keep_asset and data is not None:
                self.add_asset(doc["id"], fname, bytes(data), actor=actor)
            added_nodes: list[str] = []
            added_edges = 0
            if mode == "llm":
                added_nodes, added_edges = self._llm_extract(doc["id"], backend=backend, model=model, actor=actor)
                if added_nodes:
                    meta = self._read_meta(doc["id"]) or {}
                    meta["auto_nodes"] = list(dict.fromkeys(list(meta.get("auto_nodes") or []) + added_nodes))
                    self._p_put_doc(doc["id"], meta, None)
                    self._sync_doc_node(meta, actor)
            return {
                "document": self.get_document(doc["id"]),
                "mode": mode,
                "linked": len(auto),
                "nodes_added": added_nodes,
                "edges_added": added_edges,
                "notes": notes,
            }

    def _llm_extract(self, doc_id: str, *, backend: str | None, model: str | None, actor: str | None) -> tuple[list[str], int]:
        from graphify.llm import extract_files_direct

        body_path = self._doc_dir(doc_id) / "body.md"
        try:
            result = extract_files_direct([body_path], backend=backend, model=model, root=self.out_dir.parent)
        except (ValueError, ImportError) as exc:
            raise KBError(f"LLM extraction is not available: {exc}") from exc
        source_file = f"{self.out_dir.name}/kb/documents/{doc_id}/body.md"
        added: list[str] = []
        for node in result.get("nodes") or []:
            if not isinstance(node, dict) or not node.get("id") or not node.get("label"):
                continue
            nid = str(node["id"])
            if nid in self._nodes:
                added.append(nid)
                continue
            ftype = node.get("file_type") if node.get("file_type") in VALID_FILE_TYPES else "concept"
            attrs = {k: v for k, v in node.items() if k not in ("id", "label", "file_type", "source_file")}
            self.add_node(str(node["label"]), node_id=nid, file_type=ftype, source_file=source_file, attrs=attrs, actor=actor)
            added.append(nid)
        edges = 0
        for edge in result.get("edges") or result.get("links") or []:
            if not isinstance(edge, dict):
                continue
            s, t, rel = str(edge.get("source")), str(edge.get("target")), edge.get("relation")
            if s not in self._nodes or t not in self._nodes or not rel:
                continue
            try:
                rel = _check_relation(rel)
            except KBError:
                continue
            conf = str(edge.get("confidence") or "INFERRED").upper()
            if conf not in VALID_CONFIDENCES:
                conf = "INFERRED"
            self.add_edge(s, t, rel, confidence=conf, attrs={"source_file": source_file}, actor=actor)
            edges += 1
        return added, edges


def with_kb_vocabulary(onto: Ontology) -> Ontology:
    """Add the classes and the relation that the knowledge base itself writes."""
    onto.classes.setdefault("document", ClassSpec("document", description="A document box."))
    onto.classes.setdefault("concept", ClassSpec("concept", description="A named idea."))
    rel = onto.relations.get(MENTIONS)
    if rel is None:
        onto.relations[MENTIONS] = RelationSpec(
            MENTIONS, domain=["document"], description="A document names this node."
        )
    elif rel.domain and "document" not in rel.domain:
        rel.domain.append("document")
    return onto


def _first_heading(body: str) -> str | None:
    for line in body.splitlines()[:40]:
        stripped = line.strip()
        if stripped.startswith("#"):
            text = stripped.lstrip("#").strip()
            if text:
                return text[:200]
    return None


def _html_to_markdown(html: str) -> str:
    try:
        from markdownify import markdownify

        return markdownify(html, heading_style="ATX")
    except ImportError:
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</h[1-6]>|</li>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _pdf_to_text(data: bytes) -> tuple[str, str | None]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "Install pypdf to extract PDF text: pip install pypdf"
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"## Page {i + 1}\n\n{text.strip()}")
        return "\n\n".join(pages), None if pages else "The PDF has no text layer."
    except Exception as exc:
        return "", f"Cannot read the PDF: {exc}"
