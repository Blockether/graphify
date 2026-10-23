"""Move definitions and files in languages that name a module in each file.

Java, Kotlin, Scala and Groovy declare a package, C# and PHP a namespace, Elixir and
Fortran a module, and Swift uses the folder of a SwiftPM target. A move changes the
module of the moved code. Each qualified use, each import and each use that saw the
name through its own module or a wildcard import follows the code. The moved code
gets the imports that it uses, also for names of its old module.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from graphify import move_kit as kit
from graphify.moves import MovePlan, project_files, resolve_path
from graphify.refactor import Unsupported, symbol_name


@dataclass
class Decl:
    name: str
    node: object
    start: int
    end: int
    module: str = ""  # the module that holds it, for languages with modules in the file
    public: bool = True


@dataclass
class Imp:
    start: int
    end: int
    module: str
    names: list = field(default_factory=list)  # [(name, alias)]; empty for a wildcard
    wildcard: bool = False
    group: bool = False  # a list of names that a split can shorten
    text: str = ""


class _Hooks:
    module_word = "package"
    file_indent = ""
    qualifies_siblings = False

    def aliases(self, view, module):
        return []

    _HEADERS = frozenset({"package_declaration", "package_header", "package_clause", "using_directive",
                          "namespace_use_declaration", "import_declaration", "import"})
    _NAMESPACES = frozenset({"namespace_declaration", "file_scoped_namespace_declaration", "namespace_definition"})

    def in_header(self, view, tok):
        n = tok.parent
        while n is not None:
            if n.type in self._HEADERS:
                return True
            if n.type in self._NAMESPACES:
                name = n.child_by_field_name("name")
                return name is not None and name.start_byte <= tok.start_byte < name.end_byte
            n = n.parent
        return False

    def prepare(self, index, files):
        pass

    def new_indent(self, like: bytes) -> str:
        return self.file_indent

    def globally_seen(self, module):
        return False

    def requalify(self, view, q, module, new, need):
        return q.replace(module, new, 1)

    def retarget(self, text, module, new, name):
        return text.replace(f"{module}{self.sep}{name}", f"{new}{self.sep}{name}", 1)

    def rewrite_bare(self, view, bares, name, module, new, edits, need):
        pass

    def rewrite_body(self, block, job, index, own):
        return block

    def import_indent(self, view, at):
        return ""

    def check_move(self, view, job, conflicts):
        pass

    def outer_modules(self, module):
        return []

    def publicize(self, draft, entries, notes):
        changed = False
        for f, d in entries:
            data = draft.get(f)
            block = data[d.start:d.end].decode()
            public = self.make_public(block)
            if public != block:
                draft.put(f, data[:d.start] + public.encode() + data[d.end:])
                notes.append(f"Made `{d.name}` public in {f.name}: the moved code uses it.")
                changed = True
        return changed

    def node_name(self, node):
        return _name(node)

    def open_members(self, block, names):
        """Make the members in ``names`` visible to another module. Java needs it; others keep ``block``."""
        return block

    def companions(self, tree, data, path, root, decls):
        return decls

    def keeps(self, data):
        """True when a file without declarations still holds code, such as a script."""
        return False

    def target_module(self, tree, data, path, root):
        return self.module_at(tree, data, path, root) or ""


class Lang(_Hooks):
    name = ""
    suffixes: tuple = ()
    sep = "."
    decl_types: frozenset = frozenset()
    import_types: frozenset = frozenset()
    removable_same_module_import = True
    wildcard_only = False  # a name import is not possible: add a wildcard (using M;)
    acyclic = False  # modules cannot import each other in a cycle (Swift targets)
    public_word = ""

    # --- module of a place
    def module_at(self, tree, data: bytes, path: Path, root: Path, offset: int = 0) -> str | None:
        return None

    def modules_in(self, tree, data: bytes, path: Path, root: Path) -> list[str]:
        m = self.module_at(tree, data, path, root)
        return [m] if m is not None else []

    def sees(self, scope: str | None, module: str) -> bool:
        """True when code in ``scope`` sees the names of ``module`` without an import."""
        return scope == module

    # --- declarations and imports
    def decl_name(self, node, data: bytes) -> str | None:
        n = node.child_by_field_name("name")
        return kit.text(n, data) if n is not None else None

    def top(self, tree, data: bytes):
        return [c for c in tree.root_node.children if c.type in self.decl_types]

    def decls(self, tree, data: bytes, path: Path, root: Path) -> list[Decl]:
        out = []
        for node in self.top(tree, data):
            name = self.decl_name(node, data)
            if name:
                start, end = kit.span(node, data, self.attribute)
                out.append(Decl(name, node, start, end, self.module_at(tree, data, path, root, node.start_byte) or "",
                                self.is_public(node, data)))
        return out

    def attribute(self, node) -> bool:
        return False

    def is_public(self, node, data: bytes) -> bool:
        return True

    def make_public(self, block: str) -> str:
        return block

    def imports(self, tree, data: bytes) -> list[Imp]:
        out = []
        for n in kit.walk(tree.root_node):
            if n.type in self.import_types:
                imp = self.read_import(kit.text(n, data))
                if imp:
                    s, e = kit.line_start(data, n.start_byte), kit.line_end(data, n.end_byte - 1)
                    out.append(Imp(s, e, imp.module, imp.names, imp.wildcard, imp.group, kit.text(n, data)))
        return out

    def read_import(self, text: str) -> Imp | None:
        return None

    def import_line(self, module: str, name: str) -> str:
        raise NotImplementedError

    def wildcard_line(self, module: str) -> str:
        raise NotImplementedError

    def group_line(self, imp: Imp, keep: list) -> str:
        raise NotImplementedError

    def import_point(self, tree, data: bytes, offset: int = 0) -> int:
        """Where a new import line goes: after the last import, else after the module line."""
        imps = self.imports(tree, data)
        if imps:
            return max(i.end for i in imps)
        head = self.header_end(tree, data)
        return head

    def header_end(self, tree, data: bytes) -> int:
        return 0

    # --- qualified names
    def qualifiers(self, module: str) -> list[str]:
        return [module + self.sep] if module else []

    # --- files
    def new_file(self, module: str, imports: list[str], body: str, like: bytes) -> str:
        raise NotImplementedError

    def body_point(self, tree, data: bytes, module: str) -> tuple[int | None, str]:
        """Where moved code goes in an existing file (None = the end) and its indent."""
        return None, ""

    def derive(self, root: Path, source: Path, module: str, target: Path, draft) -> str | None:
        return analogy(root, source, module, target, self.sep)

    def set_module(self, tree, data: bytes, old: str, new: str) -> bytes:
        return data


def _split(module: str, sep: str) -> list[str]:
    return [p for p in module.split(sep) if p]


def analogy(root: Path, source: Path, module: str, target: Path, sep: str, conv=lambda s: s, stem: bool = False):
    """The module of ``target`` that follows the folder layout of ``source`` and ``module``."""
    sd = list(Path(kit.rel(root, source)).parent.parts) + ([source.stem] if stem else [])
    td = list(Path(kit.rel(root, target)).parent.parts) + ([target.stem] if stem else [])
    parts = _split(module, sep)
    if sd == td:
        return module
    k = 0
    while k < min(len(sd), len(parts)) and conv(sd[-1 - k]) == parts[-1 - k]:
        k += 1
    if k == 0 and parts:
        return None
    base, prefix = sd[: len(sd) - k], parts[: len(parts) - k]
    if td[: len(base)] != base:
        return None
    rest = [conv(p) for p in td[len(base):]]
    if not all(re.fullmatch(r"[A-Za-z_][\w]*", p) for p in rest):
        return None
    return sep.join(prefix + rest)


def _line(data: bytes, i: int) -> int:
    return data.count(b"\n", 0, i) + 1


class Java(Lang):
    name = "java"
    suffixes = (".java",)
    decl_types = frozenset({"class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
                            "annotation_type_declaration"})
    import_types = frozenset({"import_declaration"})
    package_type = "package_declaration"
    semi = ";"
    _IMPORT = re.compile(r"import\s+(static\s+)?([\w.]+?)(\.\*)?\s*;?\s*$", re.S)
    _KIND = r"(class|interface|enum|record|@interface)"

    def module_at(self, tree, data, path, root, offset=0):
        for c in tree.root_node.children:
            if c.type == self.package_type:
                return re.sub(r"^package\s+|[;\s]+$", "", kit.text(c, data)).strip()
        return ""

    def header_end(self, tree, data):
        for c in tree.root_node.children:
            if c.type == self.package_type:
                return kit.line_end(data, c.end_byte - 1)
        return 0

    def is_public(self, node, data):
        mods = next((c for c in node.children if c.type == "modifiers"), None)
        return mods is not None and re.search(r"\bpublic\b", kit.text(mods, data)) is not None

    def make_public(self, block):
        return re.sub(r"(?m)^(\s*(?:@\w+(?:\([^)]*\))?\s+)*)((?:(?:abstract|final|sealed|non-sealed|static|strictfp)\s+)*" + self._KIND + r"\b)",
                      r"\1public \2", block, count=1)

    _MEMBERS = frozenset({"method_declaration", "field_declaration", "constructor_declaration", "class_declaration",
                          "interface_declaration", "enum_declaration", "record_declaration"})

    def open_members(self, block, names):
        if self.name != "java":
            return block
        data = block.encode()
        tree = kit.parse(Path("x.java"), data)
        edits = []
        for top in tree.root_node.children:
            body = top.child_by_field_name("body")
            for m in body.children if body is not None else ():
                if m.type not in self._MEMBERS:
                    continue
                if m.type == "field_declaration":
                    decl = m.child_by_field_name("declarator")
                    name = kit.text(decl.child_by_field_name("name"), data) if decl is not None else ""
                elif m.type == "constructor_declaration":
                    name = "<init>"
                else:
                    n = m.child_by_field_name("name")
                    name = kit.text(n, data) if n is not None else ""
                if name not in names:
                    continue
                mods = next((c for c in m.children if c.type == "modifiers"), None)
                words = kit.text(mods, data) if mods is not None else ""
                if re.search(r"\b(public|private|protected)\b", words):
                    continue
                at = m.start_byte
                if mods is not None:
                    plain = [c for c in mods.children if "annotation" not in c.type]
                    at = plain[0].start_byte if plain else mods.end_byte + 1
                edits.append((at, at, "public "))
        from graphify.moves import replace_ranges

        return replace_ranges(data, edits).decode() if edits else block

    def read_import(self, text):
        m = self._IMPORT.match(text.strip())
        if not m:
            return None
        path = m.group(2)
        if m.group(3):
            return Imp(0, 0, path, [], True)
        module, _, name = path.rpartition(".")
        return Imp(0, 0, module, [(name, None)])

    def import_line(self, module, name, alias=None):
        return f"import {module}.{name}{self.semi}"

    def wildcard_line(self, module):
        return f"import {module}.*{self.semi}"

    def new_file(self, module, imports, body, like):
        head = f"package {module}{self.semi}\n\n" if module else ""
        imps = ("\n".join(imports) + "\n\n") if imports else ""
        return head + imps + body.strip("\n") + "\n"

    def set_module(self, tree, data, old, new):
        for c in tree.root_node.children:
            if c.type == self.package_type:
                return data[:c.start_byte] + f"package {new}{self.semi}".encode() + data[c.end_byte:]
        return f"package {new}{self.semi}\n\n".encode() + data if new else data


class Groovy(Java):
    name = "groovy"
    suffixes = (".groovy",)
    decl_types = frozenset({"class_declaration", "interface_declaration", "enum_declaration"})
    semi = ""

    def is_public(self, node, data):
        mods = next((c for c in node.children if c.type == "modifiers"), None)
        return mods is None or not re.search(r"\b(private|protected)\b", kit.text(mods, data))

    def make_public(self, block):
        return block


class Kotlin(Java):
    name = "kotlin"
    suffixes = (".kt", ".kts")
    decl_types = frozenset({"class_declaration", "object_declaration", "function_declaration", "property_declaration",
                            "type_alias"})
    import_types = frozenset({"import"})
    package_type = "package_header"
    semi = ""
    _IMPORT = re.compile(r"import\s+([\w.`]+?)(\.\*)?(?:\s+as\s+(\w+))?\s*;?\s*$", re.S)

    def decl_name(self, node, data):
        n = node.child_by_field_name("name")
        if n is None and node.type == "property_declaration":
            v = next((c for c in node.children if c.type == "variable_declaration"), None)
            n = next((c for c in v.children if c.type in ("identifier", "simple_identifier")), None) if v else None
        if n is None:
            n = next((c for c in node.children if c.type in ("identifier", "simple_identifier", "type_identifier")), None)
        return kit.text(n, data) if n is not None else None

    def is_public(self, node, data):
        mods = next((c for c in node.children if c.type == "modifiers"), None)
        return mods is None or not re.search(r"\bprivate\b", kit.text(mods, data))

    def make_public(self, block):
        return block

    def import_line(self, module, name, alias=None):
        return f"import {module}.{name}" + (f" as {alias}" if alias else "")

    def read_import(self, text):
        m = self._IMPORT.match(text.strip())
        if not m:
            return None
        if m.group(2):
            return Imp(0, 0, m.group(1), [], True)
        module, _, name = m.group(1).rpartition(".")
        return Imp(0, 0, module, [(name, m.group(3))])

    def module_at(self, tree, data, path, root, offset=0):
        for c in tree.root_node.children:
            if c.type == self.package_type:
                return re.sub(r"^package\s+|[;\s]+$", "", kit.text(c, data)).strip()
        return ""


class Scala(Kotlin):
    name = "scala"
    suffixes = (".scala", ".sc")
    decl_types = frozenset({"class_definition", "object_definition", "trait_definition", "function_definition",
                            "val_definition", "var_definition", "type_definition", "enum_definition"})
    import_types = frozenset({"import_declaration"})
    package_type = "package_clause"
    _IMPORT = re.compile(r"import\s+([\w.]+?)\.(\{[^}]*\}|_|\*|\w+)\s*$", re.S)

    def decl_name(self, node, data):
        n = node.child_by_field_name("name") or node.child_by_field_name("pattern")
        return kit.text(n, data) if n is not None else None

    def read_import(self, text):
        m = self._IMPORT.match(text.strip())
        if not m:
            return None
        module, last = m.group(1), m.group(2)
        if last in ("_", "*"):
            return Imp(0, 0, module, [], True)
        if last.startswith("{"):
            names, wild = [], False
            for part in last.strip("{}").split(","):
                part = part.strip()
                if part in ("_", "*"):
                    wild = True
                    continue
                k = re.split(r"\s*(?:=>|\bas\b)\s*", part)
                names.append((k[0], k[1] if len(k) > 1 else None))
            return Imp(0, 0, module, names, wild, True)
        return Imp(0, 0, module, [(last, None)])

    def group_line(self, imp, keep):
        parts = [n if not a else f"{n} => {a}" for n, a in keep]
        if imp.wildcard:
            parts.append("_")
        if len(parts) == 1 and " " not in parts[0]:
            return f"import {imp.module}.{parts[0]}"
        return f"import {imp.module}.{{{', '.join(parts)}}}"

    def wildcard_line(self, module):
        return f"import {module}._"

    def import_line(self, module, name, alias=None):
        return f"import {module}.{{{name} => {alias}}}" if alias else f"import {module}.{name}"


LANGS: dict[str, Lang] = {}


def register(*langs):
    for lang in langs:
        for suffix in lang.suffixes:
            LANGS[suffix] = lang


def lang_of(path: Path) -> Lang:
    lang = LANGS.get(Path(path).suffix.lower())
    if lang is None:
        raise Unsupported(f"No named-module move for {Path(path).suffix} files.")
    return lang


@dataclass
class Job:
    source: Path
    target: Path
    names: list  # moved top-level names
    module: str  # old module
    new_module: str
    decls: list | None = None  # None: the whole file moves


class _View:
    """A parsed file of the draft."""

    def __init__(self, lang: Lang, path: Path, data: bytes, root: Path):
        self.lang, self.path, self.data, self.root = lang, path, data, root
        self.tree = kit.parse(path, data)
        self.imports = lang.imports(self.tree, data)

    def scope(self, offset: int = 0):
        return self.lang.module_at(self.tree, self.data, self.path, self.root, offset)

    def in_import(self, offset: int) -> Imp | None:
        return next((i for i in self.imports if i.start <= offset < i.end), None)


def _qualifier(view: _View, tok, spellings) -> str | None:
    s = tok.start_byte
    for q in sorted(spellings, key=len, reverse=True):
        b = q.encode()
        if view.data[s - len(b):s] == b and not kit.is_word(view.data, s - len(b) - 1) and \
                view.data[s - len(b) - 1:s - len(b)] not in (b".", b"\\"):
            return q
    return None


def _member(view: _View, tok) -> bool:
    before = view.data[max(0, tok.start_byte - 2):tok.start_byte]
    return before.endswith((b".", b"\\", b"::", b"->", b"?.")) or before.endswith(b":") and view.lang.name == "elixir"


def _imports_name(imp: Imp, name: str) -> bool:
    return any(n == name or a == name for n, a in imp.names)


def _uses(view: _View, name: str, module: str):
    """Sites of ``name`` of ``module`` in a file: (qualified, bare, imports, shadowed)."""
    lang = view.lang
    quals, bares = [], []
    named = [i for i in view.imports if i.module == module and _imports_name(i, name)]
    other = [i for i in view.imports if i.module != module and _imports_name(i, name)]
    wild = any(i.wildcard and i.module == module for i in view.imports) or lang.globally_seen(module)
    spellings = lang.qualifiers(module) + lang.aliases(view, module)
    for tok in kit.tokens(view.tree.root_node, view.data, [name]):
        if view.in_import(tok.start_byte) or lang.in_header(view, tok):
            continue
        q = _qualifier(view, tok, spellings)
        if q is not None:
            quals.append((tok, q))
        elif not _member(view, tok):
            bares.append(tok)
    seen = []
    for tok in bares:
        scope = view.scope(tok.start_byte)
        if other and not named:
            continue
        if named or wild or lang.sees(scope, module):
            seen.append(tok)
    return quals, seen, named


def _add_imports(view: _View, lines: list[str], offset: int = 0) -> bytes:
    have = {re.sub(r"\s+", " ", i.text.strip()) for i in view.imports}
    new = [ln for ln in dict.fromkeys(lines) if re.sub(r"\s+", " ", ln.strip()) not in have]
    if not new:
        return view.data
    at = view.lang.import_point(view.tree, view.data, offset)
    indent = view.lang.import_indent(view, at)
    return kit.insert_lines(view.data, at, [indent + ln for ln in new],
                            blank_before=not view.imports and at > 0, blank_after=not view.imports)


def _rewrite(lang: Lang, draft, root: Path, path: Path, moved: list, extra: list[str], local: set[str]) -> list[str]:
    """Rewrite one file for every moved name; ``extra`` are imports it needs. Return notes."""
    data = draft.get(path)
    view = _View(lang, path, data, root)
    edits, need, first = [], list(extra), None
    for name, module, new in moved:
        if module == new:
            continue
        if name.encode() not in data:
            continue
        quals, bares, named = _uses(view, name, module)
        for tok, q in quals:
            edits.append((tok.start_byte - len(q.encode()), tok.start_byte, lang.requalify(view, q, module, new, need)))
        here = view.scope(bares[0].start_byte if bares else 0)
        for imp in named:
            if lang.sees(here, new) and lang.removable_same_module_import or name in local:
                keep = [(n, a) for n, a in imp.names if n != name]
                edits.append((imp.start, imp.end, (lang.group_line(imp, keep) + "\n") if keep or imp.wildcard else ""))
            elif imp.group:
                keep = [(n, a) for n, a in imp.names if n != name]
                alias = next((a for n, a in imp.names if n == name), None)
                edits.append((imp.start, imp.end, (lang.group_line(imp, keep) + "\n") if keep or imp.wildcard else ""))
                need.append(lang.import_line(new, name, alias))
            else:
                edits.append((imp.start, imp.end, lang.retarget(data[imp.start:imp.end].decode(), module, new, name)))
        for imp in view.imports:  # static imports of members of a moved type: import static a.b.N.m
            if imp.module == f"{module}{lang.sep}{name}" or imp.module.startswith(f"{module}{lang.sep}{name}{lang.sep}"):
                edits.append((imp.start, imp.end, lang.retarget(data[imp.start:imp.end].decode(), module, new, name)))
        if bares and not named and name not in local:
            scope = view.scope(bares[0].start_byte)
            wild_new = any(i.wildcard and i.module == new for i in view.imports)
            has_new = any(i.module == new and _imports_name(i, name) for i in view.imports)
            if not (lang.sees(scope, new) or wild_new or has_new):
                need.append(lang.wildcard_line(new) if lang.wildcard_only else lang.import_line(new, name))
                first = bares[0].start_byte if first is None else first
        if bares and name not in local:
            lang.rewrite_bare(view, bares, name, module, new, edits, need)
    if edits:
        from graphify.moves import replace_ranges

        data = replace_ranges(data, sorted(set(edits)))
        view = _View(lang, path, data, root)
    if need:
        if first is not None and edits:
            first = None
        data = _add_imports(view, need, first or 0)
    draft.put(path, kit.tidy(data) if data != view.data or edits else data)
    return []


def _drop_unused_imports(lang: Lang, path: Path, rest: bytes, moved: str) -> bytes:
    """Remove the name imports of ``rest`` that only the moved code used."""
    view = _View(lang, path, rest, path.parent)
    body = set(re.findall(r"[A-Za-z_$][\w$]*", moved))
    edits = []
    for imp in view.imports:
        if imp.wildcard or not imp.names:
            continue
        gone = [(n, a) for n, a in imp.names if (a or n) in body and not _used_outside_imports(view, a or n)]
        if not gone:
            continue
        keep = [x for x in imp.names if x not in gone]
        edits.append((imp.start, imp.end, (lang.group_line(imp, keep) + "\n") if keep else ""))
    if not edits:
        return rest
    from graphify.moves import replace_ranges

    return kit.tidy(replace_ranges(rest, edits))


def _used_outside_imports(view: _View, name: str) -> bool:
    return any(not view.in_import(t.start_byte) for t in kit.tokens(view.tree.root_node, view.data, [name]))


def _redecl(lang: Lang, draft, root: Path, path: Path, name: str):
    """The current declaration of ``name`` in the draft of ``path``."""
    data = draft.get(path)
    found = [d for d in lang.decls(kit.parse(path, data), data, path, root) if d.name == name]
    return path, (found[0] if found else None)


def _index(lang: Lang, root: Path, files) -> dict:
    """module -> name -> [(file, Decl)] of every top-level declaration."""
    out: dict = {}
    for f in files:
        try:
            data = f.read_bytes()
            tree = kit.parse(f, data)
        except (OSError, ValueError):
            continue
        for d in lang.decls(tree, data, f, root):
            out.setdefault(d.module, {}).setdefault(d.name, []).append((f, d))
    return out


def _source_imports(lang: Lang, view: _View, words: set[str], new_module: str) -> list[str]:
    out = []
    for imp in view.imports:
        if imp.module == new_module:
            continue
        if imp.wildcard and not imp.names:
            out.append(lang.wildcard_line(imp.module))
        for n, a in imp.names:
            if (a or n) in words:
                out.append(lang.import_line(imp.module, n, a))
    return out


def _plan(lang: Lang, graph: dict, root: Path, jobs: list[Job], subject: str, renames=(), conflicts=None, notes=None):
    conflicts, notes = list(conflicts or []), list(notes or [])
    files = [f for f in project_files(root, graph, lang.suffixes)]
    index = _index(lang, root, files)
    lang.prepare(index, files)
    draft = kit.Draft()
    moved = [(n, j.module, j.new_module) for j in jobs for n in j.names]
    sources = {j.source for j in jobs}
    for j in jobs:
        if j.new_module is None:
            conflicts.append(f"Cannot tell the {lang.module_word} of {kit.rel(root, j.target)}. Move to a file that declares it.")
            continue
        for n in j.names:
            for f, _ in index.get(j.new_module, {}).get(n, []):
                if f not in sources:
                    conflicts.append(f"{kit.rel(root, f)} already declares `{n}` in {lang.module_word} {j.new_module or '(default)'}.")
    if conflicts:
        return MovePlan(subject, str(jobs[0].source), str(jobs[0].target), {}, tuple(dict.fromkeys(conflicts)), tuple(notes), (), tuple(renames))
    extra: dict[Path, list[str]] = {}
    local: dict[Path, set[str]] = {}
    for j in jobs:
        src = draft.get(j.source)
        view = _View(lang, j.source, src, root)
        lang.check_move(view, j, conflicts)
        if j.decls is None:
            words = kit.words(view.tree.root_node, src)
            own = set(j.names)
            new = lang.set_module(view.tree, src, j.module, j.new_module) if j.module != j.new_module else src
            if j.module != j.new_module:
                movers = {k.source for k in jobs}
                users = [f for f in files if f not in movers and any(n.encode() in f.read_bytes() for n in j.names)]
                used = set().union(*(set(re.findall(r"(?<=\.)\s*(\w+)", f.read_text(errors="replace"))) for f in users)) | {"<init>"}
                tree2 = kit.parse(j.target, new)
                edits = []
                for d in lang.decls(tree2, new, j.target, root):
                    if not any(d.name.encode() in f.read_bytes() for f in users):
                        continue
                    text = new[d.start:d.end].decode()
                    opened = lang.open_members(text if d.public else lang.make_public(text), used)
                    if opened != text:
                        edits.append((d.start, d.end, opened))
                        notes.append(f"Made `{d.name}` and the members that other code uses public.")
                if edits:
                    from graphify.moves import replace_ranges

                    new = replace_ranges(new, edits)
            draft.remove(j.source)
            draft.put(j.target, new)
        else:
            words = set().union(*(kit.words(d.node, src) for d in j.decls))
            own = set(j.names)
            spans = sorted((d.start, d.end) for d in j.decls)
            block = "\n\n".join(kit.piece(src, s, e) for s, e in spans)
            rest = kit.cut(src, spans)
            outside = any(n.encode() in rest for n in j.names) or any(
                f not in (j.source, j.target) and any(n.encode() in f.read_bytes() for n in j.names) for f in files)
            if j.module != j.new_module and outside and not all(d.public for d in j.decls):
                public = lang.make_public(block)
                if public == block and lang.public_word:
                    conflicts.append(f"`{subject}` is not visible outside {lang.module_word} {j.module}, and other code uses it.")
                elif public != block:
                    block = public
                    notes.append(f"Made `{subject}` {lang.public_word or 'public'}: code outside {j.new_module} uses it.")
            if j.module != j.new_module:
                used = kit.words(kit.parse(j.source, rest).root_node, rest)
                for f in files:
                    if f not in (j.source, j.target) and any(n.encode() in f.read_bytes() for n in j.names):
                        used |= set(re.findall(r"(?<=\.)\s*(\w+)", f.read_text(errors="replace")))
                        used.add("<init>")
                opened = lang.open_members(block, used)
                if opened != block:
                    block = opened
                    notes.append(f"Made members of `{subject}` public: code in {lang.module_word} {j.module} uses them.")
            block = lang.rewrite_body(block, j, index, own)
            rest = _drop_unused_imports(lang, j.source, rest, block)
            draft.put(j.source, rest)
            old_indent = kit.indent_of(src, spans[0][0])
            if draft.exists(j.target):
                t = draft.get(j.target)
                at, indent = lang.body_point(kit.parse(j.target, t), t, j.new_module)
                draft.put(j.target, kit.append_block(t, kit.reindent(block, old_indent, indent), at))
            else:
                body = kit.reindent(block, old_indent, lang.new_indent(src))
                draft.put(j.target, lang.new_file(j.new_module, [], body, src).encode())
            extra.setdefault(j.target, []).extend(_source_imports(lang, view, words, j.new_module))
        local.setdefault(j.target, set()).update(own)
        if j.module != j.new_module and not lang.qualifies_siblings:
            mods = [j.module] + lang.outer_modules(j.module)
            for mod in mods:
                if lang.sees(j.new_module, mod):
                    continue
                for s in sorted((set(index.get(mod, {})) - own - {n for k in jobs for n in k.names}) & words):
                    entries = index[mod][s]
                    dotted = set(re.findall(r"(?<=\.)\s*(\w+)", block if j.decls is not None else src.decode(errors="replace")))
                    dotted |= {"<init>"} if re.search(rf"\bnew\s+{re.escape(s)}\b", block if j.decls is not None else src.decode(errors="replace")) else set()
                    for f, e in [_redecl(lang, draft, root, f, s) for f, _ in entries]:
                        if e is None:
                            continue
                        data = draft.get(f)
                        text = data[e.start:e.end].decode()
                        opened = lang.open_members(text, dotted)
                        if opened != text:
                            draft.put(f, data[:e.start] + opened.encode() + data[e.end:])
                            notes.append(f"Made members of `{s}` public in {f.name}: the moved code uses them.")
                    entries = [(f, e) for f, e in (_redecl(lang, draft, root, f, s) for f, _ in entries) if e is not None]
                    if not any(e.public for _, e in entries):
                        if not lang.publicize(draft, entries, notes):
                            conflicts.append(f"`{subject}` uses `{s}`, which is not visible outside {lang.module_word} {mod}.")
                    extra.setdefault(j.target, []).append(lang.wildcard_line(mod) if lang.wildcard_only else lang.import_line(mod, s))
    if conflicts:
        return MovePlan(subject, str(jobs[0].source), str(jobs[0].target), {}, tuple(dict.fromkeys(conflicts)), tuple(notes), (), tuple(renames))
    for j in jobs:
        if j.decls is not None and draft.exists(j.source):
            rest = draft.get(j.source)
            if not lang.decls(kit.parse(j.source, rest), rest, j.source, root) and not lang.keeps(rest):
                draft.remove(j.source)
                notes.append(f"Deleted {kit.rel(root, j.source)}: no code stays in it.")
    targets = {j.target for j in jobs}
    for f in sorted(set(files) | targets):
        if draft.exists(f):
            _rewrite(lang, draft, root, f, moved, extra.get(f, []), local.get(f, set()))
    cycle = _cycle(lang, draft, root, sorted(set(files) | targets), jobs) if lang.acyclic else None
    if cycle:
        conflicts.append(f"The move makes {lang.module_word}s {' -> '.join(cycle)} import each other in a cycle.")
        return MovePlan(subject, str(jobs[0].source), str(jobs[0].target), {}, tuple(conflicts), tuple(notes), (), tuple(renames))
    return MovePlan(subject, str(jobs[0].source), str(jobs[0].target), draft.final(), (), tuple(dict.fromkeys(notes)), (), tuple(renames))


def _cycle(lang, draft, root, files, jobs):
    """Return a module cycle through a moved module after the move, or None."""
    deps: dict[str, set[str]] = {}
    for f in files:
        if not draft.exists(f):
            continue
        data = draft.get(f)
        tree = kit.parse(f, data)
        mod = lang.module_at(tree, data, f, root)
        if mod:
            deps.setdefault(mod, set()).update(i.module for i in lang.imports(tree, data) if i.module and i.module != mod)
    for j in jobs:
        if j.module == j.new_module:
            continue
        stack = [(j.new_module, [j.new_module])]
        seen = set()
        while stack:
            mod, path = stack.pop()
            for nxt in deps.get(mod, ()):
                if nxt == j.new_module:
                    return path + [nxt]
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, path + [nxt]))
    return None


def _name(node: dict) -> str:
    name = symbol_name(str(node.get("label", "")))
    name = re.sub(r"/\d+$", "", name)
    return re.split(r"[.\\#:/]", name)[-1] if not name.startswith("$") else name


def plan_move(graph: dict, node: dict, source: Path, to: str, root: Path) -> MovePlan:
    lang = lang_of(source)
    name = lang.node_name(node)
    data = source.read_bytes()
    tree = kit.parse(source, data)
    decls = [d for d in lang.decls(tree, data, source, root) if d.name == name]
    if not decls:
        raise Unsupported(f"`{name}` is not a top-level definition of {kit.rel(root, source)}.")
    decls = lang.companions(tree, data, source, root, decls)
    target = resolve_path(root, to)
    if target.suffix.lower() not in lang.suffixes:
        raise Unsupported(f"Move {lang.name} code to a {'/'.join(lang.suffixes)} file, not {to}.")
    if target == source:
        raise Unsupported(f"`{name}` is already in {kit.rel(root, source)}.")
    module = decls[0].module
    if target.is_file():
        t = target.read_bytes()
        tt = kit.parse(target, t)
        new_module = lang.target_module(tt, t, target, root)
        if any(d.name == name for d in lang.decls(tt, t, target, root)):
            return MovePlan(name, str(source), str(target), {}, (f"{kit.rel(root, target)} already declares `{name}`.",))
    else:
        new_module = lang.derive(root, source, module, target, None)
    job = Job(source, target, [name], module, new_module, decls)
    return _plan(lang, graph, root, [job], name)


def plan_move_file(graph: dict, old: Path, new: str, root: Path, tests: bool = True) -> MovePlan:
    lang = lang_of(old)
    target = resolve_path(root, new)
    if target.suffix.lower() not in lang.suffixes:
        raise Unsupported(f"Keep the {old.suffix} suffix: {new}.")
    if target.exists():
        return MovePlan(kit.rel(root, old), str(old), str(target), {}, (f"{kit.rel(root, target)} already exists.",))
    pairs = [(old, target)]
    if tests:
        pairs += [(t, n) for t, n, *_ in kit.test_pairs(root, old, target) if not n.exists()]
    jobs, renames, notes = [], [], []
    for o, n in pairs:
        data = o.read_bytes()
        tree = kit.parse(o, data)
        decls = lang.decls(tree, data, o, root)
        module = lang.module_at(tree, data, o, root)
        derived = lang.derive(root, o, module or "", n, None)
        if derived is None:
            derived = module or ""
            if o.parent != n.parent:
                notes.append(f"{lang.module_word.capitalize()} {module or '(default)'} of {kit.rel(root, o)} stays: it does not follow the folders.")
        jobs.append(Job(o, n, sorted({d.name for d in decls}), module or "", derived))
        if o != old:
            renames.append((kit.rel(root, o), kit.rel(root, n)))
        if (module or "") != derived:
            renames.append((module or "(default)", derived))
    return _plan(lang, graph, root, jobs, kit.rel(root, old), tuple(dict.fromkeys(renames)), notes=notes)


class CSharp(Lang):
    name = "csharp"
    suffixes = (".cs",)
    module_word = "namespace"
    wildcard_only = True
    decl_types = frozenset({"class_declaration", "struct_declaration", "interface_declaration", "enum_declaration",
                            "record_declaration", "record_struct_declaration", "delegate_declaration"})
    import_types = frozenset({"using_directive"})
    _USING = re.compile(r"(global\s+)?using\s+(static\s+)?(?:(\w+)\s*=\s*)?([\w.:]+)\s*;", re.S)

    def __init__(self):
        self.globals: set[str] = set()

    def _namespaces(self, tree, data):
        """(start, end, name) of each namespace, nested names joined."""
        out = []

        def rec(node, prefix, end_default):
            for i, c in enumerate(node.children):
                if c.type in ("namespace_declaration", "file_scoped_namespace_declaration"):
                    name = kit.text(c.child_by_field_name("name"), data)
                    full = f"{prefix}.{name}" if prefix else name
                    if c.type == "file_scoped_namespace_declaration":
                        out.append((c.start_byte, end_default, full))
                        rec(c, full, end_default)
                    else:
                        out.append((c.start_byte, c.end_byte, full))
                        body = c.child_by_field_name("body")
                        if body is not None:
                            rec(body, full, c.end_byte)
        rec(tree.root_node, "", len(data))
        return out

    def module_at(self, tree, data, path, root, offset=0):
        best = ""
        for s, e, name in self._namespaces(tree, data):
            if s <= offset < e and len(name) >= len(best):
                best = name
        return best

    def top(self, tree, data):
        out = []

        def rec(node):
            for c in node.children:
                if c.type in self.decl_types:
                    out.append(c)
                elif c.type in ("namespace_declaration", "file_scoped_namespace_declaration", "declaration_list"):
                    rec(c if c.type != "namespace_declaration" else (c.child_by_field_name("body") or c))
        rec(tree.root_node)
        return out

    def target_module(self, tree, data, path, root):
        spaces = self._namespaces(tree, data)
        return spaces[-1][2] if spaces else ""

    def sees(self, scope, module):
        return scope == module or (scope or "").startswith(module + ".") or module == ""

    def outer_modules(self, module):
        parts = module.split(".")
        return [".".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]

    def prepare(self, index, files):
        self.globals = set()
        for f in files:
            for m in re.finditer(rb"^\s*global\s+using\s+([\w.]+)\s*;", f.read_bytes(), re.M):
                self.globals.add(m.group(1).decode())

    def globally_seen(self, module):
        return module in self.globals

    def read_import(self, text):
        m = self._USING.match(text.strip())
        if not m:
            return None
        path = m.group(4).removeprefix("global::")
        if m.group(3):
            module, _, name = path.rpartition(".")
            return Imp(0, 0, module, [(name, m.group(3))])
        if m.group(2):
            module, _, name = path.rpartition(".")
            return Imp(0, 0, path, [], False)
        return Imp(0, 0, path, [], True)

    def import_line(self, module, name, alias=None):
        return f"using {alias} = {module}.{name};" if alias else f"using {module};"

    def wildcard_line(self, module):
        return f"using {module};"

    def group_line(self, imp, keep):
        return ""

    def qualifiers(self, module):
        return [f"global::{module}.", f"{module}."] if module else []

    def header_end(self, tree, data):
        return 0

    def import_point(self, tree, data, offset=0):
        imps = self.imports(tree, data)
        return max(i.end for i in imps) if imps else 0

    def _file_scoped(self, like):
        return re.search(rb"^\s*namespace\s+[\w.]+\s*;", like, re.M) is not None

    def new_indent(self, like):
        return "" if self._file_scoped(like) else "    "

    def new_file(self, module, imports, body, like):
        imps = ("\n".join(imports) + "\n\n") if imports else ""
        if not module:
            return imps + body.strip("\n") + "\n"
        if self._file_scoped(like):
            return f"{imps}namespace {module};\n\n{body.strip(chr(10))}\n"
        return f"{imps}namespace {module}\n{{\n{body.strip(chr(10))}\n}}\n"

    def body_point(self, tree, data, module):
        for c in kit.walk(tree.root_node):
            if c.type == "namespace_declaration" and kit.text(c.child_by_field_name("name"), data) == module.split(".")[-1]:
                body = c.child_by_field_name("body")
                close = body.end_byte - 1
                first = next((d for d in body.children if d.type in self.decl_types), None)
                return kit.line_start(data, close), kit.indent_of(data, first.start_byte) if first else "    "
        return None, ""

    def set_module(self, tree, data, old, new):
        edits = []
        for c in kit.walk(tree.root_node):
            if c.type in ("namespace_declaration", "file_scoped_namespace_declaration"):
                n = c.child_by_field_name("name")
                if kit.text(n, data) == old:
                    edits.append((n.start_byte, n.end_byte, new))
        from graphify.moves import replace_ranges

        return replace_ranges(data, edits) if edits else data


class Php(Lang):
    name = "php"
    suffixes = (".php",)
    sep = "\\"
    module_word = "namespace"
    decl_types = frozenset({"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration",
                            "function_definition"})
    import_types = frozenset({"namespace_use_declaration"})

    def __init__(self):
        self.functions: set[str] = set()

    def module_at(self, tree, data, path, root, offset=0):
        found = ""
        for c in tree.root_node.children:
            if c.type == "namespace_definition":
                name = c.child_by_field_name("name")
                body = c.child_by_field_name("body")
                if body is not None and not (c.start_byte <= offset < c.end_byte):
                    continue
                if c.start_byte <= offset or body is None and c.start_byte <= offset:
                    found = kit.text(name, data) if name is not None else ""
        return found

    def top(self, tree, data):
        out = []
        for c in tree.root_node.children:
            if c.type in self.decl_types:
                out.append(c)
            elif c.type == "namespace_definition" and c.child_by_field_name("body") is not None:
                out += [d for d in c.child_by_field_name("body").children if d.type in self.decl_types]
        return out

    def target_module(self, tree, data, path, root):
        return self.module_at(tree, data, path, root, len(data))

    def prepare(self, index, files):
        self.functions = {n for mod in index.values() for n, es in mod.items() if any(e.node.type == "function_definition" for _, e in es)}

    def read_import(self, text):
        t = text.strip().rstrip(";")
        m = re.match(r"use\s+(function\s+|const\s+)?(.*)$", t, re.S)
        if not m:
            return None
        rest = m.group(2).strip()
        g = re.match(r"\\?([\w\\]+)\\\{(.*)\}$", rest, re.S)
        if g:
            names = []
            for part in g.group(2).split(","):
                k = re.split(r"\s+as\s+", part.strip())
                if k[0]:
                    names.append((k[0], k[1] if len(k) > 1 else None))
            return Imp(0, 0, g.group(1), names, False, True)
        parts = [p.strip() for p in rest.split(",")]
        if len(parts) != 1:
            return None
        k = re.split(r"\s+as\s+", parts[0])
        module, _, name = k[0].lstrip("\\").rpartition("\\")
        return Imp(0, 0, module, [(name, k[1] if len(k) > 1 else None)])

    def _kind(self, name):
        return "function " if name in self.functions else ""

    def import_line(self, module, name, alias=None):
        return f"use {self._kind(name)}{module}\\{name}" + (f" as {alias}" if alias else "") + ";"

    def group_line(self, imp, keep):
        kind = re.match(r"use\s+(function\s+|const\s+)?", imp.text).group(1) or ""
        if len(keep) == 1:
            n, a = keep[0]
            return f"use {kind}{imp.module}\\{n}" + (f" as {a}" if a else "") + ";"
        return f"use {kind}{imp.module}\\{{" + ", ".join(n + (f" as {a}" if a else "") for n, a in keep) + "};"

    def retarget(self, text, module, new, name):
        return text.replace(f"{module}\\{name}", f"{new}\\{name}", 1)

    def qualifiers(self, module):
        return [f"\\{module}\\", f"{module}\\"] if module else ["\\"]

    def sees(self, scope, module):
        return scope == module

    def header_end(self, tree, data):
        for c in tree.root_node.children:
            if c.type == "namespace_definition" and c.child_by_field_name("body") is None:
                return kit.line_end(data, c.end_byte - 1)
        for c in tree.root_node.children:
            if c.type in ("php_tag", "declare_statement"):
                end = kit.line_end(data, c.end_byte - 1)
        return locals().get("end", 0)

    def new_file(self, module, imports, body, like):
        strict = re.search(rb"^declare\s*\(\s*strict_types\s*=\s*1\s*\)\s*;", like, re.M)
        out = "<?php\n\n" + ("declare(strict_types=1);\n\n" if strict else "")
        out += f"namespace {module};\n\n" if module else ""
        out += ("\n".join(imports) + "\n\n") if imports else ""
        return out + body.strip("\n") + "\n"

    def set_module(self, tree, data, old, new):
        for c in tree.root_node.children:
            if c.type == "namespace_definition":
                n = c.child_by_field_name("name")
                if n is not None and kit.text(n, data) == old:
                    return data[:n.start_byte] + new.encode() + data[n.end_byte:]
        return data

    def derive(self, root, source, module, target, draft):
        composer = root / "composer.json"
        if composer.is_file():
            try:
                maps = json.loads(composer.read_text())
            except ValueError:
                maps = {}
            for section in ("autoload", "autoload-dev"):
                for prefix, dirs in (maps.get(section, {}).get("psr-4", {}) or {}).items():
                    for d in [dirs] if isinstance(dirs, str) else dirs:
                        base = (root / d).resolve()
                        if target.parent.resolve().is_relative_to(base):
                            rest = target.parent.resolve().relative_to(base).parts
                            return "\\".join([p for p in prefix.strip("\\").split("\\") if p] + list(rest))
        return analogy(root, source, module, target, "\\")

    def keeps(self, data):
        rest = re.sub(rb"<\?php|declare\s*\([^)]*\)\s*;|namespace\s+[\w\\]+\s*;|use\s+[^;]+;|//[^\n]*|/\*.*?\*/", b"", data, flags=re.S)
        return bool(rest.strip())


class Swift(Lang):
    name = "swift"
    suffixes = (".swift",)
    module_word = "module"
    wildcard_only = True
    acyclic = True
    public_word = "public"
    decl_types = frozenset({"class_declaration", "protocol_declaration", "function_declaration", "property_declaration",
                            "typealias_declaration"})
    import_types = frozenset({"import_declaration"})

    def module_at(self, tree, data, path, root, offset=0):
        parts = Path(kit.rel(root, path)).parts
        for i, p in enumerate(parts[:-1]):
            if p in ("Sources", "Tests") and i + 1 < len(parts) - 1:
                return parts[i + 1]
        return ""

    def derive(self, root, source, module, target, draft):
        return self.module_at(None, b"", target, root)

    def decl_name(self, node, data):
        n = node.child_by_field_name("name")
        if n is None and node.type == "property_declaration":
            n = next((c for c in kit.walk(node) if c.type == "simple_identifier"), None)
        return kit.text(n, data).split("<")[0].strip() if n is not None else None

    def is_public(self, node, data):
        mods = next((c for c in node.children if c.type == "modifiers"), None)
        return mods is not None and re.search(r"\b(public|open)\b", kit.text(mods, data)) is not None

    def make_public(self, block):
        if re.search(r"\b(private|fileprivate)\b", block.split("{")[0]):
            return block
        return re.sub(r"(?m)^(\s*(?:@\w+(?:\([^)]*\))?\s+)*)((?:(?:final|static|indirect|internal)\s+)*(?:class|struct|enum|protocol|func|var|let|typealias|actor|extension)\b)",
                      lambda m: m.group(1) + "public " + m.group(2).replace("internal ", ""), block, count=1)

    _MEMBERS = frozenset({"function_declaration", "property_declaration", "init_declaration", "subscript_declaration",
                          "class_declaration", "protocol_declaration", "typealias_declaration"})

    def open_members(self, block, names):
        data = block.encode()
        tree = kit.parse(Path("x.swift"), data)
        edits = []
        for top in tree.root_node.children:
            body = top.child_by_field_name("body")
            for m in body.children if body is not None and top.type == "class_declaration" else ():
                if m.type not in self._MEMBERS:
                    continue
                n = m.child_by_field_name("name")
                name = "<init>" if m.type == "init_declaration" else kit.text(n, data).split("<")[0].strip() if n is not None else ""
                if name not in names:
                    continue
                mods = next((c for c in m.children if c.type == "modifiers"), None)
                words = kit.text(mods, data) if mods is not None else ""
                if re.search(r"\b(public|open|private|fileprivate)\b", words):
                    continue
                plain = [c for c in mods.children if c.type != "attribute"] if mods is not None else []
                at = plain[0].start_byte if plain else (mods.end_byte + 1 if mods is not None else m.start_byte)
                edits.append((at, at, "public "))
        from graphify.moves import replace_ranges

        return replace_ranges(data, edits).decode() if edits else block

    def read_import(self, text):
        m = re.match(r"(?:@\w+\s+)*import\s+(?:(?:struct|class|enum|protocol|func|var|let|typealias)\s+)?([\w.]+)", text.strip())
        return Imp(0, 0, m.group(1).split(".")[0], [], True) if m else None

    def wildcard_line(self, module):
        return f"import {module}"

    def import_line(self, module, name, alias=None):
        return f"import {module}"

    def new_file(self, module, imports, body, like):
        return (("\n".join(imports) + "\n\n") if imports else "") + body.strip("\n") + "\n"

    def keeps(self, data):
        rest = re.sub(rb"(?m)^\s*(?:@\w+\s+)*import\s+[^\n]*$|//[^\n]*|/\*.*?\*/", b"", data, flags=re.S)
        return bool(rest.strip())


class VisualBasic(CSharp):
    name = "visual basic"
    suffixes = (".vb",)
    decl_types = frozenset({"type_declaration"})
    import_types = frozenset({"imports_statement"})
    _HEADERS = CSharp._HEADERS | {"imports_statement"}
    _NAMESPACES = CSharp._NAMESPACES | {"namespace_block"}
    _IMPORTS = re.compile(r"Imports\s+(?:(\w+)\s*=\s*)?([\w.]+)", re.I)

    def _namespaces(self, tree, data):
        out = []

        def rec(node, prefix):
            for c in node.children:
                if c.type == "namespace_block":
                    name = kit.text(c.child_by_field_name("name"), data)
                    full = f"{prefix}.{name}" if prefix else name
                    out.append((c.start_byte, c.end_byte, full))
                    rec(c, full)

        rec(tree.root_node, "")
        return out

    def top(self, tree, data):
        out = []

        def rec(node):
            for c in node.children:
                if c.type in self.decl_types:
                    out.append(c)
                elif c.type == "namespace_block":
                    rec(c)

        rec(tree.root_node)
        return out

    def module_at(self, tree, data, path, root, offset=0):
        spaces = self._namespaces(tree, data)
        if offset == 0 and spaces:
            return spaces[-1][2]
        return super().module_at(tree, data, path, root, offset)

    def decl_name(self, node, data):
        block = node.named_children[0] if node.named_children else None
        n = block.child_by_field_name("name") if block is not None else None
        return kit.text(n, data) if n is not None else None

    def is_public(self, node, data):
        return not re.match(rb"\s*(?:<[^>]*>\s*)*(?:Private|Friend)\b", data[node.start_byte:node.end_byte], re.I)

    def prepare(self, index, files):
        self.globals = set()

    def read_import(self, text):
        m = self._IMPORTS.match(text.strip())
        if not m:
            return None
        if m.group(1):
            module, _, name = m.group(2).rpartition(".")
            return Imp(0, 0, module, [(name, m.group(1))])
        return Imp(0, 0, m.group(2), [], True)

    def import_line(self, module, name, alias=None):
        return f"Imports {alias} = {module}.{name}" if alias else f"Imports {module}"

    def wildcard_line(self, module):
        return f"Imports {module}"

    def qualifiers(self, module):
        return [f"Global.{module}.", f"{module}."] if module else []

    def _file_scoped(self, like):
        return False

    def new_indent(self, like):
        return "    " if re.search(rb"(?im)^\s*Namespace\s", like) else ""

    def new_file(self, module, imports, body, like):
        imps = ("\n".join(imports) + "\n\n") if imports else ""
        if not module or not re.search(rb"(?im)^\s*Namespace\s", like):
            return imps + body.strip("\n") + "\n"
        return f"{imps}Namespace {module}\n{body.strip(chr(10))}\nEnd Namespace\n"

    def body_point(self, tree, data, module):
        for c in kit.walk(tree.root_node):
            if c.type == "namespace_block" and kit.text(c.child_by_field_name("name"), data) == module.split(".")[-1]:
                end = data.rfind(b"End Namespace", c.start_byte, c.end_byte)
                first = next((d for d in c.children if d.type in self.decl_types), None)
                return kit.line_start(data, end), kit.indent_of(data, first.start_byte) if first else "    "
        return None, ""

    def set_module(self, tree, data, old, new):
        edits = []
        for c in kit.walk(tree.root_node):
            if c.type == "namespace_block":
                n = c.child_by_field_name("name")
                if kit.text(n, data) == old:
                    edits.append((n.start_byte, n.end_byte, new))
        from graphify.moves import replace_ranges

        return replace_ranges(data, edits) if edits else data


register(Java(), Groovy(), Kotlin(), Scala(), CSharp(), Php(), Swift(), VisualBasic())
