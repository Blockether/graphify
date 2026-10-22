"""Index a Clojure source tree for domain plugins.

A :class:`Corpus` reads each Clojure file once. It records the namespace, the
``:require`` aliases and refers, and every top-level definition, so that a
plugin can resolve a symbol to its definition in any file of the project.

The top-level forms and the definition index are read on first use. A plugin
that detects its framework from the ``ns`` forms alone then costs no full read
on a project that it does not apply to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from graphify.domain import edn
from graphify.domain.edn import Form

CLOJURE_SOURCE_SUFFIXES = (".clj", ".cljc", ".cljs")
SKIP_DIRS = {"node_modules", "target", "graphify-out", "out", "dist", "build", "__pycache__"}
DEF_HEADS = {"def", "defn", "defn-", "defonce", "defmacro", "defmulti", "s/def", "m/=>"}


@dataclass
class Definition:
    """One top-level definition, such as ``(defn name ...)``."""

    qualified: str
    name: str
    head: str
    form: Form
    file: "SourceFile"

    @property
    def line(self) -> int:
        return self.form.line

    @property
    def value(self) -> Form | None:
        """Return the value of a ``def``: the last item of the form."""
        items = self.form.items
        return items[-1] if self.head in ("def", "defonce") and len(items) >= 3 else None

    @property
    def body(self) -> list[Form]:
        """Return the forms after the name: arities, bodies and values."""
        return self.form.items[2:]


@dataclass
class SourceFile:
    path: Path
    rel: str
    ns: str | None
    aliases: dict[str, str] = field(default_factory=dict)
    refers: dict[str, str] = field(default_factory=dict)
    is_test: bool = False
    error: str | None = None
    _forms: list[Form] | None = field(default=None, repr=False, compare=False)
    _pending: tuple | None = field(default=None, repr=False, compare=False)

    @property
    def forms(self) -> list[Form]:
        """The top-level forms of the file, read on first use."""
        if self._forms is None:
            self._forms = []
            if self._pending is not None:
                source, nodes = self._pending
                self._pending = None
                reader = edn.Reader(source, self.ns, self.aliases)
                self._forms = [f for f in (reader.read(n) for n in nodes) if f is not None]
        return self._forms

    @forms.setter
    def forms(self, forms: list[Form]) -> None:
        self._forms, self._pending = forms, None

    def requires(self, namespace: str) -> bool:
        return namespace in self.aliases.values() or namespace in self.refers.values() or namespace in self.required

    required: set[str] = field(default_factory=set)


def _is_test_path(rel: str) -> bool:
    parts = Path(rel).parts
    stem = Path(rel).stem
    return any(p in ("test", "tests", "test-integration", "it") for p in parts[:-1]) or stem.endswith("_test")


def _parse_libspec(spec: Form, sf: SourceFile) -> None:
    if spec.kind == edn.SYMBOL:
        sf.required.add(str(spec.value))
        return
    if spec.kind != edn.VECTOR or not spec.items or spec.items[0].kind != edn.SYMBOL:
        return
    lib = str(spec.items[0].value)
    sf.required.add(lib)
    opts = spec.items[1:]
    # prefix list: [a.b [c :as x]]
    if opts and opts[0].kind in (edn.VECTOR, edn.SYMBOL) and not opts[0].is_kw():
        for sub in opts:
            if sub.kind == edn.VECTOR and sub.items and sub.items[0].kind == edn.SYMBOL:
                inner = Form(edn.VECTOR, sub.line, items=[Form(edn.SYMBOL, sub.line, f"{lib}.{sub.items[0].value}")] + sub.items[1:])
                _parse_libspec(inner, sf)
            elif sub.kind == edn.SYMBOL:
                sf.required.add(f"{lib}.{sub.value}")
        return
    for i in range(0, len(opts) - 1, 2):
        key, val = opts[i], opts[i + 1]
        if key.is_kw("as") or key.is_kw("as-alias"):
            if val.kind == edn.SYMBOL:
                sf.aliases[str(val.value)] = lib
        elif key.is_kw("refer") and val.kind == edn.VECTOR:
            for s in val.items:
                if s.kind == edn.SYMBOL:
                    sf.refers[str(s.value)] = lib


def _parse_ns(form: Form, sf: SourceFile) -> None:
    if len(form.items) > 1 and form.items[1].kind == edn.SYMBOL:
        sf.ns = str(form.items[1].value)
    for clause in form.items[2:]:
        if clause.kind == edn.LIST and clause.head is not None and clause.head.is_kw() and clause.head.name in ("require", "use"):
            for spec in clause.items[1:]:
                _parse_libspec(spec, sf)


def read_file(path: Path, root: Path) -> SourceFile:
    """Read one Clojure file into a :class:`SourceFile`."""
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = path.as_posix()
    sf = SourceFile(path=path, rel=rel, ns=None, is_test=_is_test_path(rel))
    try:
        source = path.read_bytes()
        nodes = edn.top_level_nodes(edn.parse_tree(source))
    except Exception as e:  # unreadable file or missing grammar
        sf.error = str(e)
        return sf
    reader = edn.Reader(source)
    # Read the ns form first, so that ``::alias/kw`` resolves in later forms.
    for node in nodes[:3]:
        f = reader.read(node)
        if f is not None and f.kind == edn.LIST and f.head is not None and f.head.is_sym("ns"):
            _parse_ns(f, sf)
            break
    sf._pending = (source, nodes)
    return sf


def iter_clojure_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(CLOJURE_SOURCE_SUFFIXES):
                yield Path(dirpath) / name


class Corpus:
    """All Clojure files of one project, with a definition index."""

    def __init__(self, root: Path, files: list[SourceFile]):
        self.root = root
        self.files = files
        self.by_ns: dict[str, SourceFile] = {}
        self._definitions: dict[str, Definition] | None = None
        for sf in files:
            if sf.ns:
                self.by_ns.setdefault(sf.ns, sf)

    @property
    def definitions(self) -> dict[str, Definition]:
        """Every top-level definition by qualified name, indexed on first use."""
        if self._definitions is None:
            self._definitions = {}
            for sf in self.files:
                for form in sf.forms:
                    self._index(form, sf)
        return self._definitions

    @classmethod
    def load(cls, root: str | Path, paths: Iterable[str | Path] | None = None) -> "Corpus":
        """Read the Clojure files in ``paths``, or every Clojure file under ``root``."""
        root = Path(root)
        if paths is None:
            candidates = list(iter_clojure_files(root))
        else:
            candidates = [Path(p) if Path(p).is_absolute() else root / p for p in paths]
            candidates = [p for p in candidates if str(p).endswith(CLOJURE_SOURCE_SUFFIXES)]
        return cls(root, [read_file(p, root) for p in sorted(set(candidates))])

    def _index(self, form: Form, sf: SourceFile) -> None:
        head = form.head if form.kind == edn.LIST else None
        if head is None or head.kind != edn.SYMBOL or len(form.items) < 2:
            return
        if str(head.value) not in DEF_HEADS and not str(head.value).startswith("def"):
            return
        name_form = form.items[1]
        if name_form.kind != edn.SYMBOL:
            return
        name = str(name_form.value)
        qualified = f"{sf.ns}/{name}" if sf.ns else name
        self._definitions.setdefault(qualified, Definition(qualified, name, str(head.value), form, sf))

    # -- resolution --------------------------------------------------------
    def resolve(self, symbol: Form | str, sf: SourceFile) -> str:
        """Return the fully qualified name of ``symbol`` as seen from ``sf``."""
        text = str(symbol.value) if isinstance(symbol, Form) else symbol
        if "/" in text and text != "/":
            ns, name = text.split("/", 1)
            return f"{sf.aliases.get(ns, ns)}/{name}"
        if text in sf.refers:
            return f"{sf.refers[text]}/{text}"
        if sf.ns and f"{sf.ns}/{text}" in self.definitions:
            return f"{sf.ns}/{text}"
        return text

    def definition(self, symbol: Form | str, sf: SourceFile) -> Definition | None:
        return self.definitions.get(self.resolve(symbol, sf))

    def call_target(self, form: Form, sf: SourceFile) -> str | None:
        """Return the qualified name of the function that a list form calls."""
        head = form.head if form.kind == edn.LIST else None
        if head is None or head.kind != edn.SYMBOL:
            return None
        return self.resolve(head, sf)

    def calls(self, sf: SourceFile, targets: set[str]) -> Iterator[tuple[str, Form]]:
        """Yield every call in ``sf`` to one of the qualified ``targets``."""
        for top in sf.forms:
            for form in top.walk():
                target = self.call_target(form, sf)
                if target in targets:
                    yield target, form

    def enclosing_definition(self, sf: SourceFile, line: int) -> Definition | None:
        best = None
        for d in self.definitions.values():
            if d.file is sf and d.line <= line and (best is None or d.line > best.line):
                best = d
        return best
