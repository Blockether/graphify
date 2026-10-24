"""Storage for the knowledge-base overlay and its history.

A store holds what ``kb/overlay.json`` and ``kb/history.jsonl`` held before:

- one table for each overlay kind, with one record for each id, in the order
  of insertion, so the records keep their dict order;
- the history, one entry for each change, with its undo steps and its record
  keys;
- meta values: the commit counter ``seq``, the overlay stats and the state of
  ``graph.json`` that holds the whole overlay (``materialized``).

``KBStore`` is the interface of each store.  Its ``OverlayTable`` objects keep
the writes of a change in memory until ``KBStore.commit`` writes them in one
step; ``KBStore.discard`` drops them (a rollback).  Two backends come with
graphify:

- ``sqlite`` (``SQLiteStore``, the default) writes ``kb/store.sqlite``: one
  table for each overlay kind, in the order of the SQLite ``rowid``.  From
  ``ZDICT_ROWS`` rows, each row has zstd compression with one dictionary that
  the store trains from its own rows.  A change writes only the rows that it
  touched, in one transaction, so its cost does not grow with the size of the
  store.  The tables read their rows on demand: a store opens without the
  overlay in memory.
- ``memory`` (``MemoryStore``) keeps everything in the process, for tests and
  for knowledge bases that need no saved edits.

For another database, subclass ``KBStore``, implement its primitive methods
and make the backend known: call ``register_store``, declare an entry point of
the group ``graphify.kb_stores``, or name it as ``module:attr``.
``open_store`` finds the backend of a folder.  A persistent backend other than
SQLite records its name in ``kb/store.json``.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import itertools
import json
import os
import sqlite3
import tempfile
import threading
import zlib
from collections.abc import Callable, Iterable, Iterator, MutableMapping, Sequence
from pathlib import Path
from typing import Any

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - orjson is a dependency on CPython
    _orjson = None

try:
    import zstandard as _zstd
except ImportError:  # pragma: no cover - zstandard is a dependency
    _zstd = None

STORE_FILE = "store.sqlite"
# A knowledge base of a persistent backend other than SQLite names it here.
STORE_MARKER = "store.json"
# The backend of a new knowledge base when the caller names none.
STORE_ENV = "GRAPHIFY_KB_STORE"
STORE_ENTRY_POINTS = "graphify.kb_stores"
DEFAULT_STORE = "sqlite"
OVERLAY_KINDS = ("nodes", "patches", "deleted_nodes", "edges", "deleted_edges", "moved_nodes")
SCHEMA_VERSION = 1
# ``KBStore.read_row`` returns this for a record that the store does not hold.
MISSING: Any = object()
_UPSERT = sqlite3.sqlite_version_info >= (3, 24, 0)
_BATCH = 2048
# A table with at most this many rows keeps its ids in memory, so a read of a
# missing row needs no query.  Patches, deletions and moves are small tables.
_KEY_CACHE = 65536
# From this many overlay rows, the store trains its row dictionary and
# compresses each row.  The dictionary never changes after that.
ZDICT_ROWS = 1000
_ZDICT_SIZE = 16384
_ZDICT_SAMPLES = 4000
_ZSTD_LEVEL = 3
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"  # no JSON text starts with these bytes
# ``replace_overlay`` encodes this many rows at a time, and compresses them in many threads.
_BULK = 32768


def dumps(value: Any) -> bytes:
    """Return ``value`` as compact UTF-8 JSON, the same data as ``json.dumps``.

    orjson is about ten times faster.  A value that it refuses (a lone
    surrogate, an integer of more than 64 bits) goes to ``json``.
    """
    if _orjson is not None:
        try:
            return _orjson.dumps(value, default=str, option=_orjson.OPT_NON_STR_KEYS)
        except TypeError:
            pass
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8", "surrogatepass")


def loads(raw: bytes | bytearray | memoryview | str) -> Any:
    """Parse JSON text.  orjson refuses ``NaN``: ``json`` reads it.

    orjson reads an integer of more than 64 bits as a float.  Graph files
    hold no such numbers.
    """
    if _orjson is not None:
        try:
            return _orjson.loads(raw)
        except ValueError:
            pass
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    return json.loads(raw)


def _pack(value: Any) -> bytes:
    return zlib.compress(dumps(value), 1)


def _unpack(blob: bytes | None) -> Any:
    return None if blob is None else loads(zlib.decompress(blob))


def _entry(keys: bytes | None, blob: bytes) -> dict[str, Any]:
    entry = _unpack(blob)
    entry["undo"] = [tuple(step) for step in entry.get("undo") or []]
    if keys is not None:
        entry["keys"] = _unpack(keys)
    return entry


def _history_target(target: Any) -> int | None:
    """Return the change that an undo entry reverts, as the history keeps it."""
    return int(target) if isinstance(target, int) or (isinstance(target, str) and target.isdigit()) else None


def _file_ident(path: Path) -> tuple[int, int] | None:
    """Return ``(device, inode)`` of ``path``, or None without the file."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=60, isolation_level=None, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA cache_size=-65536")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB) WITHOUT ROWID")
        for kind in OVERLAY_KINDS:
            db.execute(
                f"CREATE TABLE IF NOT EXISTS ov_{kind} (rid INTEGER PRIMARY KEY, id TEXT NOT NULL, data BLOB NOT NULL)"
            )
            # Not a UNIQUE column: ``replace_overlay`` drops this index during its bulk insert.
            db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ov_{kind}_id ON ov_{kind} (id)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS history (seq INTEGER PRIMARY KEY, ts TEXT, actor TEXT, op TEXT, "
            "summary TEXT, target INTEGER, steps INTEGER, keys BLOB, entry BLOB NOT NULL)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS history_undo ON history (target) WHERE op = 'undo'")
        db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        db.close()
        raise
    return db


class OverlayTable(MutableMapping):
    """One overlay kind of a store, read on demand.

    The table acts as a dict in insertion order.  A write stays in memory
    until ``KBStore.commit`` writes it; ``KBStore.discard`` drops it (a
    rollback).  A read returns a new copy of the record each time, so a caller
    that changes a record in place assigns it again.  ``setdefault`` keeps the
    record that it returns as a write, because the caller then changes it.
    """

    def __init__(self, store: KBStore, kind: str) -> None:
        self._store = store
        self.kind = kind
        self._dirty: dict[Any, Any] = {}
        self._gone: set[Any] = set()  # saved records that the pending change deletes

    # -- pending writes --------------------------------------------------
    def pending(self) -> bool:
        return bool(self._dirty or self._gone)

    def changes(self) -> tuple[dict[Any, Any], set[Any]]:
        """Return the pending ``(puts, deletes)``, as ``KBStore.write`` takes them."""
        return self._dirty, self._gone

    def discard(self) -> None:
        """Drop the pending writes."""
        self._dirty = {}
        self._gone = set()

    # -- the mapping -----------------------------------------------------
    def __getitem__(self, key: Any) -> Any:
        if key in self._dirty:
            return self._dirty[key]
        if key in self._gone:
            raise KeyError(key)
        value = self._store.read_row(self.kind, key)
        if value is MISSING:
            raise KeyError(key)
        return value

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        if key in self._dirty:
            return True
        if key in self._gone:
            return False
        return self._store.has_row(self.kind, key)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._dirty[key] = value

    def __delitem__(self, key: Any) -> None:
        if key in self._dirty:
            del self._dirty[key]
            if key not in self._gone and self._store.has_row(self.kind, key):
                self._gone.add(key)
            return
        if key in self._gone or not self._store.has_row(self.kind, key):
            raise KeyError(key)
        self._gone.add(key)

    def pop(self, key: Any, default: Any = MISSING) -> Any:
        if key in self._dirty:
            value = self._dirty[key]
            del self[key]
            return value
        value = MISSING if key in self._gone else self._store.read_row(self.kind, key)
        if value is MISSING:
            if default is MISSING:
                raise KeyError(key)
            return default
        self._gone.add(key)
        return value

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key in self._dirty:
            return self._dirty[key]
        value = self.get(key, MISSING)
        if value is MISSING:
            value = default
        self._dirty[key] = value
        return value

    def clear(self) -> None:
        self._dirty = {}
        self._gone = set(self._store.row_keys(self.kind))

    def items(self) -> Iterator[tuple[Any, Any]]:  # type: ignore[override]
        """Yield ``(id, record)`` in insertion order, with the pending writes."""
        dirty, gone = self._dirty, self._gone
        seen: set[Any] = set()
        for key, value in self._store.row_items(self.kind):
            if key in gone:
                continue
            if key in dirty:
                seen.add(key)
                yield key, dirty[key]
            else:
                yield key, value
        for key, value in list(dirty.items()):
            if key not in seen:
                yield key, value

    def keys(self) -> Iterator[Any]:  # type: ignore[override]
        return iter(self)

    def values(self) -> Iterator[Any]:  # type: ignore[override]
        return (value for _key, value in self.items())

    def __iter__(self) -> Iterator[Any]:
        dirty, gone = self._dirty, self._gone
        seen: set[Any] = set()
        for key in self._store.row_keys(self.kind):
            if key in gone:
                continue
            if key in dirty:
                seen.add(key)
            yield key
        for key in list(dirty):
            if key not in seen:
                yield key

    def __len__(self) -> int:
        store, kind = self._store, self.kind
        count = store.row_count(kind) - len(self._gone)
        return count + sum(1 for key in self._dirty if key in self._gone or not store.has_row(kind, key))

    def __bool__(self) -> bool:
        if self._dirty:
            return True
        if self._gone:
            return len(self) > 0
        return self._store.row_count(self.kind, 1) > 0

    def __repr__(self) -> str:
        return f"<OverlayTable {self.kind}: {len(self)} records>"

    def to_dict(self) -> dict[str, Any]:
        return dict(self.items())


class HistoryView(Sequence):
    """The history entries of a store, oldest first, read on demand."""

    def __init__(self, store: KBStore) -> None:
        self._store = store

    def __len__(self) -> int:
        return self._store.history_count()

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [self._store.history_nth(i) for i in range(*index.indices(len(self)))]
        count = len(self)
        if index < 0:
            index += count
        if not 0 <= index < count:
            raise IndexError("history index out of range")
        return self._store.history_nth(index)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return self._store.history_entries()

    def __reversed__(self) -> Iterator[dict[str, Any]]:
        return self._store.history_entries(reverse=True)

    def __bool__(self) -> bool:
        return self._store.history_max() > 0


class KBStore:
    """The store of one knowledge base: its overlay tables, its history and its meta values.

    A backend subclasses this class and implements the primitive methods, from
    ``exists`` to ``history_entries``.  The other methods build on them, and a
    backend can replace them with faster ways.  The caller serialises the
    calls: ``KnowledgeBase`` holds its lock, and the cross-process lock
    ``kb/.lock`` serialises the writers of other processes.

    A record goes in as JSON data, and each read returns a new copy.  The
    overlay ids are strings.  ``write`` applies a whole change or nothing.
    """

    #: The name of the backend, for messages.
    name = "custom"
    #: False for a store that keeps nothing after the process: it gets no ``kb/store.json``.
    persistent = True
    #: The file or folder of the store, when it has one.
    path: Path | None = None
    #: The backend name that ``open_store`` used, for ``kb/store.json``.
    spec: str | None = None

    @property
    def tables(self) -> dict[str, OverlayTable]:
        """The overlay tables, one for each kind of ``OVERLAY_KINDS``."""
        tables = self.__dict__.get("_tables")
        if tables is None:
            tables = self.__dict__["_tables"] = {kind: OverlayTable(self, kind) for kind in OVERLAY_KINDS}
        return tables

    # -- primitives: each backend implements these ------------------------
    def exists(self) -> bool:
        """Return True when the store holds data: rows, history or meta values."""
        raise NotImplementedError

    def read_row(self, kind: str, key: Any) -> Any:
        """Return a new copy of the saved record ``key`` of ``kind``, or ``MISSING``."""
        raise NotImplementedError

    def row_count(self, kind: str, limit: int | None = None) -> int:
        """Return the number of saved records of ``kind``; with ``limit``, count at most ``limit``."""
        raise NotImplementedError

    def row_items(self, kind: str) -> Iterator[tuple[Any, Any]]:
        """Yield the saved ``(id, record)`` pairs of ``kind`` in insertion order."""
        raise NotImplementedError

    def write(self, changes: dict[str, tuple[dict[Any, Any], set[Any]]], entry: dict[str, Any] | None, meta: dict[str, Any]) -> int:
        """Write one change in one atomic step.  Return the new commit counter.

        ``changes`` maps an overlay kind to ``(puts, deletes)``.  The deletes
        go first.  Then a put of a saved id replaces its record in place, and a
        put of a new id goes to the end.  ``entry`` is a history entry with its
        ``seq``, or None; an entry with a saved seq stays out.  ``meta`` holds
        meta values to set, and the counter ``seq`` grows by one.
        """
        raise NotImplementedError

    def meta(self, key: str, default: Any = None) -> Any:
        """Return the meta value ``key``, or ``default``."""
        raise NotImplementedError

    def set_meta(self, **values: Any) -> None:
        """Set meta values in one step, without a new commit counter."""
        raise NotImplementedError

    def add_history(self, entries: Iterable[dict[str, Any]]) -> int:
        """Add history entries (an import) and return their number.

        An entry without an integer ``seq``, or with a saved one, stays out.
        """
        raise NotImplementedError

    def history_entries(self, *, reverse: bool = False, after: int = 0) -> Iterator[dict[str, Any]]:
        """Yield the history entries with a seq above ``after``, oldest first.

        Each entry has its ``undo`` steps as tuples, and its ``keys`` when it
        has a ``keys`` dict.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Free what the store holds open.  The next call opens it again."""

    def refresh(self) -> None:
        """Forget what the store keeps about its saved data: another process can change it."""

    # -- built on the primitives ------------------------------------------
    def has_row(self, kind: str, key: Any) -> bool:
        return self.read_row(kind, key) is not MISSING

    def row_keys(self, kind: str) -> Iterator[Any]:
        return (key for key, _value in self.row_items(kind))

    def seq(self) -> int:
        """Return the commit counter: it grows with each write of the store."""
        return int(self.meta("seq", 0) or 0)

    def overlay(self) -> dict[str, Any]:
        """Return the overlay: a dict of the lazy tables, shaped as ``overlay.json``."""
        return {"version": 1, **self.tables}

    def read_overlay(self) -> dict[str, Any]:
        """Return the saved overlay as plain dicts, shaped as ``overlay.json``."""
        return {"version": 1, **{kind: dict(self.row_items(kind)) for kind in OVERLAY_KINDS}}

    def pending(self) -> bool:
        return any(table.pending() for table in self.tables.values())

    def discard(self) -> None:
        """Drop the pending writes (a rollback).  The saved data can have changed, so ``refresh`` runs too."""
        for table in self.tables.values():
            table.discard()
        self.refresh()

    def commit(self, entry: dict[str, Any] | None = None, **meta: Any) -> int:
        """Write the pending writes, ``entry`` and ``meta`` in one step.  Return the new commit counter.

        ``entry`` is one history entry with its ``seq``.
        """
        tables = self.tables
        changes = {kind: table.changes() for kind, table in tables.items() if table.pending()}
        count = self.write(changes, entry, meta)
        for table in tables.values():
            table.discard()
        return count

    def replace_overlay(self, overlay: dict[str, Any], **meta: Any) -> int:
        """Replace every overlay record with the records of ``overlay`` (an import)."""
        self.discard()
        changes: dict[str, tuple[dict[Any, Any], set[Any]]] = {}
        for kind in OVERLAY_KINDS:
            records = overlay.get(kind)
            puts = {str(key): value for key, value in records.items()} if isinstance(records, dict) else {}
            changes[kind] = (puts, set(self.row_keys(kind)))
        return self.write(changes, None, meta)

    def history_count(self) -> int:
        return sum(1 for _entry in self.history_entries())

    def history_max(self) -> int:
        for entry in self.history_entries(reverse=True):
            return int(entry["seq"])
        return 0

    def history_nth(self, index: int) -> dict[str, Any]:
        for entry in itertools.islice(self.history_entries(), max(0, int(index)), None):
            return entry
        raise IndexError("history index out of range")

    def history_entry(self, seq: int) -> dict[str, Any] | None:
        for entry in self.history_entries(after=int(seq) - 1):
            return entry if int(entry["seq"]) == int(seq) else None
        return None

    def history_rows(self, limit: int) -> list[dict[str, Any]]:
        """Return the newest ``limit`` entries without their undo steps, newest first."""
        rows = []
        for entry in itertools.islice(self.history_entries(reverse=True), max(0, int(limit))):
            rows.append({
                "seq": int(entry["seq"]),
                "ts": entry.get("ts"),
                "actor": entry.get("actor"),
                "op": entry.get("op"),
                "summary": entry.get("summary"),
                "steps": len(entry.get("undo") or ()),
                "target": _history_target(entry.get("target")),
            })
        return rows

    def undone(self) -> set[int]:
        """Return the seqs that an undo entry reverted."""
        found = set()
        for entry in self.history_entries():
            target = _history_target(entry.get("target")) if entry.get("op") == "undo" else None
            if target is not None:
                found.add(target)
        return found

    def undo_target(self, seq: int | None, undone: set[int]) -> dict[str, Any] | None:
        """Return the newest change that is not an undo and not undone (or change ``seq``)."""
        if seq is not None:
            entry = self.history_entry(seq)
            if entry is None or entry.get("op") == "undo" or int(seq) in undone:
                return None
            return entry
        for entry in self.history_entries(reverse=True):
            if entry.get("op") != "undo" and int(entry["seq"]) not in undone:
                return entry
        return None

    def has_seq(self, seq: int) -> bool:
        return self.history_entry(seq) is not None

    def later_keys(self, seq: int) -> Iterator[tuple[int, Any, dict[str, Any] | None]]:
        """Yield ``(seq, keys, entry)`` of the changes after ``seq`` that are not undos.

        ``keys`` is the ``keys`` dict of the change.  The whole ``entry`` comes
        only for an old entry without that dict, else None.
        """
        for entry in self.history_entries(after=seq):
            if entry.get("op") == "undo":
                continue
            keys = entry.get("keys")
            if isinstance(keys, dict):
                yield int(entry["seq"]), keys, None
            else:
                yield int(entry["seq"]), None, entry

    def __enter__(self) -> KBStore:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class SQLiteStore(KBStore):
    """The SQLite file ``kb/store.sqlite`` of one knowledge base: the default backend."""

    name = DEFAULT_STORE

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._db: sqlite3.Connection | None = None
        self._ident: tuple[int, int] | None = None  # the file that ``_db`` reads
        self._zdict: Any = None  # the row dictionary, once the store has one
        self._local = threading.local()  # zstd contexts are not thread safe
        self._train_after = ZDICT_ROWS
        # The ids of the file rows of each small table, None for a large table
        # (see ``_KEY_CACHE``).  A kind is absent until its first read.
        self._keys: dict[str, set[Any] | None] = {}

    def exists(self) -> bool:
        return self._db is not None or self.path.is_file()

    def db(self, *, create: bool = False) -> sqlite3.Connection | None:
        if self._db is None:
            if not create and not self.path.is_file():
                return None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = _connect(self.path)
            self._ident = _file_ident(self.path)
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
        self._ident = None
        self._zdict = None
        # Another process can change the file before the next open.
        self._keys = {}

    def refresh(self) -> None:
        self._keys = {}

    # -- meta ------------------------------------------------------------
    def meta(self, key: str, default: Any = None) -> Any:
        db = self.db()
        if db is None:
            return default
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None or row[0] is None else loads(row[0])

    def set_meta(self, **values: Any) -> None:
        db = self.db(create=True)
        assert db is not None
        db.execute("BEGIN IMMEDIATE")
        try:
            self._put_meta(db, values)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    @staticmethod
    def _put_meta(db: sqlite3.Connection, values: dict[str, Any]) -> None:
        db.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [(key, dumps(value)) for key, value in values.items()],
        )

    def seq(self) -> int:
        """Return the commit counter: it grows with each write of the store.

        A copy or a restore can put another file at ``path`` while the
        connection reads the old one.  Each sync of ``KnowledgeBase`` reads
        the counter first, so the store opens the new file here.
        """
        if self._db is not None:
            ident = _file_ident(self.path)
            if self._ident is None:
                self._ident = ident
            elif ident != self._ident:
                self.close()
        return int(self.meta("seq", 0) or 0)

    # -- row compression -------------------------------------------------
    def _dictionary(self, db: sqlite3.Connection | None = None) -> Any:
        """Return the row dictionary of the store, or None before the store has one."""
        if self._zdict is None and _zstd is not None:
            db = db or self.db()
            row = None if db is None else db.execute("SELECT value FROM meta WHERE key = 'zdict'").fetchone()
            if row is not None and row[0]:
                self._zdict = _zstd.ZstdCompressionDict(bytes(row[0]))
        return self._zdict

    def _contexts(self) -> Any:
        """Return the zstd contexts of this thread for the row dictionary."""
        assert _zstd is not None  # a store has its dictionary only with zstandard
        local = self._local
        if getattr(local, "zdict", None) is not self._zdict:
            local.cctx = _zstd.ZstdCompressor(
                level=_ZSTD_LEVEL,
                dict_data=self._zdict,
                write_checksum=False,
                write_content_size=True,
                write_dict_id=False,
            )
            local.dctx = _zstd.ZstdDecompressor(dict_data=self._zdict)
            local.zdict = self._zdict
        return local

    def encode(self, value: Any) -> bytes:
        """Return the row bytes of ``value``: JSON, compressed when the store has its dictionary."""
        raw = dumps(value)
        return raw if self._zdict is None else self._contexts().cctx.compress(raw)

    def decode(self, raw: bytes) -> Any:
        """Return the value of the row bytes ``raw``."""
        if raw.startswith(_ZSTD_MAGIC):
            if self._dictionary() is None:
                raise ValueError(f"{self.path}: a compressed row needs zstandard and the store dictionary")
            raw = self._contexts().dctx.decompress(raw)
        return loads(raw)

    def _ready_dictionary(self, db: sqlite3.Connection, sources: Sequence[Any], *, file_rows: bool) -> bool:
        """Train the row dictionary when the overlay has ``ZDICT_ROWS`` rows.  Return True when it trained.

        The caller holds the write transaction, and resets ``_zdict`` when the
        transaction fails.  ``sources`` holds the record dicts that the
        transaction writes.  With ``file_rows``, the rows of the file count
        too, and they get compression now; ``replace_overlay`` deletes them.
        """
        if _zstd is None or self._dictionary(db) is not None:
            return False
        rows = 0
        if file_rows:
            rows = sum(db.execute(f"SELECT coalesce(max(rid), 0) FROM ov_{kind}").fetchone()[0] for kind in OVERLAY_KINDS)
        count = rows + sum(map(len, sources))
        if count < self._train_after:
            return False
        step = max(1, count // _ZDICT_SAMPLES)
        samples: list[Any] = [dumps(value) for records in sources for value in itertools.islice(records.values(), 0, None, step)]
        if rows:
            for kind in OVERLAY_KINDS:
                cur = db.execute(f"SELECT data FROM ov_{kind} WHERE rid % ? = 0", (step,))
                samples.extend(bytes(raw) for (raw,) in cur)
        try:
            zdict = _zstd.train_dictionary(_ZDICT_SIZE, samples)
        except _zstd.ZstdError:
            self._train_after = count * 2  # too few or too small rows: try again later
            return False
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('zdict', ?)", (zdict.as_bytes(),))
        self._zdict = zdict
        if rows:
            cctx = self._contexts().cctx
            for kind in OVERLAY_KINDS:
                found = db.execute(f"SELECT rid, data FROM ov_{kind}").fetchall()
                db.executemany(
                    f"UPDATE ov_{kind} SET data = ? WHERE rid = ?",
                    [(cctx.compress(raw), rid) for rid, raw in found if not raw.startswith(_ZSTD_MAGIC)],
                )
        return True

    # -- rows ------------------------------------------------------------
    def _maybe_row(self, kind: str, key: Any) -> bool:
        """Return False when the file surely has no row ``key`` of ``kind``."""
        keys = self._keys.get(kind, MISSING)
        if keys is MISSING:
            small = self.row_count(kind, _KEY_CACHE + 1) <= _KEY_CACHE
            keys = self._keys[kind] = set(self.row_keys(kind)) if small else None
        return keys is None or key in keys

    def read_row(self, kind: str, key: Any) -> Any:
        db = self.db()
        if db is None or not self._maybe_row(kind, key):
            return MISSING
        row = db.execute(f"SELECT data FROM ov_{kind} WHERE id = ?", (key,)).fetchone()
        return MISSING if row is None else self.decode(row[0])

    def has_row(self, kind: str, key: Any) -> bool:
        db = self.db()
        if db is None or not self._maybe_row(kind, key):
            return False
        return db.execute(f"SELECT 1 FROM ov_{kind} WHERE id = ?", (key,)).fetchone() is not None

    def row_count(self, kind: str, limit: int | None = None) -> int:
        db = self.db()
        if db is None:
            return 0
        if limit is None:
            return int(db.execute(f"SELECT count(*) FROM ov_{kind}").fetchone()[0])
        sql = f"SELECT count(*) FROM (SELECT 1 FROM ov_{kind} LIMIT ?)"
        return int(db.execute(sql, (int(limit),)).fetchone()[0])

    def _rows(self, kind: str, columns: str) -> Iterator[tuple]:
        db = self.db()
        if db is None:
            return
        cur = db.execute(f"SELECT {columns} FROM ov_{kind} ORDER BY rid")
        while True:
            rows = cur.fetchmany(_BATCH)
            if not rows:
                return
            yield from rows

    def row_keys(self, kind: str) -> Iterator[Any]:
        return (key for (key,) in self._rows(kind, "id"))

    def row_items(self, kind: str) -> Iterator[tuple[Any, Any]]:
        decode = self.decode
        return ((key, decode(raw)) for key, raw in self._rows(kind, "id, data"))

    # -- writes ----------------------------------------------------------
    def write(self, changes: dict[str, tuple[dict[Any, Any], set[Any]]], entry: dict[str, Any] | None, meta: dict[str, Any]) -> int:
        db = self.db(create=True)
        assert db is not None
        db.execute("BEGIN IMMEDIATE")
        trained = False
        try:
            trained = self._ready_dictionary(db, [puts for puts, _deletes in changes.values()], file_rows=True)
            encode = self.encode
            for kind, (puts, deletes) in changes.items():
                table = f"ov_{kind}"
                if deletes:
                    db.executemany(f"DELETE FROM {table} WHERE id = ?", [(key,) for key in deletes])
                if not puts:
                    continue
                rows = [(key, encode(value)) for key, value in puts.items()]
                if _UPSERT:
                    db.executemany(
                        f"INSERT INTO {table} (id, data) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET data = excluded.data",
                        rows,
                    )
                    continue
                for key, raw in rows:
                    if db.execute(f"UPDATE {table} SET data = ? WHERE id = ?", (raw, key)).rowcount == 0:
                        db.execute(f"INSERT INTO {table} (id, data) VALUES (?, ?)", (key, raw))
            if entry is not None:
                self._insert_entries(db, [entry])
            row = db.execute("SELECT value FROM meta WHERE key = 'seq'").fetchone()
            count = int(loads(row[0]) if row else 0) + 1
            self._put_meta(db, {**meta, "seq": count})
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            if trained:
                self._zdict = None
            raise
        for kind, (puts, deletes) in changes.items():
            keys = self._keys.get(kind)
            if keys is not None:
                keys.difference_update(deletes)
                keys.update(puts)
                if len(keys) > _KEY_CACHE:
                    self._keys[kind] = None
        return count

    @staticmethod
    def _insert_entries(db: sqlite3.Connection, entries: Iterable[dict[str, Any]]) -> int:
        rows = []
        for entry in entries:
            try:
                seq = int(entry["seq"])
            except (KeyError, TypeError, ValueError):
                continue
            keys = entry.get("keys")
            body = {k: v for k, v in entry.items() if k != "keys"}
            rows.append((
                seq,
                entry.get("ts"),
                entry.get("actor"),
                entry.get("op"),
                entry.get("summary"),
                _history_target(entry.get("target")),
                len(entry.get("undo") or ()),
                _pack(keys) if isinstance(keys, dict) else None,
                _pack(body),
            ))
        db.executemany(
            "INSERT OR IGNORE INTO history (seq, ts, actor, op, summary, target, steps, keys, entry) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return len(rows)

    def _encode_rows(self, records: dict[Any, Any]) -> Iterator[tuple[str, bytes]]:
        """Yield ``(id, row bytes)`` for many records, for ``replace_overlay``.

        With the row dictionary, each batch of ``_BULK`` rows compresses in
        many threads.  The frames are the same as from ``encode``.
        """
        if self._zdict is None:
            for key, value in records.items():
                yield str(key), dumps(value)
            return
        cctx = self._contexts().cctx
        many = getattr(cctx, "multi_compress_to_buffer", None)
        items = iter(records.items())
        while batch := list(itertools.islice(items, _BULK)):
            raws = [dumps(value) for _key, value in batch]
            blobs: list[bytes] | None = None
            if many is not None and len(raws) > 1:
                try:
                    found = many(raws, threads=-1)
                    blobs = [found[i].tobytes() for i in range(len(found))]
                except (_zstd.ZstdError, TypeError, ValueError):
                    blobs = None
            if blobs is None or len(blobs) != len(raws):
                blobs = [cctx.compress(raw) for raw in raws]
            yield from zip((str(key) for key, _value in batch), blobs)

    def replace_overlay(self, overlay: dict[str, Any], **meta: Any) -> int:
        self.discard()
        db = self.db(create=True)
        assert db is not None
        tables = {kind: overlay.get(kind) for kind in OVERLAY_KINDS}
        tables = {kind: records for kind, records in tables.items() if isinstance(records, dict)}
        db.execute("BEGIN IMMEDIATE")
        trained = False
        try:
            trained = self._ready_dictionary(db, list(tables.values()), file_rows=False)
            for kind in OVERLAY_KINDS:
                db.execute(f"DELETE FROM ov_{kind}")
                records = tables.get(kind)
                if records:
                    # One index build after the rows costs less than an index update for each row.
                    db.execute(f"DROP INDEX IF EXISTS ov_{kind}_id")
                    db.executemany(f"INSERT INTO ov_{kind} (id, data) VALUES (?, ?)", self._encode_rows(records))
                    db.execute(f"CREATE UNIQUE INDEX ov_{kind}_id ON ov_{kind} (id)")
            row = db.execute("SELECT value FROM meta WHERE key = 'seq'").fetchone()
            count = int(loads(row[0]) if row else 0) + 1
            self._put_meta(db, {**meta, "seq": count})
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            if trained:
                self._zdict = None
            raise
        self._keys = {}
        return count

    def add_history(self, entries: Iterable[dict[str, Any]]) -> int:
        """Add history entries (an import).  An entry with a known seq stays out."""
        db = self.db(create=True)
        assert db is not None
        db.execute("BEGIN IMMEDIATE")
        try:
            count = self._insert_entries(db, entries)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return count

    def backup(self, dest: Path | str) -> None:
        """Copy the store into ``dest``: one consistent SQLite file, also during writes."""
        db = self.db()
        target = sqlite3.connect(str(dest))
        try:
            if db is not None:
                db.backup(target)
        finally:
            target.close()

    # -- history ---------------------------------------------------------
    def _history_one(self, sql: str, args: tuple = ()) -> Any:
        db = self.db()
        if db is None:
            return None
        return db.execute(sql, args).fetchone()

    def history_count(self) -> int:
        row = self._history_one("SELECT count(*) FROM history")
        return int(row[0]) if row else 0

    def history_max(self) -> int:
        row = self._history_one("SELECT max(seq) FROM history")
        return int(row[0]) if row and row[0] is not None else 0

    def history_nth(self, index: int) -> dict[str, Any]:
        count = self.history_count()
        if index * 2 >= count:
            row = self._history_one("SELECT keys, entry FROM history ORDER BY seq DESC LIMIT 1 OFFSET ?", (count - 1 - index,))
        else:
            row = self._history_one("SELECT keys, entry FROM history ORDER BY seq LIMIT 1 OFFSET ?", (index,))
        if row is None:
            raise IndexError("history index out of range")
        return _entry(row[0], row[1])

    def history_entry(self, seq: int) -> dict[str, Any] | None:
        row = self._history_one("SELECT keys, entry FROM history WHERE seq = ?", (int(seq),))
        return None if row is None else _entry(row[0], row[1])

    def history_entries(self, *, reverse: bool = False, after: int = 0) -> Iterator[dict[str, Any]]:
        db = self.db()
        if db is None:
            return
        order = "DESC" if reverse else "ASC"
        cur = db.execute(f"SELECT keys, entry FROM history WHERE seq > ? ORDER BY seq {order}", (int(after),))
        while True:
            rows = cur.fetchmany(256)
            if not rows:
                return
            for keys, blob in rows:
                yield _entry(keys, blob)

    def history_rows(self, limit: int) -> list[dict[str, Any]]:
        """Return the newest ``limit`` entries without their undo steps, newest first."""
        db = self.db()
        if db is None:
            return []
        rows = db.execute(
            "SELECT seq, ts, actor, op, summary, steps, target FROM history ORDER BY seq DESC LIMIT ?", (int(limit),)
        ).fetchall()
        names = ("seq", "ts", "actor", "op", "summary", "steps", "target")
        return [dict(zip(names, row)) for row in rows]

    def undone(self) -> set[int]:
        db = self.db()
        if db is None:
            return set()
        rows = db.execute("SELECT target FROM history WHERE op = 'undo' AND target IS NOT NULL").fetchall()
        return {int(row[0]) for row in rows}

    def undo_target(self, seq: int | None, undone: set[int]) -> dict[str, Any] | None:
        """Return the newest change that is not an undo and not undone (or change ``seq``)."""
        db = self.db()
        if db is None:
            return None
        if seq is not None:
            row = db.execute("SELECT keys, entry, op FROM history WHERE seq = ?", (int(seq),)).fetchone()
            if row is None or row[2] == "undo" or int(seq) in undone:
                return None
            return _entry(row[0], row[1])
        cur = db.execute("SELECT seq, keys, entry FROM history WHERE op IS NOT 'undo' ORDER BY seq DESC")
        for found, keys, blob in cur:
            if int(found) not in undone:
                cur.close()
                return _entry(keys, blob)
        return None

    def has_seq(self, seq: int) -> bool:
        return self._history_one("SELECT 1 FROM history WHERE seq = ?", (int(seq),)) is not None

    def later_keys(self, seq: int) -> Iterator[tuple[int, Any, dict[str, Any] | None]]:
        db = self.db()
        if db is None:
            return
        cur = db.execute(
            "SELECT seq, keys, CASE WHEN keys IS NULL THEN entry END FROM history "
            "WHERE seq > ? AND op IS NOT 'undo' ORDER BY seq",
            (int(seq),),
        )
        for found, keys, blob in cur:
            yield int(found), _unpack(keys), None if blob is None else _entry(None, blob)


class MemoryStore(KBStore):
    """A store in the memory of this process, for tests and throwaway knowledge bases.

    It writes no file, so a new store starts empty.  Records go in as JSON,
    so each read returns a new copy, as from a file.  It implements only the
    primitive methods: it is the smallest complete backend.
    """

    name = "memory"
    persistent = False

    def __init__(self, kb_dir: Path | str | None = None) -> None:
        self.path = None if kb_dir is None else Path(kb_dir)
        self._rows: dict[str, dict[str, bytes]] = {kind: {} for kind in OVERLAY_KINDS}
        self._meta: dict[str, bytes] = {}
        self._history: dict[int, tuple[bytes, bytes | None]] = {}

    def exists(self) -> bool:
        return bool(self._meta or self._history or any(self._rows.values()))

    def read_row(self, kind: str, key: Any) -> Any:
        raw = self._rows[kind].get(str(key))
        return MISSING if raw is None else loads(raw)

    def has_row(self, kind: str, key: Any) -> bool:
        return str(key) in self._rows[kind]

    def row_count(self, kind: str, limit: int | None = None) -> int:
        count = len(self._rows[kind])
        return count if limit is None else min(count, int(limit))

    def row_items(self, kind: str) -> Iterator[tuple[Any, Any]]:
        for key, raw in list(self._rows[kind].items()):
            yield key, loads(raw)

    @staticmethod
    def _history_row(entry: dict[str, Any]) -> tuple[int, tuple[bytes, bytes | None]] | None:
        try:
            seq = int(entry["seq"])
        except (KeyError, TypeError, ValueError):
            return None
        keys = entry.get("keys")
        body = {k: v for k, v in entry.items() if k != "keys"}
        return seq, (dumps(body), dumps(keys) if isinstance(keys, dict) else None)

    def write(self, changes: dict[str, tuple[dict[Any, Any], set[Any]]], entry: dict[str, Any] | None, meta: dict[str, Any]) -> int:
        # Encode everything first: a value that fails leaves the store as it was.
        puts = {kind: {str(key): dumps(value) for key, value in records.items()} for kind, (records, _gone) in changes.items()}
        history = None if entry is None else self._history_row(entry)
        count = self.seq() + 1
        values = {**{key: dumps(value) for key, value in meta.items()}, "seq": dumps(count)}
        for kind, (_records, deletes) in changes.items():
            rows = self._rows[kind]
            for key in deletes:
                rows.pop(str(key), None)
            rows.update(puts[kind])
        if history is not None:
            self._history.setdefault(*history)
        self._meta.update(values)
        return count

    def meta(self, key: str, default: Any = None) -> Any:
        raw = self._meta.get(key)
        return default if raw is None else loads(raw)

    def set_meta(self, **values: Any) -> None:
        self._meta.update({key: dumps(value) for key, value in values.items()})

    def add_history(self, entries: Iterable[dict[str, Any]]) -> int:
        rows = [row for row in map(self._history_row, entries) if row is not None]
        for seq, raw in rows:
            self._history.setdefault(seq, raw)
        return len(rows)

    def history_entries(self, *, reverse: bool = False, after: int = 0) -> Iterator[dict[str, Any]]:
        for seq in sorted((seq for seq in self._history if seq > int(after)), reverse=reverse):
            body, keys = self._history[seq]
            entry = loads(body)
            entry["undo"] = [tuple(step) for step in entry.get("undo") or []]
            if keys is not None:
                entry["keys"] = loads(keys)
            yield entry


# -- backends --------------------------------------------------------------
StoreFactory = Callable[[Path], KBStore]
_FACTORIES: dict[str, StoreFactory] = {}


def register_store(name: str, factory: StoreFactory) -> None:
    """Make the backend ``name`` known.  ``factory(kb_dir)`` returns a new store for the folder ``kb_dir``."""
    if not isinstance(name, str) or not name or ":" in name:
        raise ValueError(f"a store name is a non-empty string without ':', not {name!r}")
    _FACTORIES[name] = factory


def store_names() -> list[str]:
    """Return the names of the known backends: the registered ones and the entry points."""
    names = set(_FACTORIES)
    names.update(ep.name for ep in importlib.metadata.entry_points(group=STORE_ENTRY_POINTS))
    return sorted(names)


def store_factory(name: str) -> StoreFactory:
    """Return the factory of the backend ``name``.

    ``name`` is a registered name, the name of an entry point of the group
    ``graphify.kb_stores``, or ``module:attr``.  An unknown name raises
    ValueError.
    """
    found = _FACTORIES.get(name)
    if found is not None:
        return found
    for ep in importlib.metadata.entry_points(group=STORE_ENTRY_POINTS, name=name):
        factory = ep.load()
        _FACTORIES[name] = factory
        return factory
    if ":" not in name:
        raise ValueError(f"unknown knowledge base store {name!r}; known stores: {', '.join(store_names())}")
    module, _, attr = name.partition(":")
    try:
        found = importlib.import_module(module)
        for part in attr.split("."):
            found = getattr(found, part)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"cannot load the knowledge base store {name!r}: {exc}") from exc
    if not callable(found):
        raise ValueError(f"the knowledge base store {name!r} is not a class or a function")
    return found


def read_store_marker(kb_dir: Path | str) -> str | None:
    """Return the backend name in ``kb/store.json``, or None without the file."""
    path = Path(kb_dir) / STORE_MARKER
    try:
        raw = loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    name = raw.get("store") if isinstance(raw, dict) else None
    if not isinstance(name, str) or not name:
        raise ValueError(f"{path} names no store")
    return name


def resolve_store_name(kb_dir: Path | str, name: str | None = None) -> str:
    """Return the backend name of the knowledge base folder ``kb_dir``.

    ``name`` comes first.  Without it, ``kb/store.json`` names the backend, a
    folder with ``store.sqlite`` keeps SQLite, and a new folder uses
    ``$GRAPHIFY_KB_STORE``, else ``sqlite``.  A ``name`` that differs from the
    backend of the folder raises ValueError: its saved edits would seem lost.

    A ``module:attr`` name loads code.  A cloned folder can hold any
    ``kb/store.json``, so such a saved name must also come from ``name`` or
    ``$GRAPHIFY_KB_STORE``; else this raises ValueError.
    """
    kb_dir = Path(kb_dir)
    env = os.environ.get(STORE_ENV, "").strip()
    saved = read_store_marker(kb_dir) or (DEFAULT_STORE if (kb_dir / STORE_FILE).is_file() else None)
    if name:
        if saved and saved != name:
            raise ValueError(f"{kb_dir} keeps its knowledge base in the store {saved!r}, not in {name!r}")
        return name
    if saved and ":" in saved and saved != env:
        raise ValueError(
            f"{kb_dir} names the store {saved!r}, which loads code; "
            f"open it with store={saved!r} (--store) or set {STORE_ENV}={saved}"
        )
    return saved or env or DEFAULT_STORE


def open_store(kb_dir: Path | str, store: KBStore | StoreFactory | str | None = None) -> KBStore:
    """Return the store of the knowledge base folder ``kb_dir``.

    ``store`` is a ``KBStore``, a factory that takes ``kb_dir``, or a backend
    name (see ``resolve_store_name``).  Only a name goes into ``kb/store.json``
    (see ``mark_store``).
    """
    if isinstance(store, KBStore):
        return store
    kb_dir = Path(kb_dir)
    spec: str | None = None
    if store is None or isinstance(store, str):
        spec = resolve_store_name(kb_dir, store)
        made = store_factory(spec)(kb_dir)
    elif callable(store):
        made = store(kb_dir)
    else:
        raise TypeError(f"store must be a KBStore, a factory or a name, not {type(store).__name__}")
    if not isinstance(made, KBStore):
        raise ValueError(f"the knowledge base store {spec or store!r} gave {type(made).__name__}, not a KBStore")
    made.spec = spec
    return made


def mark_store(kb_dir: Path | str, store: KBStore) -> bool:
    """Write ``kb/store.json`` for a persistent backend other than SQLite.  Return True when it wrote.

    The folder must exist, and a folder with a marker keeps it.  A store that
    ``open_store`` did not open by name gets no marker.
    """
    kb_dir = Path(kb_dir)
    spec = store.spec
    path = kb_dir / STORE_MARKER
    if not spec or spec == DEFAULT_STORE or not store.persistent or not kb_dir.is_dir() or path.exists():
        return False
    fd, tmp = tempfile.mkstemp(prefix=f".{STORE_MARKER}.", suffix=".tmp", dir=str(kb_dir))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(dumps({"store": spec}) + b"\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def read_store_overlay(path: Path | str) -> dict[str, Any] | None:
    """Return the overlay of the SQLite store file ``path`` as plain dicts, or None without the file."""
    path = Path(path)
    if not path.is_file():
        return None
    with SQLiteStore(path) as store:
        return store.read_overlay()


register_store(DEFAULT_STORE, lambda kb_dir: SQLiteStore(Path(kb_dir) / STORE_FILE))
register_store("memory", MemoryStore)
