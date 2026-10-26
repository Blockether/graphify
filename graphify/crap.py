"""CRAP scores for Clojure and Python functions.

``CRAP = CC² × (1 − coverage)³ + CC``, the formula of Savoia and Evans. ``CC`` is the
McCabe cyclomatic complexity of one function, counted on its tree-sitter syntax
tree. ``coverage`` comes from a report that a test run already wrote:

- Clojure: the Clofidence form report of ``clj.coverage``
  (``target/coverage/clofidence.json``, format ``vis-coverage/1``).
- Python: coverage.py JSON (``coverage json``) or LCOV (``coverage lcov``).

Speed: tree-sitter queries find the units and the decision points in C, so Python
only touches the matches. A cache keyed on file size, mtime and content skips every
unchanged file. A warm run over a large repository reads one JSON file.

Clojure units are top-level forms: ``defn``, ``defn-``, ``defmethod``, ``def`` of a
``fn``, and each method of ``deftype``, ``defrecord``, ``extend-type``,
``extend-protocol`` and a top-level ``reify``. Macros are not units: their body runs
at compile time. A nested ``fn`` counts toward its enclosing unit.

Clojure decisions: ``if``, ``if-not``, ``if-let``, ``if-some``, ``when``,
``when-not``, ``when-let``, ``when-some``, ``when-first``, ``loop``, ``while``,
``dotimes``, ``doseq``, ``for`` and each ``:when`` / ``:while`` modifier, each
``catch``, each operand after the first of ``and`` / ``or``, each test clause of
``cond``, ``condp``, ``case``, ``cond->`` and ``cond->>``, each step of ``some->`` and
``some->>``, and each arity after the first. A default clause (``:else``, a lone
trailing ``case`` or ``condp`` expression) is not a decision. Quoted and
syntax-quoted forms, ``(comment ...)`` and reader-conditional branches of the other
platform are skipped. ``.clj`` and ``.cljc`` files count the ``:clj`` branch, because
the JVM run measured it; ``.cljs`` files count the ``:cljs`` branch.

Python units are functions and methods; a nested function counts toward its
enclosing unit. Python decisions: ``if``, ``elif``, ``for``, ``while``, ``except``,
each ``case`` except ``case _``, a conditional expression, each ``and`` / ``or``, and
each ``for`` and ``if`` of a comprehension.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
from pathlib import Path

VERSION = 1
"""Cache and unit format version. Bump it when a counting rule changes."""

DEFAULT_THRESHOLD = 10.0
CLOJURE_SUFFIXES = (".clj", ".cljc", ".cljs", ".bb")
PYTHON_SUFFIXES = (".py",)
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "target", "build", "dist",
    "__pycache__", ".cpcache", ".shadow-cljs", ".clj-kondo", ".lsp", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "out",
})
TEST_DIRS = frozenset({"test", "tests", "spec", "specs", "test-native", "__tests__"})
COVERAGE_CANDIDATES = (
    "target/coverage/clofidence.json",
    "coverage.json",
    "coverage.lcov",
    "lcov.info",
    "coverage/lcov.info",
)


# ── result types ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Unit:
    """One function: where it is and how complex it is."""

    path: str
    language: str
    namespace: str
    name: str
    line: int
    end_line: int
    complexity: int
    form_line: int = 0
    """Clojure: first line of the enclosing top-level form."""
    child: int = -1
    """Clojure: element index of a method inside its form, or -1 for the whole form."""
    body_line: int = 0
    """Python: first line of the body, so the executed ``def`` line is not coverage."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One scored function. ``coverage`` and ``crap`` are None when nothing measured it."""

    path: str
    language: str
    namespace: str
    name: str
    line: int
    end_line: int
    complexity: int
    coverage: float | None
    crap: float | None


@dataclass(frozen=True, slots=True)
class Report:
    """A CRAP run: the scored functions, worst first, and what fed them."""

    root: str
    threshold: float
    entries: tuple[Entry, ...]
    files: int
    coverage_reports: tuple[str, ...] = ()
    stale_reports: tuple[str, ...] = ()
    """Coverage reports older than a source file that they score."""
    parse_errors: tuple[str, ...] = ()
    cached_files: int = 0

    @property
    def over(self) -> tuple[Entry, ...]:
        """Entries above the threshold: CRAP when measured, else complexity."""
        return tuple(e for e in self.entries if _score(e) > self.threshold)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["over_threshold"] = len(self.over)
        return data


def crap_score(complexity: int, coverage_pct: float | None) -> float | None:
    """``CC² × (1 − coverage)³ + CC`` with coverage in percent; None without coverage."""
    if coverage_pct is None:
        return None
    cc = float(complexity)
    uncovered = 1.0 - float(coverage_pct) / 100.0
    return round(cc * cc * uncovered ** 3 + cc, 4)


def _score(entry: Entry) -> float:
    return entry.crap if entry.crap is not None else float(entry.complexity)


# ── tree-sitter plumbing ────────────────────────────────────────────────────


@lru_cache(maxsize=None)
def _language(name: str):
    from tree_sitter import Language

    if name == "clojure":
        import tree_sitter_clojure as grammar
    else:
        import tree_sitter_python as grammar
    return Language(grammar.language())


@lru_cache(maxsize=None)
def _parser(name: str):
    from tree_sitter import Parser

    return Parser(_language(name))


@lru_cache(maxsize=None)
def _query(name: str, source: str):
    from tree_sitter import Query

    return Query(_language(name), source)


def _captures(name: str, source: str, node) -> dict:
    from tree_sitter import QueryCursor

    return QueryCursor(_query(name, source)).captures(node)


def _matches(name: str, source: str, node) -> list[dict]:
    """One capture dict per match, so captures of one match stay together."""
    from tree_sitter import QueryCursor

    return [found for _, found in QueryCursor(_query(name, source)).matches(node)]


def _line(node) -> int:
    return node.start_point[0] + 1


def _end_line(node) -> int:
    row, col = node.end_point
    return max(_line(node), row if col == 0 else row + 1)


def _text(data: bytes, node) -> str:
    return data[node.start_byte:node.end_byte].decode("utf-8", "replace")


class _Spans:
    """Sorted, non-overlapping byte spans; answers 'which span holds this byte'."""

    def __init__(self, spans: list[tuple[int, int, object]]):
        spans.sort(key=lambda s: s[0])
        self.starts = [s[0] for s in spans]
        self.spans = spans

    def find(self, byte: int):
        i = bisect.bisect_right(self.starts, byte) - 1
        if i >= 0 and byte < self.spans[i][1]:
            return self.spans[i][2]
        return None


def _merge(spans: list[tuple[int, int]]) -> tuple[list[int], list[int]]:
    """Union of byte spans, as sorted start and end lists for ``_inside``."""
    starts: list[int] = []
    ends: list[int] = []
    for start, end in sorted(spans):
        if ends and start <= ends[-1]:
            ends[-1] = max(ends[-1], end)
        else:
            starts.append(start)
            ends.append(end)
    return starts, ends


def _inside(merged: tuple[list[int], list[int]], byte: int) -> bool:
    starts, ends = merged
    i = bisect.bisect_right(starts, byte) - 1
    return i >= 0 and byte < ends[i]


# ── Clojure ─────────────────────────────────────────────────────────────────

_CLJ_SIMPLE = (
    "if", "if-not", "if-let", "if-some", "when", "when-not", "when-let", "when-some",
    "when-first", "loop", "while", "dotimes", "catch",
)
_CLJ_COUNTED = ("and", "or", "cond", "condp", "case", "cond->", "cond->>", "some->",
                "some->>", "doseq", "for", "fn")
_CLJ_DECISIONS = '(list_lit . value: (sym_lit name: (sym_name) @head (#any-of? @head {names})) ) @form'.format(
    names=" ".join(f'"{n}"' for n in _CLJ_SIMPLE + _CLJ_COUNTED)
)
_CLJ_SKIPPED = '[(quoting_lit) (syn_quoting_lit) (dis_expr)] @skip (list_lit . value: (sym_lit name: (sym_name) @c (#eq? @c "comment"))) @skip (read_cond_lit) @rc (splicing_read_cond_lit) @rc'
_CLJ_FN_HEADS = frozenset({"defn", "defn-"})
_CLJ_TYPE_HEADS = frozenset({"deftype", "defrecord", "extend-type", "extend-protocol", "reify"})
_NS_RE = re.compile(rb"\(\s*ns\s+(?:\^\S+\s+)*([^\s()\[\]{}\"]+)")


def _values(node) -> list:
    return [c for i, c in enumerate(node.children) if node.field_name_for_child(i) == "value"]


def _sym_parts(data: bytes, node) -> tuple[str | None, str] | None:
    if node is None or node.type != "sym_lit":
        return None
    ns = node.child_by_field_name("namespace")
    name = node.child_by_field_name("name")
    if name is None:
        return None
    return (_text(data, ns) if ns is not None else None, _text(data, name))


def _head_name(data: bytes, node) -> str | None:
    if node.type != "list_lit":
        return None
    vals = _values(node)
    parts = _sym_parts(data, vals[0]) if vals else None
    return parts[1] if parts else None


def _is_default_test(data: bytes, node) -> bool:
    if node.type == "kwd_lit":
        return True
    return node.type == "bool_lit" and _text(data, node) == "true"


def _kwd(data: bytes, node) -> str | None:
    if node.type != "kwd_lit":
        return None
    name = node.child_by_field_name("name")
    return _text(data, name) if name is not None else None


def _arities(node) -> int:
    """Arity bodies of a fn-like form: lists that start with a parameter vector."""
    n = 0
    for v in _values(node)[1:]:
        if v.type == "list_lit":
            inner = _values(v)
            if inner and inner[0].type == "vec_lit":
                n += 1
    return n


def _clj_weight(data: bytes, head: str, form) -> int:
    if head in _CLJ_SIMPLE:
        return 1
    vals = _values(form)
    if head in ("and", "or"):
        return max(len(vals) - 2, 0)
    if head == "cond":
        args = vals[1:]
        pairs = len(args) // 2
        if pairs and _is_default_test(data, args[2 * (pairs - 1)]):
            pairs -= 1
        return pairs
    if head == "condp":
        args, i, n = vals[3:], 0, 0
        while i + 1 < len(args):
            step = 3 if _kwd(data, args[i + 1]) == ">>" else 2
            n += 1
            i += step
        return n
    if head == "case":
        return len(vals[2:]) // 2
    if head in ("cond->", "cond->>"):
        return len(vals[2:]) // 2
    if head in ("some->", "some->>"):
        return max(len(vals) - 2, 0)
    if head in ("doseq", "for"):
        n = 1
        if len(vals) > 1 and vals[1].type == "vec_lit":
            n += sum(1 for v in _values(vals[1]) if _kwd(data, v) in ("when", "while"))
        return n
    if head == "fn":
        return max(_arities(form) - 1, 0)
    return 0


def _platform_skips(data: bytes, node, platform: str) -> list[tuple[int, int]]:
    """Byte spans of the reader-conditional branches that ``platform`` does not read."""
    vals = _values(node)
    keys = [(_kwd(data, vals[i]), vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
    names = [k for k, _ in keys]
    keep = platform if platform in names else ("default" if "default" in names else None)
    return [(v.start_byte, v.end_byte) for k, v in keys if k != keep]


def _clj_name(data: bytes, node) -> str:
    parts = _sym_parts(data, node)
    if parts:
        return parts[1]
    return _text(data, node)[:60]


def _clj_units(data: bytes, root, rel: str, namespace: str) -> list[tuple[Unit, int, int]]:
    """Units with the byte span whose decisions they own."""
    out: list[tuple[Unit, int, int]] = []
    for form in root.children:
        if form.type != "list_lit":
            continue
        vals = _values(form)
        if len(vals) < 2:
            continue
        head_parts = _sym_parts(data, vals[0])
        if not head_parts:
            continue
        head = head_parts[1]
        line = _line(form)
        base = dict(path=rel, language="clojure", namespace=namespace, form_line=line)
        if head in _CLJ_FN_HEADS:
            arities = _arities(form)
            unit = Unit(name=_clj_name(data, vals[1]), line=line, end_line=_end_line(form),
                        complexity=1 + max(arities - 1, 0), **base)
            out.append((unit, form.start_byte, form.end_byte))
        elif head == "defmethod" and len(vals) >= 3:
            name = f"{_clj_name(data, vals[1])} {_text(data, vals[2])[:40]}"
            arities = _arities(form)
            unit = Unit(name=name, line=line, end_line=_end_line(form),
                        complexity=1 + max(arities - 1, 0), **base)
            out.append((unit, form.start_byte, form.end_byte))
        elif head == "def" and len(vals) >= 3 and _head_name(data, vals[-1]) in ("fn", "fn*"):
            unit = Unit(name=_clj_name(data, vals[1]), line=line, end_line=_end_line(form),
                        complexity=1, **base)
            out.append((unit, form.start_byte, form.end_byte))
        elif head in _CLJ_TYPE_HEADS:
            owner = _clj_name(data, vals[1]) if head != "reify" else "reify"
            seen: dict[str, int] = {}
            for index, v in enumerate(vals[1:], start=1):
                if v.type == "sym_lit" and head == "extend-protocol":
                    owner = _clj_name(data, v)
                if v.type != "list_lit":
                    continue
                inner = _values(v)
                if not inner or inner[0].type != "sym_lit":
                    continue
                method = f"{owner}.{_clj_name(data, inner[0])}"
                seen[method] = seen.get(method, 0) + 1
                if seen[method] > 1:
                    method = f"{method}#{seen[method]}"
                unit = Unit(name=method, line=_line(v), end_line=_end_line(v), complexity=1,
                            child=index, **base)
                out.append((unit, v.start_byte, v.end_byte))
    return out


def clojure_units(data: bytes, rel: str) -> list[Unit]:
    """Every Clojure unit of one file's source, with its complexity."""
    tree = _parser("clojure").parse(data)
    root = tree.root_node
    match = _NS_RE.search(data)
    namespace = match.group(1).decode("utf-8", "replace") if match else _ns_from_path(rel)
    spans = _clj_units(data, root, rel, namespace)
    if not spans:
        return []
    platform = "cljs" if rel.endswith(".cljs") else "clj"
    skipped_caps = _captures("clojure", _CLJ_SKIPPED, root)
    skips = [(n.start_byte, n.end_byte) for n in skipped_caps.get("skip", ())]
    for rc in skipped_caps.get("rc", ()):
        skips.extend(_platform_skips(data, rc, platform))
    merged = _merge(skips)
    owners = _Spans([(s, e, i) for i, (_, s, e) in enumerate(spans)])
    extra = [0] * len(spans)
    for found in _matches("clojure", _CLJ_DECISIONS, root):
        head_node, form = found["head"][0], found["form"][0]
        owner = owners.find(form.start_byte)
        if owner is None:
            continue
        if form.start_byte == spans[owner][1]:
            continue  # the unit's own head, such as a top-level (defn ...)
        if merged[0] and _inside(merged, form.start_byte):
            continue
        sym = head_node.parent
        ns_node = sym.child_by_field_name("namespace") if sym is not None else None
        if ns_node is not None and _text(data, ns_node) not in ("clojure.core", "core"):
            continue
        extra[owner] += _clj_weight(data, _text(data, head_node), form)
    return [replace(u, complexity=u.complexity + extra[i]) for i, (u, _, _) in enumerate(spans)]


def _ns_from_path(rel: str) -> str:
    stem = rel.rsplit(".", 1)[0]
    parts = stem.split("/")
    for marker in ("src", "test", "dev", "clj", "cljc", "cljs"):
        if marker in parts:
            parts = parts[len(parts) - parts[::-1].index(marker):]
            break
    return ".".join(p.replace("_", "-") for p in parts)


# ── Python ──────────────────────────────────────────────────────────────────

_PY_DECISIONS = (
    "[(if_statement) (elif_clause) (for_statement) (while_statement) (except_clause)"
    " (conditional_expression) (boolean_operator) (for_in_clause) (if_clause)] @d"
    " (case_clause) @case"
)


def _py_units(data: bytes, node, rel: str, prefix: str, out: list) -> None:
    for child in node.children:
        target = child
        if child.type == "decorated_definition":
            target = child.child_by_field_name("definition") or child
        if target.type == "function_definition":
            name_node = target.child_by_field_name("name")
            body = target.child_by_field_name("body")
            unit = Unit(
                path=rel, language="python", namespace=prefix,
                name=_text(data, name_node) if name_node is not None else "?",
                line=_line(target), end_line=_end_line(target), complexity=1,
                body_line=_line(body) if body is not None else _line(target),
            )
            out.append((unit, target.start_byte, target.end_byte))
        elif target.type == "class_definition":
            name_node = target.child_by_field_name("name")
            body = target.child_by_field_name("body")
            if body is not None and name_node is not None:
                _py_units(data, body, rel, f"{prefix}.{_text(data, name_node)}", out)


def python_units(data: bytes, rel: str, module: str) -> list[Unit]:
    """Every Python unit of one file's source, with its complexity."""
    root = _parser("python").parse(data).root_node
    spans: list = []
    _py_units(data, root, rel, module, spans)
    if not spans:
        return []
    owners = _Spans([(s, e, i) for i, (_, s, e) in enumerate(spans)])
    extra = [0] * len(spans)
    caps = _captures("python", _PY_DECISIONS, root)
    for node in caps.get("d", ()):
        owner = owners.find(node.start_byte)
        if owner is not None:
            extra[owner] += 1
    for node in caps.get("case", ()):
        pattern = next((c for c in node.children if c.type == "case_pattern"), None)
        if pattern is not None and _text(data, pattern).strip() == "_":
            continue
        owner = owners.find(node.start_byte)
        if owner is not None:
            extra[owner] += 1
    return [replace(u, complexity=u.complexity + extra[i]) for i, (u, _, _) in enumerate(spans)]


def _py_module(root: Path, rel: str) -> str:
    path = root / rel
    parts = [path.stem] if path.stem != "__init__" else []
    parent = path.parent
    while parent != root and (parent / "__init__.py").is_file():
        parts.insert(0, parent.name)
        parent = parent.parent
    return ".".join(parts) or path.stem


# ── files and cache ─────────────────────────────────────────────────────────


def _language_of(rel: str) -> str | None:
    if rel.endswith(CLOJURE_SUFFIXES):
        return "clojure"
    if rel.endswith(PYTHON_SUFFIXES):
        return "python"
    return None


def _is_test(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in TEST_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    stem = name.rsplit(".", 1)[0]
    return name.startswith("test_") or stem.endswith(("_test", "-test", "_spec"))


def _git_files(root: Path) -> list[str] | None:
    try:
        done = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return [p for p in done.stdout.decode("utf-8", "replace").split("\0") if p]


def _walk_files(root: Path) -> list[str]:
    found: list[str] = []
    stack = [root]
    while stack:
        here = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") and entry.name not in (".",):
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in SKIP_DIRS and entry.name != "graphify-out":
                    stack.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                found.append(Path(entry.path).relative_to(root).as_posix())
    return found


def source_files(root: Path, paths=(), *, include_tests: bool = False) -> list[str]:
    """Clojure and Python sources under ``root``, relative and sorted.

    ``paths`` narrows the run to files and directories, relative to ``root`` or
    absolute. Git's ignore rules apply when ``root`` is in a Git work tree.
    """
    every = _git_files(root)
    if every is None:
        every = _walk_files(root)
    picked = []
    wanted = [_relative(root, p) for p in paths]
    for rel in every:
        if _language_of(rel) is None:
            continue
        parts = rel.split("/")
        if any(p in SKIP_DIRS for p in parts[:-1]):
            continue
        if wanted and not any(rel == w or rel.startswith(w.rstrip("/") + "/") or w in ("", ".") for w in wanted):
            continue
        if not include_tests and _is_test(rel):
            continue
        if not (root / rel).is_file():
            continue
        picked.append(rel)
    return sorted(set(picked))


def _relative(root: Path, value) -> str:
    path = Path(value)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix().removeprefix("./")


def _cache_path(root: Path) -> Path:
    from graphify.paths import GRAPHIFY_OUT

    out = Path(GRAPHIFY_OUT)
    if not out.is_absolute():
        out = root / out
    return out / "cache" / "crap.json"


def _grammar_key() -> str:
    from importlib.metadata import PackageNotFoundError, version

    parts = [str(VERSION)]
    for dist in ("tree-sitter", "tree-sitter-python", "tree-sitter-clojure"):
        try:
            parts.append(version(dist))
        except PackageNotFoundError:
            parts.append("-")
    return "/".join(parts)


def _load_cache(path: Path, key: str) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("key") != key:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _save_cache(path: Path, key: str, files: dict) -> None:
    from graphify.paths import write_json_atomic

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"key": key, "files": files})
    except OSError as e:
        import sys

        print(f"[graphify crap] cache not written: {path}: {e}", file=sys.stderr)


def _units_for(root: Path, rel: str, data: bytes) -> list[Unit]:
    language = _language_of(rel)
    if language == "clojure":
        return clojure_units(data, rel)
    return python_units(data, rel, _py_module(root, rel))


PARALLEL_MIN_FILES = 48
"""Uncached files below this count parse in this process; worker startup costs more."""


def _parse_batch(root: str, batch: list[str]) -> list[tuple[str, list[dict] | None, str]]:
    """Worker entry: units of each file as dicts, or None with the error text."""
    out = []
    base = Path(root)
    for rel in batch:
        try:
            data = (base / rel).read_bytes()
            out.append((rel, [asdict(u) for u in _units_for(base, rel, data)], ""))
        except Exception as e:  # reported by the caller with the file name
            out.append((rel, None, f"{type(e).__name__}: {e}"))
    return out


def _parse_parallel(root: Path, rels: list[str]) -> dict[str, tuple[list[dict] | None, str]]:
    from concurrent.futures import ProcessPoolExecutor

    workers = max(1, min((os.cpu_count() or 2) - 1, 8, len(rels) // 16))
    batches = [rels[i::workers * 4] for i in range(workers * 4)]
    found: dict = {}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(_parse_batch, [str(root)] * len(batches), batches):
            for rel, units, error in result:
                found[rel] = (units, error)
    return found


def scan(root, paths=(), *, include_tests: bool = False, use_cache: bool = True,
         parallel: bool = False):
    """Units of every selected source file: ``(units, files, cached, errors)``.

    ``parallel`` parses many uncached files in worker processes. Only a program
    with a guarded ``__main__``, such as the ``graphify`` command, may ask for it.
    """
    root = Path(root).resolve()
    files = source_files(root, paths, include_tests=include_tests)
    key = _grammar_key()
    cache_file = _cache_path(root)
    cache = _load_cache(cache_file, key) if use_cache else {}
    fresh: dict = {}
    units: list[Unit] = []
    errors: list[str] = []
    cached = 0
    pending: list[tuple[str, list, str]] = []
    for rel in files:
        path = root / rel
        try:
            st = path.stat()
        except OSError as e:
            errors.append(f"{rel}: {e}")
            continue
        entry = cache.get(rel)
        sig = [st.st_size, st.st_mtime_ns]
        if entry and entry.get("sig") == sig:
            fresh[rel] = entry
            units.extend(Unit(**u) for u in entry["units"])
            cached += 1
            continue
        try:
            data = path.read_bytes()
        except OSError as e:
            errors.append(f"{rel}: {e}")
            continue
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()
        if entry and entry.get("hash") == digest:
            entry = {**entry, "sig": sig}
            fresh[rel] = entry
            units.extend(Unit(**u) for u in entry["units"])
            cached += 1
            continue
        pending.append((rel, sig, digest))
    parsed: dict = {}
    if parallel and len(pending) >= PARALLEL_MIN_FILES and (os.cpu_count() or 1) > 2:
        try:
            parsed = _parse_parallel(root, [rel for rel, _, _ in pending])
        except (OSError, RuntimeError) as e:  # no worker processes: parse here instead
            import sys

            print(f"[graphify crap] parallel parse unavailable, parsing serially: {e}", file=sys.stderr)
            parsed = {}
    for rel, sig, digest in pending:
        if rel in parsed:
            found, error = parsed[rel]
        else:
            found, error = _parse_batch(str(root), [rel])[0][1:]
        if found is None:
            errors.append(f"{rel}: {error}")
            continue
        fresh[rel] = {"sig": sig, "hash": digest, "units": found}
        units.extend(Unit(**u) for u in found)
    if use_cache and (fresh.keys() != cache.keys() or cached != len(files)):
        merged = {**cache, **fresh} if paths else fresh
        _save_cache(cache_file, key, merged)
    return units, len(files), cached, errors


# ── coverage ────────────────────────────────────────────────────────────────


@dataclass
class Coverage:
    """Measured coverage, indexed for joining with units."""

    reports: list[str] = field(default_factory=list)
    report_mtimes: dict = field(default_factory=dict)
    clojure: dict = field(default_factory=dict)
    """Classpath-relative file -> {form line: (hittable, hit, {child: (hittable, hit)})}."""
    clojure_prefixes: list = field(default_factory=list)
    python: dict = field(default_factory=dict)
    """Root-relative file -> (statements, executed)."""


def _suffix_keys(rel: str):
    parts = rel.split("/")
    for i in range(len(parts)):
        yield "/".join(parts[i:])


def read_clofidence(path: Path, cov: Coverage) -> None:
    data = json.loads(path.read_text("utf-8"))
    if data.get("format") != "vis-coverage/1":
        raise ValueError(f"{path}: not a vis-coverage/1 report")
    for prefix in data.get("prefixes") or ():
        cov.clojure_prefixes.append(str(prefix))
    for form in data.get("forms") or ():
        children = {int(k): (int(v[0]), int(v[1])) for k, v in (form.get("children") or {}).items()}
        per_file = cov.clojure.setdefault(str(form["file"]), {})
        per_file[int(form["line"])] = (int(form["hittable"]), int(form["hit"]), children)


def read_coverage_json(path: Path, root: Path, cov: Coverage) -> None:
    data = json.loads(path.read_text("utf-8"))
    for name, info in (data.get("files") or {}).items():
        executed = set(info.get("executed_lines") or ())
        statements = executed | set(info.get("missing_lines") or ())
        cov.python[_relative(root, name)] = (statements, executed)


def read_lcov(path: Path, root: Path, cov: Coverage) -> None:
    current = None
    statements: set = set()
    executed: set = set()
    for raw in path.read_text("utf-8", "replace").splitlines():
        if raw.startswith("SF:"):
            current, statements, executed = _relative(root, raw[3:].strip()), set(), set()
        elif raw.startswith("DA:") and current is not None:
            line, _, hits = raw[3:].partition(",")
            n = int(line)
            statements.add(n)
            if int(hits.split(",")[0] or 0) > 0:
                executed.add(n)
        elif raw.startswith("end_of_record") and current is not None:
            if current.endswith(PYTHON_SUFFIXES):
                cov.python[current] = (statements, executed)
            current = None


def load_coverage(root, reports=None) -> Coverage:
    """Read coverage reports; ``None`` finds the usual files under ``root``."""
    root = Path(root).resolve()
    cov = Coverage()
    if reports is None:
        candidates = [root / c for c in COVERAGE_CANDIDATES if (root / c).is_file()]
    else:
        candidates = [Path(r) if Path(r).is_absolute() else root / r for r in reports]
    for path in candidates:
        if not path.is_file():
            raise FileNotFoundError(f"coverage report not found: {path}")
        head = path.read_bytes()[:256]
        if path.suffix == ".json" and b"vis-coverage" in head:
            read_clofidence(path, cov)
        elif path.suffix == ".json":
            read_coverage_json(path, root, cov)
        elif head.startswith((b"TN:", b"SF:")) or path.suffix in (".info", ".lcov"):
            read_lcov(path, root, cov)
        else:
            raise ValueError(f"unknown coverage report format: {path}")
        cov.reports.append(str(path))
        cov.report_mtimes[str(path)] = path.stat().st_mtime_ns
    return cov


def _clojure_coverage(unit: Unit, cov: Coverage) -> float | None:
    forms = None
    for key in _suffix_keys(unit.path):
        forms = cov.clojure.get(key)
        if forms is not None:
            break
    if forms is None:
        measured = any(unit.namespace.startswith(p) for p in cov.clojure_prefixes)
        return 0.0 if measured and not unit.path.endswith(".cljs") else None
    form = forms.get(unit.form_line)
    if form is None:
        return None
    hittable, hit, children = form
    if unit.child >= 0:
        hittable, hit = children.get(unit.child, (0, 0))
    if hittable <= 0:
        return None
    return round(100.0 * min(hit, hittable) / hittable, 4)


def _python_coverage(unit: Unit, cov: Coverage) -> float | None:
    measured = cov.python.get(unit.path)
    if measured is None:
        for key in _suffix_keys(unit.path):
            measured = cov.python.get(key)
            if measured is not None:
                break
    if measured is None:
        return None
    statements, executed = measured
    lines = range(unit.body_line or unit.line, unit.end_line + 1)
    total = sum(1 for n in lines if n in statements)
    if total == 0:
        return None
    hit = sum(1 for n in lines if n in executed)
    return round(100.0 * hit / total, 4)


def score(unit: Unit, cov: Coverage | None) -> Entry:
    coverage = None
    if cov is not None:
        coverage = _clojure_coverage(unit, cov) if unit.language == "clojure" else _python_coverage(unit, cov)
    return Entry(unit.path, unit.language, unit.namespace, unit.name, unit.line, unit.end_line,
                 unit.complexity, coverage, crap_score(unit.complexity, coverage))


# ── changed lines ───────────────────────────────────────────────────────────

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(root: Path, base: str = "HEAD") -> dict[str, set[int]] | None:
    """Lines added or changed since ``base``, per file; untracked files map to None-lines (all)."""
    try:
        diff = subprocess.run(["git", "diff", "--no-color", "-U0", base, "--"], cwd=root,
                              capture_output=True, timeout=60, check=False)
        untracked = subprocess.run(["git", "ls-files", "-z", "--others", "--exclude-standard"],
                                   cwd=root, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if diff.returncode != 0:
        return None
    out: dict[str, set[int]] = {}
    current = None
    for raw in diff.stdout.decode("utf-8", "replace").splitlines():
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            current = name[2:] if name.startswith("b/") else None
            if current is not None:
                out.setdefault(current, set())
        elif current is not None:
            m = _HUNK.match(raw)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                out[current].update(range(start, start + count))
    for rel in untracked.stdout.decode("utf-8", "replace").split("\0"):
        if rel:
            out[rel] = {-1}
    return out


def _touches(unit: Unit, lines: set[int]) -> bool:
    if -1 in lines:
        return True
    return any(unit.line <= n <= unit.end_line for n in lines)


# ── the run ─────────────────────────────────────────────────────────────────


def analyze(
    root=".",
    paths=(),
    *,
    coverage=None,
    threshold: float = DEFAULT_THRESHOLD,
    changed: bool | str = False,
    include_tests: bool = False,
    use_cache: bool = True,
    parallel: bool = False,
) -> Report:
    """Score every Clojure and Python function under ``root``, worst first.

    ``coverage`` is ``None`` to find reports under ``root``, ``False`` for
    complexity only, or a list of report paths. ``changed`` keeps only functions
    whose lines changed since ``HEAD`` (or since the Git revision it names).
    """
    root = Path(root).resolve()
    selected = list(paths)
    diff = None
    if changed:
        diff = changed_lines(root, changed if isinstance(changed, str) else "HEAD")
        if diff is None:
            raise RuntimeError(f"{root} is not a Git work tree; --changed needs Git")
        touched = [rel for rel in diff if _language_of(rel)]
        if selected:
            wanted = [_relative(root, p) for p in selected]
            touched = [r for r in touched if any(r == w or r.startswith(w.rstrip("/") + "/") for w in wanted)]
        selected = touched
        if not selected:
            return Report(str(root), threshold, (), 0)
    units, files, cached, errors = scan(root, selected, include_tests=include_tests or bool(changed),
                                        use_cache=use_cache, parallel=parallel)
    if diff is not None:
        units = [u for u in units if _touches(u, diff.get(u.path, set()))]
    cov = None if coverage is False else load_coverage(root, coverage)
    entries = [score(u, cov) for u in units]
    entries.sort(key=lambda e: (e.crap is None, -_score(e), e.path, e.line))
    stale: list[str] = []
    if cov is not None:
        newest = max((u_mtime for u_mtime in _mtimes(root, {u.path for u in units})), default=0)
        stale = [r for r, m in cov.report_mtimes.items() if m < newest]
    return Report(str(root), float(threshold), tuple(entries), files,
                  tuple(cov.reports) if cov else (), tuple(stale), tuple(errors), cached)


def _mtimes(root: Path, rels):
    for rel in rels:
        try:
            yield (root / rel).stat().st_mtime_ns
        except OSError:
            continue


# ── output ──────────────────────────────────────────────────────────────────


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def format_table(report: Report, limit: int | None = 30) -> str:
    rows = list(report.entries)
    shown = rows if limit is None or limit <= 0 else rows[:limit]
    head = f"{'CRAP':>8} {'CC':>4} {'Cov%':>6}  Function"
    lines = [head, "-" * len(head)]
    for e in shown:
        crap = f"{e.crap:8.1f}" if e.crap is not None else f"{'-':>8}"
        cov = f"{e.coverage:6.1f}" if e.coverage is not None else f"{'-':>6}"
        lines.append(f"{crap} {e.complexity:4d} {cov}  {e.namespace}/{e.name}  {e.path}:{e.line}")
    if len(rows) > len(shown):
        lines.append(f"... {len(rows) - len(shown)} more")
    over = len(report.over)
    measured = sum(1 for e in rows if e.coverage is not None)
    lines.append("")
    lines.append(
        f"{_count(len(rows), 'function')} in {_count(report.files, 'file')} "
        f"({report.cached_files} cached), "
        f"{measured} with coverage, {over} over {report.threshold:g}"
    )
    for r in report.stale_reports:
        lines.append(f"warning: {r} is older than a scored source file; run the tests again")
    for err in report.parse_errors:
        lines.append(f"error: {err}")
    return "\n".join(lines)


def to_edn(report: Report) -> str:
    """The crap4clj snapshot shape that uml-viewer reads."""

    def s(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    def n(value) -> str:
        return "nil" if value is None else repr(float(value)) if isinstance(value, float) else str(value)

    rows = [
        f"{{:name {s(e.name)} :namespace {s(e.namespace)} :complexity {e.complexity}"
        f" :coverage {n(e.coverage)} :crap {n(e.crap)}}}"
        for e in report.entries
    ]
    return "{:entries [" + "\n           ".join(rows) + "]}\n"


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(
        prog="graphify crap",
        description="CRAP scores (complexity x missing coverage) for Clojure and Python functions.",
    )
    p.add_argument("paths", nargs="*", help="files or directories (default: the whole project)")
    p.add_argument("--root", default=".", help="project root (default: the current directory)")
    p.add_argument("--coverage", action="append", metavar="FILE",
                   help="coverage report: Clofidence JSON, coverage.py JSON or LCOV (repeatable;"
                        " default: found under the root)")
    p.add_argument("--no-coverage", action="store_true", help="complexity only")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                   help="exit 2 when a function scores above it (default 10)")
    p.add_argument("--changed", nargs="?", const="HEAD", default=None, metavar="REV",
                   help="only functions changed since REV (default HEAD), untracked files included")
    p.add_argument("--tests", action="store_true", help="also score test files")
    p.add_argument("--limit", type=int, default=30, help="rows in the table (0 = all)")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument("--edn", metavar="FILE", help="also write the crap4clj EDN snapshot")
    p.add_argument("--no-cache", action="store_true", help="parse every file again")
    args = p.parse_args(argv)
    try:
        report = analyze(
            args.root, args.paths,
            coverage=False if args.no_coverage else args.coverage,
            threshold=args.threshold, changed=args.changed or False,
            include_tests=args.tests, use_cache=not args.no_cache, parallel=True,
        )
    except (OSError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.edn:
        Path(args.edn).parent.mkdir(parents=True, exist_ok=True)
        Path(args.edn).write_text(to_edn(report), "utf-8")
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False))
    else:
        print(format_table(report, args.limit))
    return 2 if report.over else 0
