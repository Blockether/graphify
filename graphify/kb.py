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
import errno
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

from graphify.ontology import (
    MENTIONS,
    SEVERITIES,
    TAXONOMY,
    Ontology,
    OntologyError,
    class_from_dict,
    graph_norm_label,
    infer_ontology,
    load_ontology,
    method_owners,
    method_targets,
    ontology_path,
    relation_from_dict,
    save_ontology,
    taxonomy_class,
    validate_graph,
    with_kb_vocabulary,
)
from graphify.export import _CONFIDENCE_SCORE_DEFAULTS
from graphify import kb_ingest
from graphify.kb_ingest import safe_name, slugify
from graphify.kb_link import LinkIndex, label_key, link_text, resolve_wiki, wiki_links
from graphify.validate import VALID_CONFIDENCES, VALID_FILE_TYPES

KB_ORIGIN = "kb"
KB_SOURCE = "kb"
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
_RELATION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-. ]{0,79}$")
_MISSING = object()


class KBError(ValueError):
    """A request that the knowledge base refuses (HTTP 400)."""

    status = 400


class NotFound(KBError):
    status = 404


class Conflict(KBError):
    status = 409


class Unavailable(KBError):
    """An outside service that the request needs (an LLM backend) does not answer (HTTP 502)."""

    status = 502


class UnsupportedType(KBError):
    """A file type that ingest cannot read, for example a program or an archive (HTTP 415)."""

    status = 415


class UndoConflict(Conflict):
    """A later change depends on the change to undo (HTTP 409).

    ``later`` holds the seq numbers of those later changes.
    """

    def __init__(self, message: str, later: list[int]) -> None:
        super().__init__(message)
        self.later = later


class Busy(KBError):
    """Another process holds the store lock for too long (HTTP 503)."""

    status = 503


# Seconds that a write waits for another process to release the store lock.
LOCK_TIMEOUT = 30.0
_LOCKED_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, errno.EDEADLK}


@contextmanager
def _file_lock(path: Path, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` across processes.

    It uses ``fcntl.flock`` (POSIX) or ``msvcrt.locking`` (Windows), as
    ``graphify.watch`` does.  The OS frees the lock when the process dies, so
    a crash leaves no stale lock.  The lock file stays on disk: a removed and
    created-again file lets two processes lock two different inodes (#4273).
    Raise Busy when another process holds the lock for ``timeout`` seconds.
    """
    try:
        import fcntl
    except ImportError:
        fcntl = None  # type: ignore[assignment]
    try:
        import msvcrt
    except ImportError:
        msvcrt = None  # type: ignore[assignment]
    if fcntl is None and msvcrt is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif sys.platform == "win32" and msvcrt is not None:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if exc.errno not in _LOCKED_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    raise Busy(f"another process holds {path} for more than {timeout:g} s") from None
                time.sleep(0.005)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            elif sys.platform == "win32" and msvcrt is not None:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        fh.close()


def _file_stat(path: Path) -> tuple[int, int, int] | None:
    """Return ``(inode, size, mtime_ns)`` of ``path``, or None without the file."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def edge_id(source: Any, target: Any, relation: Any) -> str:
    """Return the stable id of the edge ``source -relation-> target``."""
    raw = f"{source}\x00{target}\x00{relation}".encode("utf-8")
    return "e_" + hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:16]


# json.dump into a file always runs the pure-Python encoder: about three
# times slower on a 64 MB graph, and the server flush holds the store lock
# for that time (#8264).  JSONEncoder.encode runs the C encoder.  Long lists
# go out in slices, so the whole graph is never one more string in memory.
_JSON_COMPACT = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
_JSON_SLICE = 2000


def _write_compact_json(fh: TextIO, data: Any) -> None:
    """Write ``data`` as compact JSON, the same text as ``json.dumps``."""
    if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
        fh.write(_JSON_COMPACT.encode(data))
        return
    fh.write("{")
    for i, (key, value) in enumerate(data.items()):
        fh.write(("," if i else "") + _JSON_COMPACT.encode(key) + ":")
        if isinstance(value, list) and len(value) > _JSON_SLICE:
            fh.write("[")
            for start in range(0, len(value), _JSON_SLICE):
                part = _JSON_COMPACT.encode(value[start : start + _JSON_SLICE])
                fh.write(("," if start else "") + part[1:-1])
            fh.write("]")
        else:
            fh.write(_JSON_COMPACT.encode(value))
    fh.write("}")


def _dump_atomic(path: Path, data: Any, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if compact:
                _write_compact_json(fh, data)
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
        "directed": False,
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
        "moved_nodes": {},
    }


# Clustering and export derive these node keys, so the overlay never owns them.
DERIVED_NODE_KEYS = frozenset({"community", "community_name", "norm_label", "_kb_community"})


def is_kb_record(item: Any) -> bool:
    """Return True for a node or an edge that the knowledge base made."""
    return isinstance(item, dict) and item.get("_origin") == KB_ORIGIN


def read_overlay(out_dir: Path | str) -> dict[str, Any] | None:
    """Return the overlay of the knowledge base in ``out_dir``, or None without one."""
    path = Path(out_dir) / "kb" / "overlay.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KBError(f"cannot read {path}: {exc}") from exc
    overlay = _empty_overlay()
    if isinstance(raw, dict):
        for key in overlay:
            if key != "version" and isinstance(raw.get(key), dict):
                overlay[key] = raw[key]
    return overlay


def _pair_key(source: str, target: str, directed: bool) -> tuple[str, str]:
    if directed or source <= target:
        return source, target
    return target, source


def apply_overlay(
    data: dict[str, Any],
    overlay: dict[str, Any],
    *,
    links_key: str | None = None,
    directed: bool | None = None,
) -> dict[str, Any]:
    """Apply a knowledge-base overlay to a node-link graph in place.

    Graphify calls this after extraction and dedup, before clustering, so the
    knowledge base is part of the one graph.  The rules keep graphify's model:

    - The overlay owns the KB records.  KB records that it does not list go.
    - Clustering owns ``DERIVED_NODE_KEYS``; the overlay never changes them.
    - Graphify keeps at most one edge between two nodes.  An overlay edge
      between nodes that another edge joins stays out (``conflicts``).
    - An overlay edge without both end nodes stays out (``dangling``).
    - A node move (``move_node``) sends the records of the old id to the new
      id when the graph has the new code (``applied_moves``, see
      ``_apply_moves``).
    - A patch follows its node to a new id through its anchor (see
      ``PATCH_ANCHOR``); the overlay keeps it under the new id
      (``rekeyed_patches``).  A patch without a node stays out (``stale_patches``).
    - An overlay edge follows its end nodes the same way (see ``EDGE_ANCHOR``
      and ``rekeyed_edges``).
    - ``moved_ids`` maps each old node id whose records went to a new id, by a
      move or an anchor, to that id.  Documents name their nodes by id, so the
      store sends them there too (``move_document_ids``).

    The overlay keeps the records that stay out, so they come back when the
    graph has their nodes again.  Return the counts of the changes, and the ids
    of the records that stay out in ``conflict_edges``, ``dangling_edges`` and
    ``stale_patch_nodes`` (sorted).  ``overlay_changed`` is True when the
    overlay changed too (new ids, anchors or moves), so its owner saves it.
    """
    key = links_key or ("links" if "links" in data or "edges" not in data else "edges")
    if directed is None:
        directed = bool(data.get("directed", False))
    nodes = data.setdefault("nodes", [])
    links = data.setdefault(key, [])
    ov_nodes = overlay.get("nodes") or {}
    ov_edges = overlay.get("edges") or {}
    patches = overlay.get("patches") or {}
    deleted_nodes = overlay.get("deleted_nodes") or {}
    deleted_edges = overlay.get("deleted_edges") or {}
    moves = overlay.get("moved_nodes") or {}
    stats = {
        "nodes_added": 0, "nodes_updated": 0, "nodes_removed": 0, "nodes_patched": 0,
        "edges_added": 0, "edges_updated": 0, "edges_removed": 0,
        "conflicts": 0, "dangling": 0, "stale_patches": 0,
    }
    conflict_edges: list[str] = []
    dangling_edges: list[str] = []
    stale_patch_nodes: list[str] = []
    applied_moves: list[dict[str, str]] = []
    moves_changed = False
    if moves:
        applied_moves, moves_changed = _apply_moves(
            moves, nodes, links, patches, deleted_nodes, ov_edges, deleted_edges
        )
    moved_ids = {move["from"]: move["to"] for move in applied_moves}

    by_id: dict[str, dict[str, Any]] = {}
    removed: set[str] = set()
    kept_nodes: list[dict[str, Any]] = []
    for node in nodes:
        nid = str(node.get("id"))
        if nid in deleted_nodes or (is_kb_record(node) and nid not in ov_nodes):
            removed.add(nid)
            continue
        kept_nodes.append(node)
        by_id[nid] = node
    for nid, rec in ov_nodes.items():
        clean = {k: copy.deepcopy(v) for k, v in rec.items() if k not in DERIVED_NODE_KEYS}
        clean["id"] = nid
        node = by_id.get(nid)
        if node is None:
            kept_nodes.append(clean)
            by_id[nid] = clean
            stats["nodes_added"] += 1
            continue
        current = {k: v for k, v in node.items() if k not in DERIVED_NODE_KEYS}
        if current != clean:
            derived = {k: node[k] for k in DERIVED_NODE_KEYS if k in node}
            node.clear()
            node.update(clean)
            node.update(derived)
            stats["nodes_updated"] += 1
    rekeyed: list[dict[str, str]] = []
    anchored = 0
    lookup: tuple[dict, dict, set[str]] | None = None
    for nid, patch in list(patches.items()):
        nid = str(nid)
        node = by_id.get(nid)
        if not isinstance(patch, dict):
            continue
        if node is None:
            if nid in deleted_nodes:
                continue
            if lookup is None:
                lookup = _anchor_lookup(kept_nodes)
            new_id = _resolve_anchor(patch.get(PATCH_ANCHOR), lookup, patches, deleted_nodes)
            if new_id is None:
                stale_patch_nodes.append(nid)
                continue
            # The node has a new id (a file rename or a new id scheme): the
            # patch follows it, and the overlay keeps it under the new id.
            del patches[nid]
            patches[new_id] = patch
            rekeyed.append({"from": nid, "to": new_id})
            moved_ids.setdefault(nid, new_id)
            node = by_id[new_id]
            _refresh_anchor(patch, node)
        elif PATCH_ANCHOR not in patch:
            anchor = _derive_anchor(node, patch)
            if anchor is not None:
                patch[PATCH_ANCHOR] = anchor
                anchored += 1
        changed = False
        for attr, value in patch.items():
            if attr in ("id", PATCH_ANCHOR) or attr in DERIVED_NODE_KEYS:
                continue
            if value is None:
                if attr in node:
                    node.pop(attr)
                    changed = True
            elif node.get(attr, _MISSING) != value:
                node[attr] = copy.deepcopy(value)
                changed = True
        stats["nodes_patched"] += int(changed)
    stats["nodes_removed"] = len(removed)
    nodes[:] = kept_nodes

    by_eid: dict[str, dict[str, Any]] = {}
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    kept_links: list[dict[str, Any]] = []
    for link in links:
        s, t = str(link.get("source")), str(link.get("target"))
        eid = edge_id(s, t, link.get("relation"))
        if (
            s in removed
            or t in removed
            or eid in deleted_edges
            or (is_kb_record(link) and eid not in ov_edges)
        ):
            stats["edges_removed"] += 1
            continue
        kept_links.append(link)
        by_eid.setdefault(eid, link)
        by_pair.setdefault(_pair_key(s, t, directed), link)
    rekeyed_edges: list[dict[str, str]] = []
    edges_anchored = 0
    edge_lookup: tuple[dict, dict, set[str]] | None = None
    for eid, rec in list(ov_edges.items()):
        eid = str(eid)
        s, t = str(rec.get("source")), str(rec.get("target"))
        refresh: tuple[str, ...] = ()
        if s not in by_id or t not in by_id:
            if edge_lookup is None:
                edge_lookup = _anchor_lookup(kept_nodes, patches)
            ends = _follow_edge(rec, by_id, edge_lookup, deleted_nodes)
            new_eid = edge_id(ends[0], ends[1], rec.get("relation")) if ends else ""
            if not ends or new_eid in ov_edges:
                dangling_edges.append(eid)
                continue
            # The end nodes have new ids (a file rename): the edge follows
            # them, and the overlay keeps it under the new id.
            refresh = tuple(end for end, nid in (("source", s), ("target", t)) if nid not in by_id)
            del ov_edges[eid]
            rec["source"], rec["target"] = ends
            ov_edges[new_eid] = rec
            rekeyed_edges.append({"from": eid, "to": new_eid})
            for old_end, new_end in zip((s, t), ends):
                if old_end != new_end:
                    moved_ids.setdefault(old_end, new_end)
            eid, (s, t) = new_eid, ends
        edges_anchored += int(_anchor_edge(rec, by_id, patches, refresh))
        clean = {k: v for k, v in rec.items() if k != EDGE_ANCHOR}
        current = by_eid.get(eid)
        if current is not None:
            if current != clean:
                current.clear()
                current.update(copy.deepcopy(clean))
                stats["edges_updated"] += 1
            continue
        if _pair_key(s, t, directed) in by_pair:
            conflict_edges.append(eid)
            continue
        new = copy.deepcopy(clean)
        kept_links.append(new)
        by_eid[eid] = new
        by_pair[_pair_key(s, t, directed)] = new
        stats["edges_added"] += 1
    links[:] = kept_links
    stats["conflicts"] = len(conflict_edges)
    stats["dangling"] = len(dangling_edges)
    stats["stale_patches"] = len(stale_patch_nodes)
    stats["patches_anchored"] = anchored
    stats["edges_anchored"] = edges_anchored
    return {
        **stats,
        "conflict_edges": sorted(conflict_edges),
        "dangling_edges": sorted(dangling_edges),
        "stale_patch_nodes": sorted(stale_patch_nodes),
        "rekeyed_patches": sorted(rekeyed, key=lambda r: r["from"]),
        "rekeyed_edges": sorted(rekeyed_edges, key=lambda r: r["from"]),
        "applied_moves": applied_moves,
        "moved_ids": dict(sorted(moved_ids.items())),
        "overlay_changed": bool(moves_changed or rekeyed or anchored or rekeyed_edges or edges_anchored),
    }


# A patch keeps the identity of the extracted node that it changes under this
# key: its source file, label and class.  Graphify ids contain the file path,
# so a file rename or a new id scheme gives the node a new id.  The anchor
# finds the node again (memo R6).
PATCH_ANCHOR = "_anchor"
# An overlay edge keeps the anchors of its code end nodes under this key
# (``{"source": anchor, "target": anchor}``), so it follows a file rename like
# a patch.  KB nodes keep their ids and need no anchor.  The graph never shows
# this key.
EDGE_ANCHOR = "_anchors"
# Node keys that decide the anchor (``taxonomy_class`` reads the rest).
_ANCHOR_KEYS = frozenset({
    "label", "source_file", "class", "file_type", "node_kind", "metadata", "type",
    "_callable", "_callable_class", "source_location", "external",
})


def _anchor_class(node: dict[str, Any]) -> str:
    value = node.get("class")
    if value not in (None, ""):
        return str(value)
    return taxonomy_class(node) or str(node.get("file_type") or "")


def patch_anchor(node: dict[str, Any]) -> dict[str, str]:
    """Return the anchor of an extracted node: (source_file, label, class)."""
    return {
        "source_file": str(node.get("source_file") or ""),
        "label": str(node.get("label") or ""),
        "class": _anchor_class(node),
    }


def _derive_anchor(node: dict[str, Any], patch: dict[str, Any]) -> dict[str, str] | None:
    """Return the anchor of an old patch from its node, or None when unsure.

    ``graph.json`` can hold the node with the patch applied (a KB flush) or
    without it (a graphify build).  When the node already has a value that
    the patch sets for an anchor key, the extracted value is unknown.
    """
    for key, value in patch.items():
        if key in _ANCHOR_KEYS and node.get(key) == value:
            return None
    return patch_anchor(node)


def _anchor_lookup(
    nodes: list[dict[str, Any]], patches: dict[str, Any] | None = None
) -> tuple[dict, dict, set[str]]:
    """Index the code nodes by anchor.  With ``patches``, a patched node gives
    the anchor of its patch: the extracted values, not the patched ones."""
    by_file: dict[tuple[str, str, str], list[str]] = {}
    by_label: dict[tuple[str, str], list[str]] = {}
    files: set[str] = set()
    for node in nodes:
        if is_kb_record(node):
            continue
        nid = str(node.get("id"))
        anchor = (_node_anchor(node, patches.get(nid)) if patches is not None else None) or patch_anchor(node)
        by_file.setdefault((anchor["source_file"], anchor["label"], anchor["class"]), []).append(nid)
        by_label.setdefault((anchor["label"], anchor["class"]), []).append(nid)
        files.add(anchor["source_file"])
    return by_file, by_label, files


def _resolve_anchor(
    anchor: Any,
    lookup: tuple[dict, dict, set[str]],
    patches: dict[str, Any],
    deleted_nodes: dict[str, Any],
) -> str | None:
    """Return the one node that an anchor names now, or None.

    First a unique hit on (source_file, label, class).  Then a unique hit on
    (label, class) in the whole graph, but only when no node has the anchor's
    source_file any more: the file was moved or renamed.  When the file still
    has nodes, the symbol was deleted, and another file's symbol with the
    same label is not the same record.  A node that has its own patch or is
    deleted is never a target.
    """
    if not isinstance(anchor, dict) or not anchor.get("label"):
        return None
    by_file, by_label, files = lookup
    label, cls = str(anchor.get("label")), str(anchor.get("class") or "")
    source_file = str(anchor.get("source_file") or "")
    tiers = [by_file.get((source_file, label, cls))]
    if source_file not in files:
        tiers.append(by_label.get((label, cls)))
    for hits in tiers:
        if hits and len(hits) == 1:
            target = hits[0]
            return None if target in patches or target in deleted_nodes else target
        if hits:
            return None  # ambiguous: never guess
    return None


def _refresh_anchor(patch: dict[str, Any], node: dict[str, Any]) -> None:
    """Give a patch that followed its node to a new id the anchor of that node.

    The old anchor stays when the node already shows a patched anchor value.
    """
    fresh = _derive_anchor(node, patch)
    if fresh is not None:
        patch[PATCH_ANCHOR] = fresh


def _node_anchor(node: dict[str, Any], patch: Any) -> dict[str, str] | None:
    """Return the anchor of the code node at one end of an edge, or None.

    A KB node keeps its id, so it needs no anchor.  A patched node shows its
    patched values; the anchor of its patch holds the extracted ones.
    """
    if is_kb_record(node):
        return None
    if isinstance(patch, dict):
        anchor = patch.get(PATCH_ANCHOR)
        return dict(anchor) if isinstance(anchor, dict) else None
    return patch_anchor(node)


def _anchor_edge(
    rec: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    patches: dict[str, Any],
    refresh: tuple[str, ...] = (),
) -> bool:
    """Give an overlay edge the anchors of its code end nodes (``EDGE_ANCHOR``).

    An end keeps its anchor unless ``refresh`` names it: the edge just followed
    that end to a new id.  Return True when an anchor changed.
    """
    old = rec.get(EDGE_ANCHOR)
    anchors = dict(old) if isinstance(old, dict) else {}
    changed = False
    for end in ("source", "target"):
        if end in anchors and end not in refresh:
            continue
        nid = str(rec.get(end))
        node = by_id.get(nid)
        anchor = None if node is None else _node_anchor(node, patches.get(nid))
        if anchor is not None and anchors.get(end) != anchor:
            anchors[end] = anchor
            changed = True
    if changed:
        rec[EDGE_ANCHOR] = anchors
    return changed


def _follow_edge(
    rec: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    lookup: tuple[dict, dict, set[str]],
    deleted_nodes: dict[str, Any],
) -> tuple[str, str] | None:
    """Return the end ids that an overlay edge names now, or None.

    An end that the graph has keeps its id.  A missing end needs an anchor
    that names one node (``_resolve_anchor``).  That node can have its own
    patch: an edge does not compete with a patch.
    """
    found = rec.get(EDGE_ANCHOR)
    anchors = found if isinstance(found, dict) else {}
    ends: list[str] = []
    for end in ("source", "target"):
        nid = str(rec.get(end))
        if nid not in by_id:
            nid = _resolve_anchor(anchors.get(end), lookup, {}, deleted_nodes) or ""
            if not nid:
                return None
        ends.append(nid)
    return ends[0], ends[1]


def _move_target(old: str, moves: dict[str, Any], present: Any) -> str | None:
    """Return the first id on the move chain of ``old`` that ``present`` has.

    Two renames before a rebuild give the chain ``old -> b -> c``.  A chain
    that comes back to an id on it (a rename and its revert) has no target.
    """
    seen = {old}
    nid = old
    while True:
        nxt = moves.get(nid)
        if not isinstance(nxt, str) or not nxt or nxt in seen:
            return None
        if nxt in present:
            return nxt
        seen.add(nxt)
        nid = nxt


def _move_returns(old: str, moves: dict[str, Any]) -> bool:
    """Return True when the move chain of ``old`` comes back to ``old``."""
    seen: set[str] = set()
    nid = old
    while nid not in seen:
        seen.add(nid)
        nxt = moves.get(nid)
        if not isinstance(nxt, str):
            return False
        if nxt == old:
            return True
        nid = nxt
    return False


def _has_records(nid: str, patches: dict[str, Any], deleted_nodes: dict[str, Any], ov_edges: dict[str, Any]) -> bool:
    """Return True when the overlay has a patch, a deletion or an edge of node ``nid``."""
    if nid in patches or nid in deleted_nodes:
        return True
    return any(
        isinstance(rec, dict) and nid in (str(rec.get("source")), str(rec.get("target")))
        for rec in ov_edges.values()
    )


def _apply_moves(
    moves: dict[str, Any],
    nodes: list[dict[str, Any]],
    links: list[dict[str, Any]],
    patches: dict[str, Any],
    deleted_nodes: dict[str, Any],
    ov_edges: dict[str, Any],
    deleted_edges: dict[str, Any],
) -> tuple[list[dict[str, str]], bool]:
    """Send the overlay records of moved nodes to their new ids.

    ``moves`` maps an old node id to its new id (``move_node``).  A move waits
    while the graph has the old id: the graph does not have the new code yet.
    Then the patch, the deletion, the overlay edges and the deleted edges of
    the old id go to the first id on the move chain that the graph has, and
    the move goes.  A record stays under the old id when the new id has its
    own record of the same kind.  A move without a target stays only while
    the overlay has records of the old id or another move leads to it.  A
    chain that comes back to an id that the graph has goes: the code was
    renamed and renamed back.  Return the applied moves (sorted) and True
    when the overlay changed.
    """
    present = {str(node.get("id")): node for node in nodes}
    targets = {str(value) for value in moves.values()}
    resolved: dict[str, str] = {}
    changed = False
    reverted: list[str] = []
    for old in sorted(moves):
        if old in present:
            if _move_returns(old, moves):
                reverted.append(old)  # a rename and its revert: the code has the old id again
            continue
        new = _move_target(old, moves, present)
        if new is not None:
            resolved[old] = new
        elif old not in targets and not _has_records(old, patches, deleted_nodes, ov_edges):
            del moves[old]  # nothing can follow it any more
            changed = True
    for old in reverted:
        del moves[old]
        changed = True
    for old, new in resolved.items():
        del moves[old]
        patch = patches.get(old)
        if isinstance(patch, dict) and new not in patches and new not in deleted_nodes:
            del patches[old]
            patches[new] = patch
            _refresh_anchor(patch, present[new])
        if old in deleted_nodes:
            deleted_nodes.setdefault(new, deleted_nodes.pop(old))
    for eid, rec in list(ov_edges.items()):
        if not isinstance(rec, dict):
            continue
        ends = (str(rec.get("source")), str(rec.get("target")))
        moved = (resolved.get(ends[0], ends[0]), resolved.get(ends[1], ends[1]))
        if moved == ends:
            continue
        new_eid = edge_id(moved[0], moved[1], rec.get("relation"))
        if new_eid in ov_edges:
            continue  # the new ids have their own edge: this one stays out
        del ov_edges[eid]
        rec["source"], rec["target"] = moved
        anchors = rec.get(EDGE_ANCHOR)
        for side, old, new in zip(("source", "target"), ends, moved):
            fresh = _node_anchor(present[new], patches.get(new)) if old != new else None
            if fresh is not None and isinstance(anchors, dict):
                anchors[side] = fresh
        ov_edges[new_eid] = rec
    if resolved and deleted_edges:
        olds: dict[str, list[str]] = {}
        for old, new in resolved.items():
            olds.setdefault(new, []).append(old)
        for link in links:
            s, t = str(link.get("source")), str(link.get("target"))
            if s not in olds and t not in olds:
                continue
            eid = edge_id(s, t, link.get("relation"))
            for old_s in (s, *olds.get(s, ())):
                for old_t in (t, *olds.get(t, ())):
                    old_eid = edge_id(old_s, old_t, link.get("relation"))
                    if old_eid != eid and old_eid in deleted_edges:
                        deleted_edges.setdefault(eid, deleted_edges.pop(old_eid))
    return [{"from": old, "to": new} for old, new in sorted(resolved.items())], changed or bool(resolved)


# The keys of a document meta that list the linked node ids.
DOC_LINK_KEYS = ("nodes", "wiki_nodes", "auto_nodes", "llm_nodes")


def move_document_ids(
    meta: dict[str, Any], moved: dict[str, str], nodes: dict[str, dict[str, Any]]
) -> bool:
    """Send the node ids of document ``meta`` to their new ids in ``moved``.

    ``moved`` is the ``moved_ids`` result of ``apply_overlay``.  The links, the
    text-link scores, the link decisions and the review candidates follow.  A
    candidate takes the label and the file of its new node from ``nodes``.
    Two old ids of one new id give one link.  Return True when ``meta`` changed.
    """
    changed = False
    for key in DOC_LINK_KEYS:
        ids = meta.get(key)
        if isinstance(ids, list) and any(str(nid) in moved for nid in ids):
            meta[key] = list(dict.fromkeys(moved.get(str(nid), str(nid)) for nid in ids))
            changed = True
    scores = meta.get("auto_scores")
    if isinstance(scores, dict) and any(nid in moved for nid in scores):
        kept: dict[str, Any] = {}
        for nid, score in scores.items():
            new = moved.get(nid, nid)
            if new == nid or new not in kept:
                kept[new] = score  # the new id keeps its own score
        meta["auto_scores"] = kept
        changed = True
    decisions = meta.get("decisions")
    if isinstance(decisions, dict) and any(nid in moved for nid in decisions.values() if isinstance(nid, str)):
        meta["decisions"] = {
            key: moved.get(nid, nid) if isinstance(nid, str) else nid for key, nid in decisions.items()
        }
        changed = True
    for item in meta.get("possible") or ():
        cands = item.get("candidates") if isinstance(item, dict) else None
        for cand in cands if isinstance(cands, list) else ():
            new = moved.get(str(cand.get("id"))) if isinstance(cand, dict) else None
            if new is None:
                continue
            cand["id"] = new
            node = nodes.get(new) or {}
            for attr in ("label", "source_file"):
                if attr in cand and node.get(attr) is not None:
                    cand[attr] = node[attr]
            changed = True
    return changed


# Keys of the ``apply_overlay`` result that do not count graph changes: the
# records that stay out and the overlay bookkeeping.
OVERLAY_SKIP_KEYS = frozenset(
    {
        "conflicts", "dangling", "stale_patches", "conflict_edges", "dangling_edges",
        "stale_patch_nodes", "rekeyed_patches", "patches_anchored", "rekeyed_edges",
        "edges_anchored", "applied_moves", "moved_ids", "overlay_changed",
    }
)


# Label keys (review 2, item 8):
# - ``graph_norm_label`` (ontology) is the ``norm_label`` that graphify's
#   writer stores; KB records store the same value.
# - ``kb_link.label_key`` is the match key of the linker and ``find_node``.
# - ``build._norm_label`` stays as it is: it is the dedupe key of graphify's
#   core build, and a change there changes which extracted nodes merge.


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


def _check_edge_provenance(rec: dict[str, Any]) -> None:
    """Check the ``confidence_score`` and ``source_file`` of an edge record."""
    score = rec.get("confidence_score")
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0.0 <= float(score) <= 1.0:
            raise KBError(f"confidence_score must be a number from 0 to 1, not {score!r}")
        rec["confidence_score"] = float(score)
    src = rec.get("source_file")
    if src is not None and (not isinstance(src, str) or not src.strip() or len(src) > 1000):
        raise KBError("source_file must be a non-empty path of at most 1000 characters")


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


def _describe_key(key: str) -> str:
    """Return a footprint key (see ``KnowledgeBase._footprint``) in words."""
    kind, _, name = key.partition(":")
    words = {"n": "node", "e": "edge", "d": "document", "a": "asset"}
    return "the ontology" if key == "o" else f"{words.get(kind, kind)} {name}"


def _community_order(cid: Any) -> tuple[int, Any]:
    """Sort key for community ids: numbers in numeric order, then text."""
    if isinstance(cid, (int, float)) and not isinstance(cid, bool):
        return 0, cid
    return 1, str(cid)


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
        self.lock_path = self.kb_dir / ".lock"
        self.gen_path = self.kb_dir / "generation.json"
        self.autoflush = autoflush
        self._create = create
        self._lock = threading.RLock()
        # Seqs that this thread committed, for ``take_change_seqs`` (#8177 X2).
        self._thread = threading.local()
        self._store_depth = 0
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._undo_log: list[tuple] | None = None
        self._tx_keys: tuple[set[str], set[str]] = (set(), set())
        self._tx_docs: set[str] = set()
        self._dirty = False
        self._seen: dict[str, Any] = {}
        self._cache: dict[str, tuple[int, Any]] = {}
        self.version = 0
        self.history: list[dict[str, Any]] = []
        self._undone: set[int] = set()
        self._max_seq = 0
        self._hist_pos = 0
        self._hist_ino: int | None = None
        self._key_index: dict[str, list[int]] = {}
        self._keys_of: dict[int, tuple[set[str], set[str]]] = {}
        self._indexed = 0
        with self._lock, self._store_lock(create=self._create):
            self._read_history(full=True)
            self._load()
        self._maybe_flush()

    # -- store files and the cross-process lock -------------------------
    # Many processes can open one store: a ``kb serve`` server, ``kb ingest``
    # and other CLI calls.  Each write holds ``kb/.lock`` and first loads what
    # the other processes wrote, so a seq never repeats and no change is lost.
    def _signature(self) -> dict[str, Any]:
        return {
            "graph": _file_stat(self.graph_path),
            "overlay": _file_stat(self.overlay_path),
            "history": _file_stat(self.history_path),
            "ontology": _file_stat(self.onto_path),
            "generation": _file_stat(self.gen_path),
        }

    @contextmanager
    def _store_lock(self, *, create: bool = True) -> Iterator[None]:
        """Hold the cross-process store lock.  The caller holds ``self._lock``.

        The lock nests.  With ``create=False`` and no ``kb/`` folder, it takes
        no lock and makes no files: nothing else writes that store yet.
        """
        if self._store_depth or (not create and not self.kb_dir.is_dir()):
            self._store_depth += 1
            try:
                yield
            finally:
                self._store_depth -= 1
            return
        with _file_lock(self.lock_path):
            self._store_depth += 1
            try:
                yield
            finally:
                self._store_depth -= 1

    def _sync(self) -> bool:
        """Load the changes of other processes.  Return True when the state changed."""
        cur = self._signature()
        seen = self._seen
        if cur == seen:
            return False
        before = self.version
        if cur["history"] != seen.get("history"):
            self._read_history()
        _gen, files = self._read_generation()
        # A flush of another KB process rewrites graph.json from the same
        # overlay: generation.json then names that file, and no reload is due.
        rebuilt = cur["graph"] != seen.get("graph") and cur["graph"] != files.get("graph")
        if rebuilt or cur["overlay"] != seen.get("overlay") or cur["ontology"] != seen.get("ontology"):
            self._load()
        else:
            self._seen = cur
            self._set_generation(bump=False)
        if self.version != before:
            self._cache.clear()
            return True
        return False

    def _content_files(self) -> dict[str, Any]:
        return {
            "graph": _file_stat(self.graph_path),
            "overlay": _file_stat(self.overlay_path),
            "ontology": _file_stat(self.onto_path),
        }

    def _read_generation(self) -> tuple[int, dict[str, Any]]:
        """Return the stored generation and the file stats that it describes."""
        try:
            raw = json.loads(self.gen_path.read_text(encoding="utf-8"))
            files = raw.get("files") or {}
            stats = {k: tuple(v) if v else None for k, v in files.items()}
            return int(raw.get("generation") or 0), stats
        except (OSError, ValueError, TypeError, AttributeError):
            return 0, {}

    def _set_generation(self, *, bump: bool) -> None:
        """Set ``self.version`` and store it in ``kb/generation.json``.

        ``version`` grows with each change and survives restarts, so two states
        never share a version (HTTP ETags use it).  The file keeps the stats of
        ``graph.json``, the overlay and the ontology that the version describes.
        When ``_load`` finds other stats, another program changed a file (for
        example a graphify rebuild), and the version grows again.
        """
        gen, files = self._read_generation()
        value = max(self.version, gen, self._last_seq(), 0 if bump else 1) + (1 if bump else 0)
        self.version = value
        current = self._content_files()
        if (value, current) == (gen, files):
            return
        if self.kb_dir.is_dir() or self._create:
            record = {"generation": value, "files": {k: list(v) if v else None for k, v in current.items()}}
            _dump_atomic(self.gen_path, record)
            self._seen["generation"] = _file_stat(self.gen_path)

    def _last_seq(self) -> int:
        return self._max_seq

    # -- loading -------------------------------------------------------
    def _load(self) -> None:
        """Read ``graph.json``, the overlay and the ontology again.

        The caller holds ``self._lock`` and, when the store exists, the store
        lock.  This only marks the graph dirty; callers flush it.
        """
        with self._lock:
            sig = self._signature()
            if self.graph_path.is_file():
                try:
                    data = json.loads(self.graph_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise KBError(f"cannot read {self.graph_path}: {exc}") from exc
            elif self._create:
                data = _empty_graph()
            else:
                raise NotFound(f"{self.graph_path} does not exist")
            if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
                raise KBError(f"{self.graph_path} is not a node-link graph")
            self.links_key = "links" if "links" in data or "edges" not in data else "edges"
            data.setdefault(self.links_key, [])
            self.data = data
            self.overlay = self._read_overlay()
            stats = apply_overlay(self.data, self.overlay, links_key=self.links_key)
            self.overlay_stats = stats
            self._reindex()
            # The documents follow first: when a crash stops the overlay write,
            # the next load moves the records and the documents again.
            self._move_doc_links(stats["moved_ids"])
            if stats["overlay_changed"] and self.overlay_path.is_file():
                # Keep the new ids, anchors and applied moves, so the next load
                # finds them at once.  The caller holds the store lock.
                _dump_atomic(self.overlay_path, self.overlay)
                sig["overlay"] = _file_stat(self.overlay_path)
            changed = any(v for k, v in stats.items() if k not in OVERLAY_SKIP_KEYS)
            self.ontology_inferred = False
            onto = load_ontology(self.onto_path)
            if onto is None:
                onto = self._default_ontology()
                self.ontology_inferred = True
            self.ontology = onto
            self._seen = sig
            _gen, files = self._read_generation()
            self._set_generation(bump=files != self._content_files())
            self._cache.clear()
            if changed or not self.graph_path.is_file():
                self._dirty = True

    def _maybe_flush(self) -> None:
        if self._dirty and self.autoflush:
            self.flush()

    def _default_ontology(self) -> Ontology:
        """Infer the ontology from extracted knowledge only, plus the KB vocabulary.

        Manual nodes and edges stay out, so a new class or relation shows up as
        a finding until somebody adds it to the ontology.
        """
        return self._infer()

    def _infer(self, class_key: str = "file_type") -> Ontology:
        """Infer an ontology from the extracted records only (S6).

        The default ontology and the Infer action use this one base, so the
        two never disagree about the KB records.
        """
        base = {
            "nodes": [n for n in self.data["nodes"] if n.get("_origin") != KB_ORIGIN],
            "links": [x for x in self.data[self.links_key] if x.get("_origin") != KB_ORIGIN],
        }
        return with_kb_vocabulary(infer_ontology(base, class_key=class_key))

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
        return read_overlay(self.out_dir) or _empty_overlay()

    def refresh_if_changed(self) -> bool:
        """Load the changes of other processes: a graphify rebuild or another writer."""
        with self._lock:
            if self._signature() == self._seen:
                return False
            with self._store_lock(create=False):
                changed = self._sync()
            if changed:
                self._notify({"type": "reload", "version": self.version})
                self._maybe_flush()
            return changed

    def _pair_links(self, s: str, t: str) -> list[dict[str, Any]]:
        """Return the edges that join ``s`` and ``t``.  Graphify keeps at most one."""
        found = [x for x in self._out.get(s, []) if str(x.get("target")) == t]
        if not self.data.get("directed") and s != t:
            found += [x for x in self._out.get(t, []) if str(x.get("target")) == s]
        return found

    def _assign_communities(self) -> bool:
        """Put each KB node without a community into the commonest one of its neighbours.

        This is label propagation (Raghavan, Albert and Kumara, 2007,
        doi:10.1103/PhysRevE.76.036106) for the new KB nodes only: the other
        nodes keep the communities of the last graphify build.  The update is
        synchronous: each round votes with the labels of the round before, so
        the result does not depend on node order.  A tie goes to the smallest
        community id.  Rounds repeat until no node changes, so a chain of new
        KB nodes gets the label of its nearest built node.  ``_kb_community``
        marks a label from this step, so each flush votes again with the edges
        of that time.  The next graphify build clusters the whole graph again.
        Return True when a community changed.
        """
        todo = [n for n in self.data["nodes"] if is_kb_record(n) and (n.get("community") is None or n.get("_kb_community"))]
        nodes = list(todo)
        before = [(n.get("community"), n.get("community_name")) for n in nodes]
        for node in todo:
            if node.pop("_kb_community", None):
                node.pop("community", None)
                node.pop("community_name", None)
        while todo:
            picks: list[tuple[dict[str, Any], Any, Any]] = []
            for node in todo:
                nid = str(node.get("id"))
                votes: Counter[Any] = Counter()
                names: dict[Any, Any] = {}
                for link in self._out.get(nid, []) + self._in.get(nid, []):
                    s = str(link.get("source"))
                    other = self._nodes.get(str(link.get("target")) if s == nid else s)
                    if other is None:
                        continue
                    cid = other.get("community")
                    if cid is None or isinstance(cid, bool):
                        continue
                    votes[cid] += 1
                    if other.get("community_name"):
                        names.setdefault(cid, other["community_name"])
                if votes:
                    best = min(votes, key=lambda c: (-votes[c], _community_order(c)))
                    picks.append((node, best, names.get(best)))
            if not picks:
                break
            for node, best, name in picks:
                node["community"] = best
                node["_kb_community"] = True
                if name:
                    node["community_name"] = name
            todo = [n for n in todo if n.get("community") is None]
        return before != [(n.get("community"), n.get("community_name")) for n in nodes]

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
            # The footprint comes from the store as it is now, when the change
            # runs, not later at undo time (review 2, item 7).
            self._step_keys(inverse, self._tx_keys[0], self._tx_keys[1])

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
            fresh = nid not in ov["patches"]
            patch = ov["patches"].setdefault(nid, {})
            for key in set(base) | set(rec):
                if key == PATCH_ANCHOR:
                    continue
                new = rec.get(key, _MISSING)
                if base.get(key, _MISSING) != new:
                    patch[key] = None if new is _MISSING else copy.deepcopy(new)
            if not any(key != PATCH_ANCHOR for key in patch):
                ov["patches"].pop(nid, None)
            elif fresh and before is not None:
                # The node has no patch yet, so it holds its extracted values.
                patch[PATCH_ANCHOR] = patch_anchor(before)
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
        if EDGE_ANCHOR in rec:
            rec = {k: v for k, v in rec.items() if k != EDGE_ANCHOR}
        eid = edge_id(rec.get("source"), rec.get("target"), rec.get("relation"))
        existing = self._edges.get(eid)
        before = copy.deepcopy(existing[0]) if existing else None
        self._raw_put_edge(rec)
        self.overlay["deleted_edges"].pop(eid, None)
        stored = copy.deepcopy(rec)
        _anchor_edge(stored, self._nodes, self.overlay["patches"])
        self.overlay["edges"][eid] = stored
        self._log(("put_edge", before) if before is not None else ("drop_edge", eid))

    def _p_drop_edge(self, eid: str) -> None:
        records = list(self._edges.get(eid, []))
        if not records:
            # The graph lost an end node, so only the overlay holds the edge.
            if eid in self.overlay["edges"]:
                self._p_drop_overlay("edges", eid)
            return
        for link in records:
            self._raw_drop_edge_record(link)
        self.overlay["edges"].pop(eid, None)
        if any(link.get("_origin") != KB_ORIGIN for link in records):
            self.overlay["deleted_edges"][eid] = now_iso()
        for link in reversed(records):
            self._log(("put_edge", copy.deepcopy(link)))

    def _p_drop_overlay(self, key: str, rid: str) -> None:
        """Remove an overlay record that the graph does not hold (R6)."""
        rec = self.overlay[key].pop(rid, None)
        if rec is not None:
            self._log(("restore_overlay", key, rid, copy.deepcopy(rec)))

    def _p_put_overlay(self, key: str, rid: str, rec: Any) -> None:
        """Set an overlay record that the graph does not hold, such as a node move."""
        before = self.overlay[key].get(rid)
        self.overlay[key][rid] = copy.deepcopy(rec)
        if before is None:
            self._log(("drop_overlay", key, rid))
        else:
            self._log(("restore_overlay", key, rid, copy.deepcopy(before)))

    def _settle_moves(self) -> None:
        """Apply the node moves whose old node the graph lost (``move_node``).

        The graph in memory holds the overlay already, so the overlay applies
        again in place and only the moved records and document links change.
        This runs after the change, outside its undo log: the code moved, so an
        undo keeps the records and the document links at their new ids.
        """
        moves = self.overlay["moved_nodes"]
        if not any(old not in self._nodes and _move_target(old, moves, self._nodes) for old in moves):
            return
        self.overlay_stats = apply_overlay(self.data, self.overlay, links_key=self.links_key)
        self._reindex()
        self._move_doc_links(self.overlay_stats["moved_ids"])

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
        elif kind == "restore_overlay":
            self.overlay[step[1]][step[2]] = copy.deepcopy(step[3])
            self._log(("drop_overlay", step[1], step[2]))
        elif kind == "drop_overlay":
            self._p_drop_overlay(step[1], step[2])
        else:
            raise KBError(f"unknown undo step {kind!r}")

    def _trash_slot(self, stem: str) -> Path:
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        for n in range(1000):
            slot = self.trash_dir / f"{safe_name(stem)}-{stamp}-{n}"
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
            with self._store_lock():
                self._sync()
                log: list[tuple] = []
                overlay_before = copy.deepcopy(self.overlay)
                self._tx_keys = (set(), set())
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
        """Apply due node moves, write the overlay and one history entry.

        The caller holds the store lock.
        """
        self._settle_moves()
        _dump_atomic(self.overlay_path, self.overlay)
        seq = self._last_seq() + 1
        entry = {"seq": seq, "ts": now_iso(), "actor": actor or "api", "op": op, "summary": summary, "undo": log}
        entry.update(extra or {})
        writes, reads = self._tx_keys
        entry["keys"] = {"w": sorted(writes), "r": sorted(reads - writes)}
        self._append_history(entry)
        self._add_history_entry(entry)
        self._thread.seqs = [*getattr(self._thread, "seqs", []), seq]
        for name, path in (("overlay", self.overlay_path), ("ontology", self.onto_path)):
            self._seen[name] = _file_stat(path)
        self._set_generation(bump=True)
        self._cache.clear()
        self._dirty = True
        if self.autoflush:
            self.flush()
        self._notify({"type": "change", "version": self.version, "seq": seq, "op": op, "summary": summary, "actor": entry["actor"]})
        return entry

    def take_change_seqs(self) -> list[int]:
        """Return and clear the seqs that this thread committed, in commit order.

        A server clears them before each request and reports them after it,
        so a client can undo its own change and not a later one (#8177 X2).
        """
        seqs = list(getattr(self._thread, "seqs", []))
        self._thread.seqs = []
        return seqs

    def flush(self) -> bool:
        """Write ``graph.json`` when it has unsaved changes."""
        with self._lock:
            if not self._dirty:
                return False
            with self._store_lock():
                if self._undo_log is None:
                    self._sync()
                if not self._dirty:
                    return False
                # Report 2 (#8161): new communities change the view, so the
                # version grows and the cached view and ETags are rebuilt.
                moved = self._assign_communities()
                _dump_atomic(self.graph_path, self.data, compact=True)
                self._seen["graph"] = _file_stat(self.graph_path)
                self._set_generation(bump=moved)
                if moved:
                    self._cache.clear()
                self._dirty = False
                return True

    # -- undo footprints -------------------------------------------------
    # Selective undo (Berlage 1994, doi:10.1145/196699.196721; Prakash and
    # Knister 1994, doi:10.1145/198425.198427): any change can be undone, but
    # only when no later live change depends on it.  Each history entry keeps
    # the records that it wrote ("w") and the records that it needs ("r"): an
    # edge needs its two end nodes.  Undo of change N conflicts with a later
    # change M when w(N) meets w(M) or r(M).  An undone change and the undo
    # entry that undid it cancel out, so they never conflict.
    def _footprint(self, log: list[tuple]) -> dict[str, list[str]]:
        """Derive the footprint of an old entry that has no stored one."""
        writes: set[str] = set()
        reads: set[str] = set()
        for step in log:
            self._step_keys(step, writes, reads)
        return {"w": sorted(writes), "r": sorted(reads - writes)}

    def _step_keys(self, step: tuple, writes: set[str], reads: set[str]) -> None:
        """Add the records that one undo step touches to ``writes`` and ``reads``."""
        kind = step[0]
        if kind in ("put_node", "drop_node"):
            nid = step[1]["id"] if kind == "put_node" else step[1]
            writes.add(f"n:{nid}")
        elif kind in ("put_edge", "drop_edge"):
            if kind == "put_edge":
                rec = step[1]
                eid = edge_id(rec.get("source"), rec.get("target"), rec.get("relation"))
            else:
                eid = step[1]
                found = self._edges.get(eid)
                rec = found[0] if found else self.overlay["edges"].get(eid) or {}
            writes.add(f"e:{eid}")
            for end in ("source", "target"):
                if rec.get(end) is not None:
                    reads.add(f"n:{rec[end]}")
        elif kind in ("restore_overlay", "drop_overlay"):
            writes.add(f"{'e' if step[1] == 'edges' else 'n'}:{step[2]}")
        elif kind == "put_ontology":
            writes.add("o")
        elif kind in ("put_doc", "drop_doc", "restore_doc"):
            writes.add(f"d:{step[1]}")
        elif kind in ("drop_asset", "restore_asset"):
            writes.add(f"a:{step[1]}/{step[2]}")
            reads.add(f"d:{step[1]}")

    def _entry_keys(self, entry: dict[str, Any]) -> tuple[set[str], set[str]]:
        keys = entry.get("keys")
        if not isinstance(keys, dict):
            # An entry from before footprints: derive it from its undo steps.
            keys = self._footprint([tuple(s) for s in entry.get("undo", [])])
        return set(keys.get("w") or ()), set(keys.get("r") or ())

    def _index_keys(self) -> None:
        """Index the footprints of the new history entries by record key (item 13).

        Undo then reads only the entries that share a key with its target, not
        the whole history.
        """
        for entry in self.history[self._indexed:]:
            try:
                seq = int(entry["seq"])
            except (KeyError, TypeError, ValueError):
                continue
            if entry.get("op") == "undo":
                continue
            w, r = self._entry_keys(entry)
            self._keys_of[seq] = (w, r)
            for key in w | r:
                self._key_index.setdefault(key, []).append(seq)
        self._indexed = len(self.history)

    def _undo_conflicts(self, target: dict[str, Any]) -> tuple[list[int], list[str]]:
        """Return the later live changes that depend on ``target`` and the shared records."""
        self._index_keys()
        first = int(target["seq"])
        writes, _reads = self._keys_of.get(first) or self._entry_keys(target)
        found: set[int] = set()
        shared: set[str] = set()
        for key in writes:
            for seq in self._key_index.get(key, ()):
                if seq > first and seq not in self._undone:
                    found.add(seq)
                    shared.add(key)
        return sorted(found), sorted(shared)

    # -- history file ----------------------------------------------------
    # ``kb/history.jsonl`` only grows: an undo is a new entry.  Each process
    # reads the lines that other processes added since its last read.
    def _reset_history(self) -> None:
        self.history = []
        self._undone = set()
        self._max_seq = 0
        self._hist_pos = 0
        self._hist_ino = None
        self._key_index: dict[str, list[int]] = {}
        self._keys_of: dict[int, tuple[set[str], set[str]]] = {}
        self._indexed = 0

    def _add_history_entry(self, entry: dict[str, Any]) -> None:
        entry["undo"] = [tuple(step) for step in entry.get("undo", [])]
        self.history.append(entry)
        try:
            self._max_seq = max(self._max_seq, int(entry["seq"]))
            if entry.get("op") == "undo" and entry.get("target"):
                self._undone.add(int(entry["target"]))
        except (KeyError, TypeError, ValueError):
            pass

    def _read_history(self, *, full: bool = False) -> None:
        """Read the history entries that are new since the last read."""
        stat = _file_stat(self.history_path)
        if full or stat is None or stat[0] != self._hist_ino or stat[1] < self._hist_pos:
            self._reset_history()
        self._seen["history"] = stat
        if stat is None:
            return
        with open(self.history_path, "rb") as fh:
            fh.seek(self._hist_pos)
            chunk = fh.read()
        end = chunk.rfind(b"\n")
        self._hist_ino = stat[0]
        if end < 0:
            return
        for raw in chunk[: end + 1].splitlines():
            try:
                entry = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(entry, dict) and "seq" in entry:
                self._add_history_entry(entry)
        self._hist_pos += end + 1

    def _append_history(self, entry: dict[str, Any]) -> None:
        """Append one entry.  The caller holds the store lock."""
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(entry, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with open(self.history_path, "ab") as fh:
            if fh.tell() > 0:
                # A writer that died during an append leaves a line without
                # its end.  Close it, so that this entry is its own line.
                with open(self.history_path, "rb") as check:
                    check.seek(-1, os.SEEK_END)
                    if check.read(1) != b"\n":
                        line = b"\n" + line
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
            size = fh.tell()
        stat = _file_stat(self.history_path)
        self._hist_pos = size
        self._hist_ino = stat[0] if stat else None
        self._seen["history"] = stat

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
        """Return the class of ``node``, as ``validate_graph`` finds it (S5).

        Methods and their owners are known by their ``method`` edges, so the
        two edge sets come from the graph once per version.
        """
        targets, owners = self._cached("method_sets", self._method_sets)
        return self.ontology.node_class(node, method_targets=targets, method_owners=owners) or "unclassified"

    def _method_sets(self) -> tuple[set[str] | None, set[str] | None]:
        if self.ontology.class_key != "file_type":
            return None, None
        return method_targets(self.data), method_owners(self.data)

    def _check_class(self, cls: Any) -> str:
        """Return a valid class name for a node write (bug 18).

        An unknown class is an error in a strict ontology.  Otherwise the
        world is open: the write goes in and validation reports the class as
        the warning ``unknown_class``.
        """
        name = str(cls).strip()
        if not name or len(name) > 80 or "\n" in name:
            raise KBError(f"invalid class name {cls!r}")
        if self.ontology.knows(name) or not self.ontology.strict:
            return name
        known = set(self.ontology.classes)
        if self.ontology.class_key == "file_type":
            known |= set(TAXONOMY)
        raise KBError(f"class {name} is not in the strict ontology. Known classes: {', '.join(sorted(known))}")

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
            # Report 2 (#8161): tell apart nodes that share a label.
            "other_source_file": other_node.get("source_file") or None,
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
        """Return the node whose label matches ``label``; a KB node wins.

        The match key is the linker's ``label_key`` (folded case, no ``()``,
        no leading dot), so ``find_node`` and the linker agree.  The lookup
        uses the cached link index, not a scan of all nodes.
        """
        key = label_key(label)
        if not key:
            return None
        with self._lock:
            if self._undo_log is not None:
                # Inside a change the cached index can miss new nodes.
                hits = [str(n.get("id")) for n in self.data["nodes"] if label_key(n.get("label")) == key]
            else:
                rest, order = self._cached("label_rest", self._label_rest)
                hits = list(self._link_index().candidates(key)) + rest.get(key, [])
                hits.sort(key=lambda nid: order.get(nid, len(order)))
            nodes = [self._nodes[nid] for nid in hits if nid in self._nodes]
            for node in nodes:
                if node.get("_origin") == KB_ORIGIN:
                    return node
            return nodes[0] if nodes else None

    def _label_rest(self) -> tuple[dict[str, list[str]], dict[str, int]]:
        """Index the nodes that the link index leaves out, and the node order."""
        index = self._link_index()
        rest: dict[str, list[str]] = {}
        order: dict[str, int] = {}
        for pos, node in enumerate(self.data["nodes"]):
            nid = str(node.get("id"))
            order.setdefault(nid, pos)
            key = label_key(node.get("label"))
            if key and nid not in index.candidates(key):
                rest.setdefault(key, []).append(nid)
        return rest, order

    def search(self, query: str, *, limit: int = 50, cls: str | None = None) -> list[dict[str, Any]]:
        q = str(query or "").strip().lower()
        if not q:
            return []
        terms = q.split()
        hits: list[tuple[float, dict[str, Any]]] = []
        by_id: dict[str, tuple[float, dict[str, Any]]] = {}
        with self._lock:
            for node in self.data["nodes"]:
                node_cls = self.node_class(node)
                # A class filter also takes the subclasses (RDFS rule rdfs9): code finds code.function.
                if cls and not self.ontology.is_a(node_cls, cls):
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
                    by_id[str(node.get("id"))] = (score, {
                        "kind": "node",
                        "id": node.get("id"),
                        "label": node.get("label"),
                        "class": node_cls,
                        "source_file": node.get("source_file"),
                        "score": round(score, 2),
                    })
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
                    # Report 2 (#8161): one hit for each document.  The
                    # document row replaces the hit of its ``doc:`` node.
                    twin = by_id.pop(str(meta.get("node_id")), None)
                    if twin is not None:
                        score = max(score, twin[0])
                    if score:
                        snippet = body[max(0, pos - 60): pos + 120] if pos >= 0 else body[:160]
                        hits.append((score, {
                            "kind": "document",
                            "id": meta["id"],
                            "node": meta.get("node_id"),
                            "label": meta.get("title"),
                            "class": "document",
                            "source_file": twin[1]["source_file"] if twin else None,
                            "snippet": " ".join(snippet.split()),
                            "score": round(score, 2),
                        }))
        hits.extend(by_id.values())
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
                "norm_label": graph_norm_label(label),
                "_origin": KB_ORIGIN,
                "created_at": stamp,
                "updated_at": stamp,
                "actor": actor or "api",
            })
            if cls:
                rec["class"] = self._check_class(cls)
            if ftype == "document":
                # S4: a document node is a page, as in graphify's markdown extraction.
                rec.setdefault("node_kind", "page")
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
                rec["norm_label"] = graph_norm_label(rec["label"])
            if changes.get("class") is not None:
                rec["class"] = self._check_class(changes["class"])
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
            if node is None and str(nid) in self.overlay["patches"]:
                # A stale patch (R6): the graph lost the node, the overlay kept its changes.
                self._p_drop_overlay("patches", str(nid))
                return {"deleted": str(nid), "edges_deleted": 0, "stale_patch": True}
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

    def move_node(self, old: str, new: str, *, actor: str | None = None) -> dict[str, Any]:
        """Send the overlay records of code node ``old`` to its new id ``new``.

        A rename or a file move gives a code node a new id.  Its changes, edges
        and deletion then follow it (see ``_apply_moves``).  The result
        ``status`` is one of these:

        - ``pending``: the graph still has ``old``, so the records follow after
          the next graphify rebuild.  An undo removes the move.
        - ``moved``: the graph has the new code, so the records follow now.
          The code moved, so an undo keeps them at the new id.
        - ``skipped``: the graph does not have ``old`` and the overlay has no
          records of it.  Nothing changes.
        """
        old, new = str(old or ""), str(new or "")
        if not old or not new:
            raise KBError("a move needs from and to")
        if old == new:
            raise KBError("from and to are the same node")
        with self._tx("move_node", f"Move node {old} to {new}", actor):
            ov = self.overlay
            for nid in (old, new):
                if nid in ov["nodes"] or is_kb_record(self._nodes.get(nid)):
                    raise KBError(f"node {nid} is a knowledge-base node: only code nodes move")
            moves = ov["moved_nodes"]
            if old in self._nodes or old in moves.values():
                status = "pending"
            elif not _has_records(old, ov["patches"], ov["deleted_nodes"], ov["edges"]):
                return {"from": old, "to": new, "status": "skipped"}
            elif _move_target(old, {**moves, old: new}, self._nodes) is not None:
                status = "moved"
            else:
                status = "pending"
            self._p_put_overlay("moved_nodes", old, new)
            return {"from": old, "to": new, "status": status}

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
            if not existing:
                other = self._pair_links(s, t)
                if other:
                    x = other[0]
                    raise Conflict(
                        f"{x.get('source')} -{x.get('relation')}-> {x.get('target')} already joins "
                        "these nodes. Graphify keeps one edge between two nodes: update or "
                        "delete that edge."
                    )
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
                    "weight": float(extra.get("weight", 1.0) or 1.0),
                    "_origin": KB_ORIGIN,
                    "created_at": now_iso(),
                    "actor": actor or "api",
                })
                # An explicit score and source file win: ingest gives a mention
                # its document body as provenance and its linker's score.
                if extra.get("confidence_score") is None:
                    rec["confidence_score"] = _CONFIDENCE_SCORE_DEFAULTS.get(conf, _CONFIDENCE_SCORE_DEFAULTS["AMBIGUOUS"])
                if not extra.get("source_file"):
                    rec["source_file"] = KB_SOURCE
                created = True
            _check_edge_provenance(rec)
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
            _check_edge_provenance(rec)
            self._p_put_edge(rec)
            out = copy.deepcopy(rec)
            out["id"] = eid
            return out

    def delete_edge(self, eid: str, *, actor: str | None = None) -> dict[str, Any]:
        with self._lock:
            records = self._edges.get(eid)
            doc_id = self._mention_doc(records[0]) if records else None
        if doc_id:
            # N4: a document's mentions edge changes the document too.
            return self._unlink(eid, doc_id, actor)
        with self._tx("delete_edge", f"Delete edge {eid}", actor):
            if eid in self._edges:
                self._p_drop_edge(eid)
                return {"deleted": eid}
            if eid in self.overlay["edges"]:
                # A stale or conflicting overlay edge (R6): it is only in the overlay.
                self._p_drop_overlay("edges", eid)
                return {"deleted": eid, "overlay_only": True}
            raise NotFound(f"edge {eid} does not exist")

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
        if kind == "move_node":
            return self.move_node(str(op.get("from") or ""), str(op.get("to") or ""), actor=actor)
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
                # #8239: the same default as POST /api/documents.
                link=bool(op.get("link", True)),
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
            onto = self._infer(self.ontology.class_key)
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
        """Return the consistency report of the graph against the ontology.

        One full check runs per version (R7); each call only filters and cuts
        that cached list.  The report also names the overlay records that stay
        out of the graph (R6): ``overlay_conflict``, ``stale_overlay_edge`` and
        ``stale_overlay_patch``.
        """
        try:
            limit = max(0, int(limit))
        except (TypeError, ValueError):
            raise KBError(f"limit must be an integer, not {limit!r}") from None
        with self._lock:
            full = self._cached("validate", self._full_report)
            findings = full["findings"]
            if not include_info:
                findings = [f for f in findings if f["severity"] != "info"]
            counts = Counter(f["severity"] for f in findings)
            by_code = Counter(f["code"] for f in findings)
            return {
                "ok": counts.get("error", 0) == 0,
                "counts": {s: counts.get(s, 0) for s in SEVERITIES},
                "by_code": dict(by_code.most_common()),
                "nodes": full["nodes"],
                "edges": full["edges"],
                "findings": copy.deepcopy(findings[:limit]),
                "truncated": len(findings) > limit,
                "total": len(findings),
                "version": self.version,
            }

    def _full_report(self) -> dict[str, Any]:
        report = validate_graph(self.data, self.ontology, limit=sys.maxsize, include_info=True)
        findings = report["findings"] + self._overlay_findings()
        rank = {s: i for i, s in enumerate(SEVERITIES)}
        findings.sort(key=lambda f: (rank.get(f["severity"], 9), f["code"]))
        return {"findings": findings, "nodes": report["nodes"], "edges": report["edges"]}

    def _overlay_findings(self) -> list[dict[str, Any]]:
        """Report the overlay records that the graph cannot hold now (R6).

        The overlay keeps them, so they come back when the graph has their
        nodes again.  Delete one with ``delete_edge`` or ``delete_node``.
        """
        out: list[dict[str, Any]] = []
        for eid, rec in sorted(self.overlay["edges"].items()):
            if eid in self._edges:
                continue
            s, t = str(rec.get("source")), str(rec.get("target"))
            edge = {"id": eid, "source": s, "target": t, "relation": rec.get("relation")}
            missing = [end for end in (s, t) if end not in self._nodes]
            if missing:
                out.append({
                    "severity": "warning", "code": "stale_overlay_edge",
                    "message": f"Edge {s} {rec.get('relation')} {t} waits for missing node {', '.join(missing)}.",
                    "edge": edge,
                })
            else:
                other = self._pair_links(s, t)
                shown = f"{other[0].get('source')} {other[0].get('relation')} {other[0].get('target')}" if other else "another edge"
                out.append({
                    "severity": "warning", "code": "overlay_conflict",
                    "message": f"Edge {s} {rec.get('relation')} {t} stays out: {shown} already joins these nodes.",
                    "edge": edge,
                })
        for nid in sorted(self.overlay["patches"]):
            if nid not in self._nodes and nid not in self.overlay["deleted_nodes"]:
                out.append({
                    "severity": "warning", "code": "stale_overlay_patch",
                    "message": f"Changes to node {nid} wait: the graph has no node {nid} now.",
                    "node": nid,
                })
        for moved in self.overlay_stats.get("rekeyed_patches") or ():
            out.append({
                "severity": "info", "code": "rekeyed_overlay_patch",
                "message": f"Changes to node {moved['from']} moved to node {moved['to']}: same file, label and class, or a unique label and class.",
                "node": moved["to"],
                "from": moved["from"],
            })
        for moved in self.overlay_stats.get("rekeyed_edges") or ():
            rec = self.overlay["edges"].get(moved["to"]) or {}
            out.append({
                "severity": "info", "code": "rekeyed_overlay_edge",
                "message": f"Edge {moved['from']} moved to edge {moved['to']}: its end nodes have new ids with the same anchors.",
                "edge": {"id": moved["to"], "source": rec.get("source"), "target": rec.get("target"), "relation": rec.get("relation")},
                "from": moved["from"],
            })
        for moved in self.overlay_stats.get("applied_moves") or ():
            out.append({
                "severity": "info", "code": "moved_overlay_node",
                "message": f"Records of node {moved['from']} moved to node {moved['to']}: the code moved the node.",
                "node": moved["to"],
                "from": moved["from"],
            })
        for old, new in sorted(self.overlay["moved_nodes"].items()):
            out.append({
                "severity": "info", "code": "pending_overlay_move",
                "message": f"Records of node {old} move to node {new} when the graph has the new code.",
                "node": old,
                "to": new,
            })
        return out

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
        """Undo the latest change (or change ``seq``) that is not undone yet.

        Undo of an older change is selective (see ``_footprint``).  Raise
        UndoConflict (HTTP 409) with the later seq numbers when a later change
        that is not undone touched the same records or needs them.  Undo those
        first.  Undo of the latest change always works.
        """
        if seq is not None:
            try:
                seq = int(seq)
            except (TypeError, ValueError):
                raise KBError(f"seq must be an integer, not {seq!r}") from None
        with self._lock, self._store_lock():
            self._sync()
            target = None
            for entry in reversed(self.history):
                if entry.get("op") == "undo" or entry["seq"] in self._undone:
                    continue
                if seq is None or entry["seq"] == seq:
                    target = entry
                    break
            if target is None:
                if seq is not None and any(e["seq"] == seq for e in self.history):
                    raise Conflict(f"change {seq} is an undo or is undone already")
                raise NotFound("there is no change to undo" if seq is None else f"change {seq} does not exist")
            gone = [
                str(step[1]["id"]) for step in target.get("undo", [])
                if step[0] == "put_node" and step[1].get("_origin") != KB_ORIGIN
                and str(step[1]["id"]) not in self._nodes
                and str(step[1]["id"]) not in self.overlay["deleted_nodes"]
            ]
            if gone:
                # A graphify rebuild removed these extracted nodes.  Undo must
                # not bring back a node that the code no longer has.
                raise Conflict(
                    f"change {target['seq']} cannot be undone: the graph has no node {', '.join(sorted(gone)[:5])} now"
                )
            later, shared = self._undo_conflicts(target)
            if later:
                shown = ", ".join(_describe_key(k) for k in shared[:5])
                more = f" and {len(shared) - 5} more" if len(shared) > 5 else ""
                nums = ", ".join(str(n) for n in later)
                raise UndoConflict(
                    f"change {target['seq']} cannot be undone: later changes {nums} also changed "
                    f"or need {shown}{more}. Undo the later changes first.",
                    later,
                )
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
                    # D3: the number of open review items, for the document list.
                    meta["review"] = len(meta.get("possible") or [])
                    out.append(meta)
        out.sort(key=lambda m: str(m.get("updated_at") or ""), reverse=True)
        return out

    def get_document(self, doc_id: str) -> dict[str, Any]:
        with self._lock:
            meta = self._read_meta(doc_id)
            if meta is None:
                raise NotFound(f"document {doc_id} does not exist")
            doc = dict(meta)
            # A read shows only the decisions that count; it writes nothing (#8211).
            doc["decisions"] = self._decisions(meta)
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
        for key in DOC_LINK_KEYS:
            for nid in meta.get(key) or []:
                if nid not in seen:
                    seen.append(str(nid))
        return seen

    def _move_doc_links(self, moved: dict[str, str]) -> None:
        """Send the node ids of every document to their new ids in ``moved``.

        ``moved`` is the ``moved_ids`` result of ``apply_overlay``, so the
        mentions edges of the documents went there already.  A re-link reads
        the ids in the meta and drops the edges of other nodes, so the meta
        follows the edges.  This writes outside the undo log, like the moved
        records.  The caller holds the store lock.
        """
        if not moved or not self.docs_dir.is_dir():
            return
        for folder in sorted(self.docs_dir.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            try:
                meta = self._read_meta(folder.name)
            except NotFound:
                continue  # not a document folder
            if meta and move_document_ids(meta, moved, self._nodes):
                _dump_atomic(folder / "meta.json", meta)

    def _link_index(self) -> LinkIndex:
        """Return the entity-linking index of the graph, rebuilt once per version."""
        return self._cached("link_index", lambda: LinkIndex.build(self.data["nodes"], self.data[self.links_key]))

    def _resolve_wiki_links(self, body: str, actor: str | None) -> tuple[list[str], list[dict[str, Any]]]:
        """Resolve ``[[target]]`` links.  Return the node ids and the links to review.

        A link that fits one node uses it.  A link that fits no node creates a
        node.  A link that fits several nodes is never guessed (memo R4).
        """
        ids: list[str] = []
        possible: list[dict[str, Any]] = []
        made: dict[tuple[str, str | None], str] = {}
        for target, cls, offset in wiki_links(body or ""):
            found = resolve_wiki(
                target, self._link_index(), cls=cls, class_of=self.ontology.node_class, onto=self.ontology,
                anchors=ids)
            if found.status in ("id", "unique"):
                nid = found.ids[0]
            elif found.status == "missing":
                key = (graph_norm_label(target), cls)
                if key not in made:
                    made[key] = str(self.add_node(target, cls=cls, actor=actor)["id"])
                nid = made[key]
            else:
                possible.append({
                    "mention": target,
                    "offset": offset,
                    "kind": "wiki",
                    "reason": "ambiguous_wiki",
                    "candidates_total": len(found.ids),
                    "candidates": [
                        {"id": cid, "label": (self._nodes.get(cid) or {}).get("label") or cid,
                         "source_file": (self._nodes.get(cid) or {}).get("source_file") or ""}
                        for cid in found.ids[:5]
                    ],
                })
                continue
            if nid not in ids:
                ids.append(nid)
        return ids, possible

    def _link_document(self, meta: dict[str, Any], body: str, actor: str | None) -> None:
        """Set the wiki, auto and possible links of ``meta`` from ``body``."""
        # A document from before the link flag keeps linking when it had text links.
        meta["link"] = bool(meta.get("link", bool(meta.get("auto_nodes"))))
        # #8211: a re-link drops a link decision whose node left the explicit
        # nodes or the graph.  The caller writes ``meta``, so history covers it.
        if meta.get("decisions"):
            meta["decisions"] = self._decisions(meta)
        # #8177 X1: a re-link keeps the LLM review items whose mention is still in the text.
        low = body.lower()
        llm_items = [
            item for item in meta.get("possible") or []
            if item.get("kind") == "llm" and str(item.get("mention") or "").lower() in low
        ]
        meta["wiki_nodes"], wiki_possible = self._resolve_wiki_links(body, actor)
        meta["auto_nodes"], meta["auto_scores"], meta["possible"] = [], {}, wiki_possible + llm_items
        if meta["link"]:
            decision = self._text_links(meta, body)
            links = self._decided_links(meta, decision.links)
            meta["auto_nodes"] = [str(link["id"]) for link in links]
            meta["auto_scores"] = {str(link["id"]): float(link["confidence_score"]) for link in links}
            meta["possible"] = wiki_possible + decision.possible + llm_items
        meta["possible"] = self._open_items(meta, meta["possible"])

    # -- review decisions (D3) -------------------------------------------
    # A person decides a review item once: "this mention is node N" or
    # "this mention is not a link".  ``meta["decisions"]`` maps the
    # ``label_key`` of the mention to the node id, or to None.  Each re-link
    # applies the decisions after the linker, so a decided item never comes
    # back.  A link decision counts only while its node is an explicit node of
    # the document and is in the graph (#8211); "not a link" always counts.
    def _decisions(self, meta: dict[str, Any]) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        named = {str(n) for n in meta.get("nodes") or []}
        for key, nid in (meta.get("decisions") or {}).items():
            if nid is None or (str(nid) in self._nodes and str(nid) in named):
                out[str(key)] = None if nid is None else str(nid)
        return out

    def _open_items(self, meta: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop the review items whose mention has a decision."""
        decided = self._decisions(meta)
        return [p for p in items if label_key(p.get("mention")) not in decided]

    def _text_links(self, meta: dict[str, Any], body: str) -> Any:
        """Run the linker on ``body``; explicit and wiki nodes are its anchors."""
        anchors = list(meta.get("nodes") or []) + list(meta.get("wiki_nodes") or [])
        return link_text(
            body, self._link_index(), anchors=anchors, exclude=[meta["node_id"]], title=meta.get("title"))

    def _decided_links(self, meta: dict[str, Any], links: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop the text links that a person decided against.

        A decision for a name key (link or "not a link") stops every text
        link of that name (#8229): a link decision keeps only its explicit
        node.  A named or wiki link still wins.
        """
        decided = set(self._decisions(meta))
        if not decided:
            return list(links)
        named = set(meta.get("nodes") or []) | set(meta.get("wiki_nodes") or [])
        return [x for x in links if str(x["id"]) in named or label_key(x.get("mention")) not in decided]

    # -- unlink (N4) ------------------------------------------------------
    def _mention_doc(self, link: dict[str, Any]) -> str | None:
        """Return the document id when ``link`` is a mentions edge of a document node."""
        if link.get("relation") != MENTIONS:
            return None
        src = self._nodes.get(str(link.get("source"))) or {}
        if not is_kb_record(src) or src.get("node_kind") != "page" or not src.get("doc_id"):
            return None
        doc_id = str(src["doc_id"])
        meta = self._read_meta(doc_id)
        return doc_id if meta and meta.get("node_id") == src.get("id") else None

    def _wiki_text(self, body: str, nid: str) -> str:
        """Return the ``[[...]]`` target in ``body`` that resolves to ``nid``."""
        for target, cls, _offset in wiki_links(body or ""):
            found = resolve_wiki(
                target, self._link_index(), cls=cls, class_of=self.ontology.node_class, onto=self.ontology)
            if found.status in ("id", "unique") and found.ids[0] == nid:
                return target
        return str((self._nodes.get(nid) or {}).get("label") or nid)

    def _unlink(self, eid: str, doc_id: str, actor: str | None) -> dict[str, Any]:
        """Delete the mentions edge ``eid`` of a document and change the document with it.

        The next re-link must not add the edge again (N4):
        - an explicit node leaves the explicit nodes (its link decision goes);
        - a text link stores "not a link" for each name key that linked it;
        - a wiki link is in the text, so only a text edit removes it (409).
        """
        target = str(self._edges[eid][0].get("target"))
        label = str((self._nodes.get(target) or {}).get("label") or target)
        title = (self._read_meta(doc_id) or {}).get("title") or doc_id
        with self._tx("unlink", f"Unlink {label} from {title}", actor):
            meta = self._read_meta(doc_id)
            if meta is None:
                raise NotFound(f"document {doc_id} does not exist")
            body = self._read_body(doc_id)
            if target in (meta.get("wiki_nodes") or []):
                raise Conflict(f"the text links it with [[{self._wiki_text(body, target)}]]; edit the text")
            for key in ("nodes", "llm_nodes"):
                if target in (meta.get(key) or []):
                    meta[key] = [n for n in meta[key] if n != target]
            self._link_document(meta, body, actor)
            if target in meta["auto_nodes"]:
                # Every text link has a mention (a same-file link has the file name).
                keys = {label_key(x.get("mention")) for x in self._text_links(meta, body).links if str(x["id"]) == target}
                keys.discard("")
                meta["decisions"] = {**(meta.get("decisions") or {}), **dict.fromkeys(sorted(keys))}
                self._link_document(meta, body, actor)
            meta["updated_at"] = now_iso()
            meta["actor"] = actor or "api"
            self._p_put_doc(doc_id, meta, None)
            self._sync_doc_node(meta, actor)
            if eid in self._edges:
                self._p_drop_edge(eid)
            return {"deleted": eid, "document": doc_id}

    def review_mention(
        self, doc_id: str, mention: str, node: str | None, *, actor: str | None = None
    ) -> dict[str, Any]:
        """Decide the open review items of ``mention`` in a document.

        ``node`` links the mention to that node as an explicit node of the
        document.  ``None`` means "not a link".  Return the document.
        """
        key = label_key(mention)
        if not key:
            raise KBError("mention is empty")
        with self._lock:
            meta = self._read_meta(doc_id)
            if meta is None:
                raise NotFound(f"document {doc_id} does not exist")
            title = meta.get("title") or doc_id
        text = " ".join(str(mention).split())
        with self._tx("review", f"Review {text} in {title}", actor):
            meta = self._read_meta(doc_id)
            if meta is None:
                raise NotFound(f"document {doc_id} does not exist")
            if node is not None and str(node) not in self._nodes:
                raise NotFound(f"node {node} does not exist")
            if not any(label_key(p.get("mention")) == key for p in meta.get("possible") or []):
                raise KBError(f"document {doc_id} has no open review item for {text!r}")
            meta["decisions"] = {**(meta.get("decisions") or {}), key: None if node is None else str(node)}
            if node is not None and str(node) not in (meta.get("nodes") or []):
                meta["nodes"] = [*(meta.get("nodes") or []), str(node)]
            self._link_document(meta, self._read_body(doc_id), actor)
            meta["updated_at"] = now_iso()
            meta["actor"] = actor or "api"
            self._p_put_doc(doc_id, meta, None)
            self._sync_doc_node(meta, actor)
            return self.get_document(doc_id)

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
            "norm_label": graph_norm_label(meta.get("title") or meta["id"]),
            "doc_id": meta["id"],
            # Bug 9: graphify's own markdown pages carry node_kind "page".
            "node_kind": "page",
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
        explicit = set(meta.get("nodes") or []) | set(meta.get("wiki_nodes") or [])
        scores = meta.get("auto_scores") or {}
        for target in sorted(wanted):
            if target not in self._nodes:
                continue
            # Named links are EXTRACTED; text links keep the linker's score (memo R4).
            if target in explicit:
                conf, score = "EXTRACTED", _CONFIDENCE_SCORE_DEFAULTS["EXTRACTED"]
            else:
                conf, score = "INFERRED", float(scores.get(target, _CONFIDENCE_SCORE_DEFAULTS["INFERRED"]))
            have = self._edges.get(edge_id(nid, target, MENTIONS))
            if have and have[0].get("confidence") == conf and have[0].get("confidence_score") == score:
                continue
            try:
                self.add_edge(nid, target, MENTIONS, confidence=conf, attrs={"confidence_score": score}, actor=actor)
            except Conflict:
                # Another edge already joins the pair; graphify keeps one edge.
                continue

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
        link: bool = False,
    ) -> dict[str, Any]:
        """Create a document box with a markdown body and a graph node.

        With ``link=True`` the names in the body are linked to graph nodes
        (memo R4), and a new body links again.
        """
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
                "auto_nodes": [],
                "link": bool(link),
                "assets": [],
                "source": source,
                "node_id": f"doc:{new_id}",
                "created_at": stamp,
                "updated_at": stamp,
                "actor": actor or "api",
            }
            self._link_document(meta, body, actor)
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
            retitled = False
            if title is not None:
                new_title = _check_label(title)
                retitled = new_title != meta.get("title")
                meta["title"] = new_title
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
            # N3: the title names the file of a pasted code block, so it relinks too.
            if body is not None or nodes is not None or (retitled and meta.get("link")):
                # Explicit nodes are anchors of the text links, so they also relink.
                self._link_document(meta, self._read_body(doc_id) if body is None else body, actor)
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
        if not str(name or "").strip():
            raise KBError("asset name is empty")
        # Bugs 5 and 20: keep the suffix of a non-ASCII or long name.
        fname = safe_name(name)
        with self._tx("add_asset", f"Attach {fname} to {doc_id}", actor):
            meta = self._read_meta(doc_id)
            if meta is None:
                raise NotFound(f"document {doc_id} does not exist")
            # E2E report 2, N6: a new upload never replaces a stored file.
            fname = self._new_asset_name(doc_id, meta, fname)
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

    def _new_asset_name(self, doc_id: str, meta: dict[str, Any], fname: str) -> str:
        """Return ``fname``, or ``stem-2.ext`` and so on when that name is taken."""
        folder = self._doc_dir(doc_id) / "assets"
        taken = {a.get("name") for a in meta.get("assets", [])}
        stem, dot, ext = fname.rpartition(".")
        if not stem:
            stem, dot, ext = fname, "", ""
        name, n = fname, 2
        while name in taken or (folder / name).exists():
            name = f"{stem}-{n}{dot}{ext}"
            n += 1
        return name

    def asset_path(self, doc_id: str, name: str) -> Path:
        path = self._doc_dir(doc_id) / "assets" / safe_name(name)
        if not path.is_file():
            raise NotFound(f"asset {name} does not exist")
        return path

    def delete_asset(self, doc_id: str, name: str, *, actor: str | None = None) -> dict[str, Any]:
        fname = safe_name(name)
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
    def ingest(
        self,
        name: str,
        *,
        data: bytes | None = None,
        text: str | None = None,
        url: str | None = None,
        content_type: str | None = None,
        mode: str = "text",
        title: str | None = None,
        tags: list[str] | None = None,
        link: bool = True,
        backend: str | None = None,
        model: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Turn a file, text or URL into a document box and connect it to the graph.

        The bytes and the Content-Type decide the format, not only the name.
        ``link`` links the names in the text to graph nodes (memo R4).
        ``mode="llm"`` also adds grounded LLM triples (memo R5).  The LLM call
        runs before any write, so a failed call changes nothing.
        """
        if mode not in ("text", "llm"):
            raise KBError("mode must be text or llm")
        if url:
            got = kb_ingest.fetch(url, max_bytes=MAX_ASSET_BYTES)
            data, content_type, url = got.data, got.content_type, got.url
            name = name or got.name
        if text is not None:
            data, content_type = str(text).encode("utf-8"), content_type or "text/markdown"
        if data is None:
            raise KBError("give data, text or url")
        if len(data) > MAX_ASSET_BYTES:
            raise KBError(f"{name or 'the file'} is larger than {MAX_ASSET_BYTES // 1_048_576} MB")
        conv = kb_ingest.convert(
            bytes(data), name or "document", content_type=content_type, url=url, max_chars=MAX_TEXT_CHARS)
        doc_title = _check_label(title or conv.title)
        plan, llm = None, None
        if mode == "llm":
            plan, llm = self._llm_plan(conv.body, doc_title, backend=backend, model=model)
        with self._tx("ingest", f"Ingest {conv.name}", actor):
            doc = self.create_document(
                doc_title, body=conv.body, tags=tags, source=url or conv.name, actor=actor, link=link)
            if conv.keep_asset:
                self.add_asset(doc["id"], conv.name, bytes(data), mime=content_type, actor=actor)
            added: list[str] = []
            edges = 0
            skipped: list[dict[str, Any]] = []
            meta = self._read_meta(doc["id"]) or {}
            if plan is not None:
                added, edges, skipped = self._apply_plan(plan, meta, actor)
                meta["llm_nodes"] = list(dict.fromkeys(list(meta.get("llm_nodes") or []) + added))
                meta["findings"] = plan.findings
                meta["possible"] = list(meta.get("possible") or []) + plan.review
                self._p_put_doc(doc["id"], meta, None)
                self._sync_doc_node(meta, actor)
            return {
                "document": self.get_document(doc["id"]),
                "mode": mode,
                "kind": conv.kind,
                "linked": len(meta.get("auto_nodes") or []),
                "possible": meta.get("possible") or [],
                "nodes_added": added,
                "edges_added": edges,
                "notes": conv.notes,
                "missing_converters": conv.missing_converters,
                "findings": plan.findings if plan else [],
                "dropped": plan.dropped if plan else [],
                "skipped": skipped,
                "llm": llm,
            }

    def _llm_plan(
        self, body: str, title: str, *, backend: str | None, model: str | None
    ) -> tuple[kb_ingest.Plan, dict[str, Any]]:
        """Ask the LLM for triples and check them against the text and the graph (memo R5).

        The document id is a guess here; ``_apply_plan`` maps it to the real id.
        """
        with self._lock:
            guess = self._new_doc_id(title)
            index = self._link_index()
            summary = kb_ingest.ontology_summary(self.ontology)
            anchors = [str(link["id"]) for link in link_text(body, index).links]
            onto = self.ontology
        backend = kb_ingest.resolve_backend(backend)
        # B-ingest #8159: send every chunk of a long body, not only the first.
        answers: list[tuple[int, str | dict[str, Any]]] = []
        answers += kb_ingest.extract(body, doc_id=guess, title=title, summary=summary, backend=backend, model=model)
        plan = kb_ingest.plan_extraction(
            answers, body=body, doc_id=guess, doc_node=f"doc:{guess}", doc_title=title, index=index, onto=onto,
            class_of=onto.node_class, anchors=anchors, backend=backend, model=model)
        info = {"backend": backend, "model": kb_ingest.default_model(backend, model), **plan.stats}
        return plan, info

    def _apply_plan(
        self, plan: kb_ingest.Plan, meta: dict[str, Any], actor: str | None
    ) -> tuple[list[str], int, list[dict[str, Any]]]:
        """Write the planned nodes and edges.  Return the node ids, the edge count and the skipped edges."""
        doc_node = meta["node_id"]
        source_file = f"{self.out_dir.name}/kb/documents/{meta['id']}/body.md"
        ids: dict[str, str] = {}
        guess = next((e["attrs"].get("doc_id") for e in plan.edges if e.get("attrs")), None)
        if guess:
            ids[f"doc:{guess}"] = doc_node
        for key, spec in plan.nodes.items():
            attrs = {k: v for k, v in spec.items() if k not in ("label", "class", "file_type")}
            attrs["doc_id"] = meta["id"]
            node = self.add_node(
                spec["label"], cls=spec.get("class"), file_type=spec.get("file_type") or "concept",
                source_file=source_file, attrs=attrs, actor=actor)
            ids[key] = str(node["id"])
        edges = 0
        skipped: list[dict[str, Any]] = []
        for edge in plan.edges:
            s = ids.get(edge["source"]) or str(edge["source"])
            t = ids.get(edge["target"]) or str(edge["target"])
            rel = edge["relation"]
            attrs = dict(edge.get("attrs") or {})
            attrs.update({"doc_id": meta["id"], "confidence_score": edge["confidence_score"], "source_file": source_file})
            if s == doc_node and rel != MENTIONS:
                # A typed LLM edge replaces the plain text link of the same pair.
                mention = edge_id(doc_node, t, MENTIONS)
                if mention in self._edges:
                    self._p_drop_edge(mention)
            try:
                self.add_edge(s, t, rel, confidence=edge["confidence"], attrs=attrs, actor=actor)
                edges += 1
            except Conflict:
                skipped.append({"source": s, "target": t, "relation": rel, "reason": "pair_has_edge"})
            except KBError as exc:
                skipped.append({"source": s, "target": t, "relation": rel, "reason": str(exc)})
        return [v for k, v in ids.items() if k in plan.nodes], edges, skipped
