"""SQLite storage for the knowledge-base overlay and its history.

``kb/store.sqlite`` holds what ``kb/overlay.json`` and ``kb/history.jsonl``
held before:

- one table for each overlay kind, with one row for each record, in the order
  of insertion (the SQLite ``rowid``), so the records keep their dict order;
  from ``ZDICT_ROWS`` rows, each row has zstd compression with one dictionary
  that the store trains from its own rows;
- the history, one row for each change, with its undo steps and its record
  keys compressed;
- a ``meta`` table: the commit counter ``seq``, the overlay stats and the state
  of ``graph.json`` that holds the whole overlay (``materialized``).

A change writes only the rows that it touched, in one SQLite transaction, so
the cost of a change does not grow with the size of the store.  The overlay
tables read their rows on demand: a store opens without the overlay in memory.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import threading
import zlib
from collections.abc import Iterable, Iterator, MutableMapping, Sequence
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
OVERLAY_KINDS = ("nodes", "patches", "deleted_nodes", "edges", "deleted_edges", "moved_nodes")
SCHEMA_VERSION = 1
_MISSING = object()
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
    """One overlay kind, read from SQLite on demand.

    The table acts as a dict in insertion order.  A write stays in memory
    until ``Store.commit`` writes it; ``Store.discard`` drops it (a rollback).
    A read returns a new copy of the record each time, so a caller that changes
    a record in place assigns it again.  ``setdefault`` keeps the record that
    it returns as a write, because the caller then changes it.
    """

    def __init__(self, store: Store, kind: str) -> None:
        self._store = store
        self.kind = kind
        self._table = f"ov_{kind}"
        self._dirty: dict[str, Any] = {}
        self._gone: set[str] = set()  # rows of the file that the pending change deletes
        # The ids of the file rows when the table is small, else None (see ``_KEY_CACHE``).
        self._keys: set[Any] | None = None
        self._keys_read = False

    # -- pending writes --------------------------------------------------
    def pending(self) -> bool:
        return bool(self._dirty or self._gone)

    def discard(self) -> None:
        """Drop the pending writes.  The file can have changed, so the id cache goes too."""
        self._dirty = {}
        self._gone = set()
        self._forget_keys()

    def _forget_keys(self) -> None:
        self._keys = None
        self._keys_read = False

    def _committed(self) -> None:
        """Clear the pending writes after their commit, and keep the id cache current."""
        keys = self._keys
        if keys is not None:
            keys.difference_update(self._gone)
            keys.update(self._dirty)
            if len(keys) > _KEY_CACHE:
                self._keys = None
        self._dirty = {}
        self._gone = set()

    def _maybe_on_disk(self, key: Any) -> bool:
        """Return False when the file surely has no row ``key``."""
        if not self._keys_read:
            self._keys_read = True
            db = self._store.db()
            small = db is not None and db.execute(
                f"SELECT count(*) FROM (SELECT 1 FROM {self._table} LIMIT {_KEY_CACHE + 1})"
            ).fetchone()[0] <= _KEY_CACHE
            self._keys = {k for (k,) in self._rows("id")} if small else None
        keys = self._keys
        return keys is None or key in keys

    def _write(self, db: sqlite3.Connection) -> None:
        table = self._table
        if self._gone:
            db.executemany(f"DELETE FROM {table} WHERE id = ?", [(key,) for key in self._gone])
        if not self._dirty:
            return
        encode = self._store.encode
        rows = [(key, encode(value)) for key, value in self._dirty.items()]
        if _UPSERT:
            db.executemany(
                f"INSERT INTO {table} (id, data) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET data = excluded.data",
                rows,
            )
            return
        for key, raw in rows:
            if db.execute(f"UPDATE {table} SET data = ? WHERE id = ?", (raw, key)).rowcount == 0:
                db.execute(f"INSERT INTO {table} (id, data) VALUES (?, ?)", (key, raw))

    # -- reads of the file -----------------------------------------------
    def _raw(self, key: Any) -> bytes | None:
        db = self._store.db()
        if db is None:
            return None
        if not self._maybe_on_disk(key):
            return None
        row = db.execute(f"SELECT data FROM {self._table} WHERE id = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def _on_disk(self, key: Any) -> bool:
        db = self._store.db()
        if db is None:
            return False
        if not self._maybe_on_disk(key):
            return False
        return db.execute(f"SELECT 1 FROM {self._table} WHERE id = ?", (key,)).fetchone() is not None

    def _disk_count(self) -> int:
        db = self._store.db()
        return 0 if db is None else int(db.execute(f"SELECT count(*) FROM {self._table}").fetchone()[0])

    def _rows(self, columns: str) -> Iterator[tuple]:
        db = self._store.db()
        if db is None:
            return
        cur = db.execute(f"SELECT {columns} FROM {self._table} ORDER BY rid")
        while True:
            rows = cur.fetchmany(_BATCH)
            if not rows:
                return
            yield from rows

    # -- the mapping -----------------------------------------------------
    def __getitem__(self, key: Any) -> Any:
        if key in self._dirty:
            return self._dirty[key]
        if key in self._gone:
            raise KeyError(key)
        raw = self._raw(key)
        if raw is None:
            raise KeyError(key)
        return self._store.decode(raw)

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
        return self._on_disk(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._dirty[key] = value

    def __delitem__(self, key: Any) -> None:
        if key in self._dirty:
            del self._dirty[key]
            if key not in self._gone and self._on_disk(key):
                self._gone.add(key)
            return
        if key in self._gone or not self._on_disk(key):
            raise KeyError(key)
        self._gone.add(key)

    def pop(self, key: Any, default: Any = _MISSING) -> Any:
        if key in self._dirty:
            value = self._dirty[key]
            del self[key]
            return value
        raw = None if key in self._gone else self._raw(key)
        if raw is None:
            if default is _MISSING:
                raise KeyError(key)
            return default
        self._gone.add(key)
        return self._store.decode(raw)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key in self._dirty:
            return self._dirty[key]
        value = self.get(key, _MISSING)
        if value is _MISSING:
            value = default
        self._dirty[key] = value
        return value

    def clear(self) -> None:
        self._dirty = {}
        self._gone = {key for (key,) in self._rows("id")}

    def items(self) -> Iterator[tuple[Any, Any]]:  # type: ignore[override]
        """Yield ``(id, record)`` in insertion order, with the pending writes."""
        dirty, gone, decode = self._dirty, self._gone, self._store.decode
        seen: set[str] = set()
        for key, raw in self._rows("id, data"):
            if key in gone:
                continue
            if key in dirty:
                seen.add(key)
                yield key, dirty[key]
            else:
                yield key, decode(raw)
        for key, value in list(dirty.items()):
            if key not in seen:
                yield key, value

    def keys(self) -> Iterator[Any]:  # type: ignore[override]
        return iter(self)

    def values(self) -> Iterator[Any]:  # type: ignore[override]
        return (value for _key, value in self.items())

    def __iter__(self) -> Iterator[Any]:
        dirty, gone = self._dirty, self._gone
        seen: set[str] = set()
        for (key,) in self._rows("id"):
            if key in gone:
                continue
            if key in dirty:
                seen.add(key)
            yield key
        for key in list(dirty):
            if key not in seen:
                yield key

    def __len__(self) -> int:
        count = self._disk_count() - len(self._gone)
        return count + sum(1 for key in self._dirty if key in self._gone or not self._on_disk(key))

    def __bool__(self) -> bool:
        if self._dirty:
            return True
        if self._gone:
            return len(self) > 0
        db = self._store.db()
        return db is not None and db.execute(f"SELECT 1 FROM {self._table} LIMIT 1").fetchone() is not None

    def __repr__(self) -> str:
        return f"<OverlayTable {self.kind}: {len(self)} records>"

    def to_dict(self) -> dict[str, Any]:
        return dict(self.items())


class HistoryView(Sequence):
    """The history entries of a store, oldest first, read on demand."""

    def __init__(self, store: Store) -> None:
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


class Store:
    """The SQLite file of one knowledge base.

    The caller serialises the calls (``KnowledgeBase`` holds its lock), and
    the cross-process store lock serialises the writers of other processes.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._db: sqlite3.Connection | None = None
        self.tables = {kind: OverlayTable(self, kind) for kind in OVERLAY_KINDS}
        self._zdict: Any = None  # the row dictionary, once the store has one
        self._local = threading.local()  # zstd contexts are not thread safe
        self._train_after = ZDICT_ROWS

    def overlay(self) -> dict[str, Any]:
        """Return the overlay: a dict of the lazy tables, shaped as ``overlay.json``."""
        return {"version": 1, **self.tables}

    def exists(self) -> bool:
        return self._db is not None or self.path.is_file()

    def db(self, *, create: bool = False) -> sqlite3.Connection | None:
        if self._db is None:
            if not create and not self.path.is_file():
                return None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = _connect(self.path)
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
        self._zdict = None
        # Another process can change the file before the next open.
        for table in self.tables.values():
            table._forget_keys()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

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
        """Return the commit counter: it grows with each write of the store."""
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

    def _ready_dictionary(self, db: sqlite3.Connection, incoming: Sequence[Any] | None = None) -> bool:
        """Train the row dictionary when the overlay has ``ZDICT_ROWS`` rows.  Return True when it trained.

        The caller holds the write transaction, and resets ``_zdict`` when the
        transaction fails.  ``incoming`` holds the record dicts of
        ``replace_overlay``; else the pending rows and the rows of the file
        count, and the rows of the file get compression now.
        """
        if _zstd is None or self._dictionary(db) is not None:
            return False
        if incoming is None:
            sources: list[Any] = [table._dirty for table in self.tables.values()]
            rows = sum(db.execute(f"SELECT coalesce(max(rid), 0) FROM ov_{kind}").fetchone()[0] for kind in OVERLAY_KINDS)
        else:
            sources, rows = list(incoming), 0
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

    # -- writes ----------------------------------------------------------
    def pending(self) -> bool:
        return any(table.pending() for table in self.tables.values())

    def discard(self) -> None:
        for table in self.tables.values():
            table.discard()

    def commit(self, entry: dict[str, Any] | None = None, **meta: Any) -> int:
        """Write the pending rows, ``entry`` and ``meta`` in one transaction.

        Return the new commit counter.  ``entry`` is one history entry with its
        ``seq``; its ``keys`` go to their own column.
        """
        db = self.db(create=True)
        assert db is not None
        db.execute("BEGIN IMMEDIATE")
        trained = False
        try:
            trained = self._ready_dictionary(db)
            for table in self.tables.values():
                table._write(db)
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
        for table in self.tables.values():
            table._committed()
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
            target = entry.get("target")
            rows.append((
                seq,
                entry.get("ts"),
                entry.get("actor"),
                entry.get("op"),
                entry.get("summary"),
                int(target) if isinstance(target, int) or (isinstance(target, str) and target.isdigit()) else None,
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

    def replace_overlay(self, overlay: dict[str, Any], **meta: Any) -> int:
        """Replace every overlay row with the records of ``overlay`` (an import)."""
        self.discard()
        db = self.db(create=True)
        assert db is not None
        tables = {kind: overlay.get(kind) for kind in OVERLAY_KINDS}
        tables = {kind: records for kind, records in tables.items() if isinstance(records, dict)}
        db.execute("BEGIN IMMEDIATE")
        trained = False
        try:
            trained = self._ready_dictionary(db, list(tables.values()))
            compress = None if self._zdict is None else self._contexts().cctx.compress
            encode = dumps if compress is None else (lambda value: compress(dumps(value)))
            for kind in OVERLAY_KINDS:
                db.execute(f"DELETE FROM ov_{kind}")
                records = tables.get(kind)
                if records:
                    # One index build after the rows costs less than an index update for each row.
                    db.execute(f"DROP INDEX IF EXISTS ov_{kind}_id")
                    db.executemany(
                        f"INSERT INTO ov_{kind} (id, data) VALUES (?, ?)",
                        ((str(key), encode(value)) for key, value in records.items()),
                    )
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
        for table in self.tables.values():
            table._forget_keys()
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

    def later_keys(self, seq: int) -> Iterator[tuple[int, Any, bytes | None]]:
        """Yield ``(seq, keys, entry blob)`` of the changes after ``seq`` that are not undos.

        The entry blob comes only for an old entry without stored keys.
        """
        db = self.db()
        if db is None:
            return
        cur = db.execute(
            "SELECT seq, keys, CASE WHEN keys IS NULL THEN entry END FROM history "
            "WHERE seq > ? AND op IS NOT 'undo' ORDER BY seq",
            (int(seq),),
        )
        for found, keys, blob in cur:
            yield int(found), _unpack(keys), blob


def entry_from_blob(blob: bytes) -> dict[str, Any]:
    """Decode an entry blob from ``Store.later_keys``."""
    return _entry(None, blob)


def read_store_overlay(path: Path | str) -> dict[str, Any] | None:
    """Return the overlay of the store file ``path`` as plain dicts, or None without the file."""
    path = Path(path)
    if not path.is_file():
        return None
    with Store(path) as store:
        overlay: dict[str, Any] = {"version": 1}
        for kind, table in store.tables.items():
            overlay[kind] = table.to_dict()
        return overlay
