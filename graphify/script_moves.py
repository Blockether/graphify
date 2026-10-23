"""JavaScript and TypeScript moves: move a module file, or move a top-level declaration.

Module specifiers follow the move: ``import`` and ``export ... from``, ``require``,
``import()``, ``jest.mock`` and ``vi.mock``, relative paths and ``tsconfig.json``
path aliases. A definition move takes the imports that the code needs and
repoints named, default, namespace and re-export imports of the declaration.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from graphify.moves import MovePlan, project_files, replace_ranges, resolve_path
from graphify.refactor import Mention, Unsupported, symbol_name

SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
_EXTS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
_CALLS = frozenset({
    "require", "import", "require.resolve",
    "jest.mock", "jest.unmock", "jest.doMock", "jest.dontMock", "jest.requireActual", "jest.requireMock", "jest.createMockFromModule",
    "vi.mock", "vi.unmock", "vi.doMock", "vi.doUnmock", "vi.importActual", "vi.importMock",
})
_DECLS = frozenset({
    "function_declaration", "generator_function_declaration", "function_signature", "class_declaration",
    "abstract_class_declaration", "lexical_declaration", "variable_declaration", "interface_declaration",
    "type_alias_declaration", "enum_declaration",
})
_TYPE_DECLS = frozenset({"interface_declaration", "type_alias_declaration"})
_IDENTS = frozenset({"identifier", "type_identifier", "shorthand_property_identifier"})
_PARSERS: dict = {}


def _parse(data: bytes, path: Path):
    suffix = path.suffix.lower()
    kind = "tsx" if suffix == ".tsx" else "ts" if suffix in (".ts", ".mts", ".cts") else "js"
    if kind not in _PARSERS:
        from tree_sitter import Language, Parser

        if kind == "js":
            import tree_sitter_javascript as tsjs

            lang = tsjs.language()
        else:
            import tree_sitter_typescript as tsts

            lang = tsts.language_tsx() if kind == "tsx" else tsts.language_typescript()
        _PARSERS[kind] = Parser(Language(lang))
    return _PARSERS[kind].parse(data)


def _text(node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _walk(node, skip=()):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in skip:
            continue
        yield n
        stack.extend(reversed(n.children))


def _rel(root: Path, path) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- specifiers


def _jsonc(text: str):
    try:
        return json.loads(text)
    except ValueError:
        pass
    out, i, quote = [], 0, None
    while i < len(text):
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1 : i + 2])
                i += 1
            elif c == quote:
                quote = None
        elif c == '"':
            quote = c
            out.append(c)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = len(text) if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    try:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))
    except ValueError:
        return None


def aliases(root: Path) -> list[tuple[str, str]]:
    """The ``(pattern, target)`` path aliases of ``tsconfig.json`` or ``jsconfig.json``."""
    out = []
    for name in ("tsconfig.json", "jsconfig.json"):
        f = root / name
        if not f.is_file():
            continue
        cfg = _jsonc(f.read_text(encoding="utf-8", errors="replace")) or {}
        opts = cfg.get("compilerOptions") or {}
        base = root / str(opts.get("baseUrl") or ".")
        for pattern, targets in (opts.get("paths") or {}).items():
            for t in targets or ():
                out.append((str(pattern), os.path.normpath(str(base / str(t)))))
        if opts.get("baseUrl"):
            out.append(("*", os.path.normpath(str(base)) + "/*"))
    return out


@dataclass(frozen=True)
class _Hit:
    path: Path
    style: str  # exact | swap | bare | index
    ext: str
    alias: tuple[str, str] | None = None


def _lookup(p: Path) -> _Hit | None:
    sfx = p.suffix.lower()
    if p.is_file() and sfx in SUFFIXES:
        return _Hit(p.resolve(), "exact", p.suffix)
    for ts in _JS_TO_TS.get(sfx, ()):
        q = p.with_suffix(ts)
        if q.is_file():
            return _Hit(q.resolve(), "swap", p.suffix)
    for e in _EXTS:
        q = Path(str(p) + e)
        if q.is_file():
            return _Hit(q.resolve(), "bare", "")
    if p.is_dir():
        for e in _EXTS:
            q = p / ("index" + e)
            if q.is_file():
                return _Hit(q.resolve(), "index", "")
    return None


def resolve_spec(base: Path, spec: str, alias_list=()) -> _Hit | None:
    """The project file that the module specifier ``spec`` in directory ``base`` names, or None."""
    if spec.startswith("."):
        return _lookup(Path(os.path.normpath(str(base / spec))))
    for pattern, target in alias_list:
        if "*" in pattern:
            pre, post = pattern.split("*", 1)
            if not (spec.startswith(pre) and spec.endswith(post) and len(spec) >= len(pre) + len(post)):
                continue
            rest = spec[len(pre) : len(spec) - len(post)]
            hit = _lookup(Path(target.replace("*", rest, 1)))
        elif spec == pattern:
            hit = _lookup(Path(target))
        else:
            continue
        if hit is not None:
            return _Hit(hit.path, hit.style, hit.ext, (pattern, target))
    return None


def _strip(path: Path) -> str:
    s = str(path)
    for e in sorted(_EXTS, key=len, reverse=True):
        if s.lower().endswith(e):
            return s[: -len(e)]
    return s


def spell(from_dir: Path, target: Path, hit: _Hit) -> str:
    """Spell a specifier for ``target`` from ``from_dir`` in the style of ``hit``."""
    if hit.style == "index":
        body = str(target.parent) if target.stem == "index" else _strip(target)
    elif hit.style == "bare":
        body = _strip(target)
    elif hit.style == "swap":
        body = _strip(target) + hit.ext
    else:
        body = str(target)
    if hit.alias is not None:
        pattern, tgt = hit.alias
        if "*" in pattern:
            tpre, tpost = tgt.split("*", 1)
            if body.startswith(tpre) and body.endswith(tpost) and len(body) > len(tpre) + len(tpost):
                pre, post = pattern.split("*", 1)
                return pre + body[len(tpre) : len(body) - len(tpost)] + post
        elif body == _strip(Path(tgt)) or body == tgt:
            return pattern
    rel = os.path.relpath(body, from_dir).replace(os.sep, "/")
    return rel if rel.startswith(".") else "./" + rel


def _specifiers(tree, data: bytes):
    """Yield ``(string_node, kind)`` for each module specifier: ``static`` or ``call``."""
    for n in _walk(tree.root_node):
        if n.type in ("import_statement", "export_statement"):
            s = n.child_by_field_name("source")
            if s is not None and s.type == "string":
                yield s, "static"
        elif n.type == "import_require_clause":
            s = n.child_by_field_name("source") or next((c for c in n.named_children if c.type == "string"), None)
            if s is not None and s.type == "string":
                yield s, "static"
        elif n.type == "call_expression":
            f = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            if f is None or args is None or _text(f, data) not in _CALLS:
                continue
            first = next((c for c in args.named_children if c.type != "comment"), None)
            if first is not None and first.type == "string":
                yield first, "call"


def _value(node, data: bytes) -> tuple[str, str]:
    raw = _text(node, data)
    return raw[0], raw[1:-1]


# ---------------------------------------------------------------- file move


def _companions(old: Path, target: Path) -> dict[Path, Path]:
    out = {}
    for kind in ("test", "spec"):
        for sfx in SUFFIXES:
            name = f"{old.stem}.{kind}{sfx}"
            new = f"{target.stem}.{kind}{sfx}"
            for d, nd in ((old.parent, target.parent), (old.parent / "__tests__", target.parent / "__tests__")):
                if (d / name).is_file():
                    out[(d / name).resolve()] = nd / new
    return out


def plan_move_file(graph: dict, old: Path, new: str, root: Path, tests: bool = True) -> MovePlan:
    target = resolve_path(root, new)
    if new.endswith(("/", os.sep)) or target.is_dir():
        target = target / old.name
    elif not target.suffix:
        target = target.with_suffix(old.suffix)
    conflicts, notes = [], []
    if target.suffix.lower() not in SUFFIXES:
        conflicts.append(f"{target} is not a JavaScript or TypeScript file.")
    moved = {old: target}
    if tests and target != old:
        moved.update(_companions(old, target))
    for src, dst in moved.items():
        if dst.exists():
            conflicts.append(f"{dst} already exists.")
    alias_list = aliases(root)
    contents: dict = {}
    calls = 0
    for f in sorted(set(project_files(root, graph, SUFFIXES)) | set(moved)):
        data = f.read_bytes()
        nf = moved.get(f, f)
        edits = []
        for node, kind in _specifiers(_parse(data, f), data):
            quote, spec = _value(node, data)
            hit = resolve_spec(f.parent, spec, alias_list)
            if hit is None or (hit.path not in moved and (f not in moved or hit.alias is not None)):
                continue
            new_spec = spell(nf.parent, moved.get(hit.path, hit.path), hit)
            if new_spec != spec:
                edits.append((node.start_byte, node.end_byte, quote + new_spec + quote))
                calls += kind == "call"
        after = replace_ranges(data, edits)
        if f in moved:
            contents[str(nf)] = (None, after)
            contents[str(f)] = (data, None)
        elif after != data:
            contents[str(f)] = (data, after)
    if len(moved) > 1:
        notes.append("Tests move along: " + ", ".join(f"{_rel(root, a)} -> {_rel(root, b)}" for a, b in moved.items() if a != old) + ".")
    if calls:
        notes.append(f"{calls} require, import() or mock calls follow the module.")
    renames = tuple((_rel(root, a), _rel(root, b)) for a, b in moved.items())
    mentions = _path_mentions(root, graph, contents, {a: b for a, b in moved.items()})
    return MovePlan(f"{_rel(root, old)} -> {_rel(root, target)}", str(old), str(target), contents, tuple(conflicts), tuple(notes), tuple(mentions), renames)


def _path_mentions(root: Path, graph: dict, contents: dict, moved: dict) -> list[Mention]:
    """Lines that still spell an old path, such as package.json entries or path strings."""
    needles = [_strip(Path(_rel(root, a))) for a in moved]
    needles = [n for n in needles if "/" in n or n]
    pats = [re.compile(r"(?<![\w.-])" + re.escape(n) + r"(?![\w-])") for n in needles]
    out = []
    files = {str(p) for p in project_files(root, graph, SUFFIXES + (".json",))} | set(contents)
    for path in sorted(files):
        if path in contents:
            data = contents[path][1]
        else:
            p = Path(path)
            data = p.read_bytes() if p.is_file() else None
        if not data:
            continue
        text = data.decode("utf-8", "replace")
        if not any(n in text for n in needles):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in pats):
                out.append(Mention(path, i, line.strip()[:120], "text"))
    return out


# ---------------------------------------------------------------- imports


@dataclass(frozen=True)
class _Named:
    imported: str
    local: str
    text: str
    node: object


@dataclass(frozen=True)
class _Import:
    stmt: object
    source: object
    quote: str
    spec: str
    type_only: bool
    default: str | None
    namespace: str | None
    named: tuple[_Named, ...]
    braces: object


def _imports(tree, data: bytes) -> list[_Import]:
    out = []
    for stmt in tree.root_node.named_children:
        if stmt.type != "import_statement":
            continue
        source = stmt.child_by_field_name("source")
        if source is None or source.type != "string":
            continue
        quote, spec = _value(source, data)
        type_only = any(c.type == "type" for c in stmt.children)
        default = namespace = braces = None
        named = []
        clause = next((c for c in stmt.named_children if c.type == "import_clause"), None)
        for c in clause.named_children if clause is not None else ():
            if c.type == "identifier":
                default = _text(c, data)
            elif c.type == "namespace_import":
                namespace = _text(c.named_children[-1], data)
            elif c.type == "named_imports":
                braces = c
                for s in c.named_children:
                    if s.type != "import_specifier":
                        continue
                    imported = _text(s.child_by_field_name("name"), data)
                    alias = s.child_by_field_name("alias")
                    named.append(_Named(imported, _text(alias, data) if alias is not None else imported, _text(s, data), s))
        out.append(_Import(stmt, source, quote, spec, type_only, default, namespace, tuple(named), braces))
    return out


def _semi(tree, data: bytes) -> bool:
    ends = [_text(s, data).rstrip().endswith(";") for s in tree.root_node.named_children if s.type in ("import_statement", "lexical_declaration", "expression_statement", "export_statement")]
    return not ends or sum(ends) * 2 >= len(ends)


def _quote(tree, data: bytes, default: str = "'") -> str:
    for imp in _imports(tree, data):
        return imp.quote
    return default


def _statement(kind: str, type_only: bool, default, namespace, named, spec: str, quote: str, semi: bool) -> str:
    parts = []
    if default:
        parts.append(default)
    if namespace:
        parts.append(f"* as {namespace}")
    if named or not parts:
        parts.append("{ " + ", ".join(named) + " }")
    return f"{kind} {'type ' if type_only else ''}{', '.join(parts)} from {quote}{spec}{quote}" + (";" if semi else "")


def _line_span(data: bytes, node) -> tuple[int, int]:
    start = data.rfind(b"\n", 0, node.start_byte) + 1
    end = data.find(b"\n", node.end_byte)
    return start, len(data) if end < 0 else end + 1


def _braces(data: bytes, node, texts) -> str:
    body = _text(node, data)
    if "\n" not in body:
        return "{ " + ", ".join(texts) + " }"
    first = node.named_children[0] if node.named_children else node
    line = data.rfind(b"\n", 0, first.start_byte) + 1
    indent = data[line : first.start_byte].decode()
    close = data.rfind(b"\n", 0, node.end_byte - 1) + 1
    return "{\n" + "".join(f"{indent}{t},\n" for t in texts) + data[close : node.end_byte - 1].decode() + "}"


def _reimport(data: bytes, imp: _Import, default, namespace, named_texts, semi: bool):
    """The edit that turns ``imp`` into an import of these parts, or deletes it."""
    if not default and not namespace and not named_texts:
        s, e = _line_span(data, imp.stmt)
        return (s, e, "")
    if default == imp.default and namespace == imp.namespace and imp.braces is not None and named_texts:
        return (imp.braces.start_byte, imp.braces.end_byte, _braces(data, imp.braces, named_texts))
    text = _statement("import", imp.type_only, default, namespace, named_texts, imp.spec, imp.quote, _text(imp.stmt, data).rstrip().endswith(";"))
    return (imp.stmt.start_byte, imp.stmt.end_byte, text)


def _insert_point(tree, data: bytes) -> int:
    last = None
    for stmt in tree.root_node.named_children:
        if stmt.type == "import_statement":
            last = stmt
    if last is not None:
        return _line_span(data, last)[1]
    for stmt in tree.root_node.named_children:
        if stmt.type == "comment" or stmt.type == "hash_bang_line":
            continue
        if stmt.type == "expression_statement" and stmt.named_children and stmt.named_children[0].type == "string":
            continue
        return _line_span(data, stmt)[0]
    return len(data)


def add_import(data: bytes, path: Path, target, spec: str, *, default=None, named=(), type_only=False, alias_list=(), namespace=None) -> bytes:
    """Import ``default`` and ``named`` texts from ``spec`` (the file ``target``), merged into an import that exists."""
    tree = _parse(data, path)
    named = list(named)
    if namespace:
        if not any(i.namespace == namespace and i.spec == spec for i in _imports(tree, data)):
            data = _insert_line(data, tree, _statement("import", type_only, None, namespace, [], spec, _quote(tree, data, "'"), _semi(tree, data)))
            tree = _parse(data, path)
        if not default and not named:
            return data
    for imp in _imports(tree, data):
        hit = resolve_spec(path.parent, imp.spec, alias_list) if imp.spec != spec else None
        same = imp.spec == spec or (hit is not None and target is not None and hit.path == target)
        if not same or imp.type_only != type_only or imp.namespace:
            continue
        have = {n.text for n in imp.named} | {n.local for n in imp.named if n.local == n.imported}
        new = [t for t in named if t not in have]
        dflt = imp.default or default
        if default and imp.default and imp.default != default:
            continue
        if not new and dflt == imp.default:
            return data
        edit = _reimport(data, imp, dflt, None, [n.text for n in imp.named] + new, True)
        return replace_ranges(data, [edit])
    if not default and not named:
        return data
    return _insert_line(data, tree, _statement("import", type_only, default, None, named, spec, _quote(tree, data), _semi(tree, data)))


def _insert_line(data: bytes, tree, line: str) -> bytes:
    line += "\n"
    at = _insert_point(tree, data)
    if at == len(data) and data and not data.endswith(b"\n"):
        line = "\n" + line
    tail = data[at:]
    if at > 0 and not any(s.type == "import_statement" for s in tree.root_node.named_children) and tail.strip():
        line += "\n"
    elif at == 0 and tail.strip():
        line += "\n"
    return data[:at] + line.encode() + tail


# ---------------------------------------------------------------- definition move


@dataclass(frozen=True)
class _Entry:
    stmt: object
    decl: object
    exported: bool
    default: bool


def _declared(decl, data: bytes) -> list[str]:
    if decl.type in ("lexical_declaration", "variable_declaration"):
        out = []
        for d in decl.named_children:
            n = d.child_by_field_name("name") if d.type == "variable_declarator" else None
            if n is not None and n.type == "identifier":
                out.append(_text(n, data))
        return out
    n = decl.child_by_field_name("name")
    return [_text(n, data)] if n is not None else []


def _tops(tree, data: bytes):
    """Top-level declarations: ``{name: [_Entry]}``, the export clauses and the ``export default name`` statements."""
    decls: dict[str, list[_Entry]] = {}
    clauses, defaults = [], {}
    for stmt in tree.root_node.named_children:
        if stmt.type == "export_statement":
            decl = stmt.child_by_field_name("declaration")
            default = any(c.type == "default" for c in stmt.children)
            if decl is not None and decl.type in _DECLS:
                for name in _declared(decl, data):
                    decls.setdefault(name, []).append(_Entry(stmt, decl, True, default))
            elif stmt.child_by_field_name("source") is None:
                value = stmt.child_by_field_name("value")
                if default and value is not None and value.type == "identifier":
                    defaults[_text(value, data)] = stmt
                clause = next((c for c in stmt.named_children if c.type == "export_clause"), None)
                if clause is not None:
                    clauses.append((stmt, clause))
        elif stmt.type in _DECLS:
            for name in _declared(stmt, data):
                decls.setdefault(name, []).append(_Entry(stmt, stmt, False, False))
        elif stmt.type == "ambient_declaration":
            for decl in stmt.named_children:
                if decl.type in _DECLS:
                    for name in _declared(decl, data):
                        decls.setdefault(name, []).append(_Entry(stmt, decl, False, False))
    return decls, clauses, defaults


def _clause_names(clauses, data: bytes, name: str) -> list[tuple[object, object, str]]:
    """``(stmt, specifier, exported_name)`` of each export clause entry for the local ``name``."""
    out = []
    for stmt, clause in clauses:
        for s in clause.named_children:
            if s.type == "export_specifier" and _text(s.child_by_field_name("name"), data) == name:
                alias = s.child_by_field_name("alias")
                out.append((stmt, s, _text(alias, data) if alias is not None else name))
    return out


def _idents(node, data: bytes, skip=()) -> set[str]:
    return {_text(n, data) for n in _walk(node, skip) if n.type in _IDENTS}


def _span(data: bytes, stmt) -> tuple[int, int]:
    cur = stmt
    prev = stmt.prev_named_sibling
    while prev is not None and prev.type == "comment" and data[prev.end_byte : cur.start_byte].count(b"\n") <= 1:
        cur, prev = prev, prev.prev_named_sibling
    return _line_span(data, cur)[0], _line_span(data, stmt)[1]


def _cut(data: bytes, spans) -> bytes:
    out = data
    for start, end in sorted(set(spans), reverse=True):
        left, right = out[:start], out[end:]
        nb = len(left) - len(left.rstrip(b"\n"))
        na = len(right) - len(right.lstrip(b"\n"))
        left, right = left.rstrip(b"\n"), right.lstrip(b"\n")
        if not left:
            out = right
        elif not right:
            out = left + b"\n"
        else:
            out = left + b"\n" * max(1, min(nb + na, 2)) + right
    return out


def _clause_edit(data: bytes, stmt, clause, drop) -> tuple[int, int, str]:
    keep = [_text(s, data) for s in clause.named_children if s.type == "export_specifier" and s not in drop]
    if not keep:
        s, e = _line_span(data, stmt)
        return (s, e, "")
    return (clause.start_byte, clause.end_byte, _braces(data, clause, keep))


def _target_path(to: str, source: Path, root: Path) -> Path:
    target = resolve_path(root, to)
    if to.endswith(("/", os.sep)) or target.is_dir():
        return target / source.name
    if not target.suffix:
        return target.with_suffix(source.suffix)
    return target


def _style_of(tree, data: bytes, path: Path, alias_list) -> str:
    for imp in _imports(tree, data):
        if imp.spec.startswith("."):
            hit = resolve_spec(path.parent, imp.spec, alias_list)
            if hit is not None:
                return "bare" if hit.style == "index" else hit.style
    return "bare"


def _hit_for(target: Path, style: str, alias=None, ext: str | None = None) -> _Hit:
    if style == "swap":
        ext = ext or {".ts": ".js", ".tsx": ".js", ".mts": ".mjs", ".cts": ".cjs"}.get(target.suffix.lower(), target.suffix)
    return _Hit(target, style, ext or "", alias)


def _exported_as(decls, clauses, defaults, data: bytes, local: str) -> tuple[str | None, str | None]:
    """How another module imports ``local``: ``(default_local, named_text)``. Both are None when it is not exported."""
    entries = decls.get(local, ())
    if any(e.exported and not e.default for e in entries):
        return None, local
    for _, _, alias in _clause_names(clauses, data, local):
        if alias == local:
            return None, local
    if any(e.default for e in entries) or local in defaults:
        return local, None
    for _, _, alias in _clause_names(clauses, data, local):
        return (local, None) if alias == "default" else (None, f"{alias} as {local}")
    return None, None


def _with_export(data: bytes, stmts) -> bytes:
    return replace_ranges(data, [(s.start_byte, s.start_byte, "export ") for s in stmts])


def plan_move(graph: dict, node: dict, source: Path, to: str, root: Path) -> MovePlan:
    name = symbol_name(str(node.get("label", "")))
    target = _target_path(to, source, root)
    if target.suffix.lower() not in SUFFIXES:
        raise Unsupported(f"{target} is not a JavaScript or TypeScript file.")
    data = source.read_bytes()
    tree = _parse(data, source)
    decls, clauses, defaults = _tops(tree, data)
    entries = decls.get(name)
    if not entries:
        raise Unsupported(f"`{name}` is not a top-level declaration of {source}.")
    if re.search(rb"\bmodule\.exports\b|^\s*exports\.\w+\s*=", data, re.M) and not any(e.exported for e in entries):
        raise Unsupported(f"{source} uses CommonJS exports. Move the whole file with move_file instead.")
    for e in entries:
        if len(_declared(e.decl, data)) > 1:
            raise Unsupported(f"`{name}` shares one declaration with {', '.join(n for n in _declared(e.decl, data) if n != name)}. Split the declaration first.")
    alias_list = aliases(root)
    conflicts, notes, mentions = [], [], []
    if target == source:
        conflicts.append(f"`{name}` is already in {source}.")
    is_type = all(e.decl.type in _TYPE_DECLS for e in entries)
    clause_hits = _clause_names(clauses, data, name)
    default_stmt = defaults.get(name)
    publics = {alias for _, _, alias in clause_hits}
    if any(e.exported and not e.default for e in entries):
        publics.add(name)
    if any(e.default for e in entries) or default_stmt is not None:
        publics.add("default")
    # The source without the declaration.
    drops: dict = {}
    for stmt, spec, _ in clause_hits:
        drops.setdefault(stmt.id, (stmt, []))[1].append(spec)
    step = replace_ranges(data, [_clause_edit(data, stmt, next(c for c in stmt.named_children if c.type == "export_clause"), specs) for stmt, specs in drops.values()])
    step_tree = _parse(step, source)
    step_decls, _, step_defaults = _tops(step_tree, step)
    spans = [_span(step, e.stmt) for e in step_decls[name]]
    pieces = []
    named_export = any(e.exported and not e.default for e in entries) or name in publics
    remaining_probe = _cut(step, spans + ([_line_span(step, step_defaults[name])] if name in step_defaults else []))
    left_tree = _parse(remaining_probe, source)
    remaining = name in _idents(left_tree.root_node, remaining_probe, skip=("import_statement",))
    if not any(e.exported for e in entries) and remaining and "default" not in publics:
        named_export = True
    for e, (s, end) in zip(step_decls[name], spans):
        piece = step[s:end]
        if named_export and not e.exported:
            at = e.stmt.start_byte - s
            piece = piece[:at] + b"export " + piece[at:]
        pieces.append(piece.decode("utf-8", "replace").strip("\n"))
    code = "\n".join(pieces)
    semi = _semi(tree, data)
    for alias in sorted(publics - {name, "default"}):
        code += f"\n\nexport {{ {name} as {alias} }}" + (";" if semi else "")
    if default_stmt is not None:
        code += f"\n\nexport default {name}" + (";" if semi else "")
    leftover = remaining_probe
    # What the moved code needs.
    free = set().union(*(_idents(e.stmt, data) for e in entries)) - {name}
    need_locals = sorted(n for n in free if n in decls)
    if need_locals and remaining:
        conflicts.append(f"`{name}` uses {', '.join(need_locals)} of {_rel(root, source)}, and {_rel(root, source)} still uses `{name}`: a circular import. Move {', '.join(need_locals)} first.")
    tgt_exists = target.is_file()
    tgt_data = target.read_bytes() if tgt_exists else b""
    t = tgt_data
    if tgt_exists:
        t_tree = _parse(t, target)
        t_decls, _, t_defaults = _tops(t_tree, t)
        if name in t_decls:
            conflicts.append(f"{_rel(root, target)} already declares `{name}`.")
        if "default" in publics and (t_defaults or any(e.default for es in t_decls.values() for e in es)):
            conflicts.append(f"{_rel(root, target)} already has a default export.")
        t, c = _repoint_file(t, target, source, target, publics, name, alias_list, local=True)
        conflicts += c
    style = _style_of(tree, data, source, alias_list)
    for imp in _imports(tree, data):
        default = imp.default if imp.default in free else None
        namespace = imp.namespace if imp.namespace in free else None
        named = [n.text for n in imp.named if n.local in free]
        if not (default or namespace or named):
            continue
        hit = resolve_spec(source.parent, imp.spec, alias_list)
        if hit is not None and hit.path == target:
            continue
        spec = spell(target.parent, hit.path, hit) if hit is not None else imp.spec
        t = add_import(t, target, hit.path if hit else None, spec, default=default, named=named, type_only=imp.type_only, alias_list=alias_list, namespace=namespace)
    exports_needed = []
    if need_locals:
        n_default, n_named = None, []
        for local in need_locals:
            d, nm = _exported_as(decls, clauses, defaults, data, local)
            if d is None and nm is None:
                exports_needed.append(local)
                nm = local
            if d:
                n_default = d
            if nm:
                n_named.append(nm)
        spec = spell(target.parent, source, _hit_for(source, style))
        t = add_import(t, target, source, spec, default=n_default, named=n_named, alias_list=alias_list)
    t = (t.rstrip(b"\n") + b"\n\n" if t.strip() else b"") + code.encode() + b"\n"
    contents: dict = {str(target): (tgt_data if tgt_exists else None, t)}
    if not tgt_exists:
        notes.append(f"Created {_rel(root, target)}.")
    # The source file.
    s = leftover
    s_tree = _parse(s, source)
    uses = _idents(s_tree.root_node, s, skip=("import_statement",))
    edits = []
    for imp in _imports(s_tree, s):
        default = imp.default if imp.default and (imp.default not in free or imp.default in uses) else None
        namespace = imp.namespace if imp.namespace and (imp.namespace not in free or imp.namespace in uses) else None
        named = [n.text for n in imp.named if n.local not in free or n.local in uses]
        if (default, namespace, len(named)) != (imp.default, imp.namespace, len(imp.named)):
            edits.append(_reimport(s, imp, default, namespace, named, semi))
    s = replace_ranges(s, edits)
    if exports_needed:
        s_decls, _, _ = _tops(_parse(s, source), s)
        s = _with_export(s, [e.stmt for local in exports_needed for e in s_decls.get(local, ())])
        notes.append(f"{_rel(root, source)} now exports {', '.join(exports_needed)} for `{name}`.")
    if remaining:
        spec = spell(source.parent, target, _hit_for(target, style))
        s = add_import(s, source, target, spec, default=None if named_export else name, named=[name] if named_export else [], type_only=is_type, alias_list=alias_list)
    contents[str(source)] = (data, s.lstrip(b"\n"))
    # Every other importer.
    probe = (source.parent.name if source.stem == "index" else source.stem).encode()
    for f in project_files(root, graph, SUFFIXES):
        if f in (source, target):
            continue
        before = f.read_bytes()
        if probe not in before:
            continue
        after, c = _repoint_file(before, f, source, target, publics, name, alias_list)
        conflicts += c
        if after != before:
            contents[str(f)] = (before, after)
        f_tree = _parse(after, f)
        for n, kind in _specifiers(f_tree, after):
            if kind != "call":
                continue
            hit = resolve_spec(f.parent, _value(n, after)[1], alias_list)
            mock = _text(n.parent.parent.child_by_field_name("function"), after) not in ("require", "import")
            if hit is not None and hit.path == source and (after != before or not mock):
                mentions.append(Mention(str(f), n.start_point[0] + 1, _text(n.parent.parent, after)[:120], "dynamic"))
    if mentions:
        notes.append("Some require, import() or mock calls name the old module. Check them by hand.")
    subject = f"{_rel(root, source)}::{name}"
    return MovePlan(f"{subject} -> {_rel(root, target)}", str(source), str(target), contents, tuple(dict.fromkeys(conflicts)), tuple(notes), tuple(mentions), ((subject, f"{_rel(root, target)}::{name}"),))


def _repoint_file(data: bytes, path: Path, source: Path, target: Path, publics, name: str, alias_list, local: bool = False):
    """Point the imports and re-exports of the moved declaration in ``path`` at ``target``.

    With ``local``, ``path`` is the target itself: its imports of the declaration go away.
    """
    tree = _parse(data, path)
    conflicts, edits, adds = [], [], []
    semi = _semi(tree, data)
    bound = set(_tops(tree, data)[0]) | {n for imp in _imports(tree, data) for n in [imp.default, imp.namespace, *(x.local for x in imp.named)] if n}
    for imp in _imports(tree, data):
        hit = resolve_spec(path.parent, imp.spec, alias_list)
        if hit is not None and hit.path == source:
            bound -= {n.local for n in imp.named if n.imported in publics and n.local == n.imported}
    for imp in _imports(tree, data):
        hit = resolve_spec(path.parent, imp.spec, alias_list)
        if hit is None or hit.path != source:
            continue
        moved = [n for n in imp.named if n.imported in publics]
        default = imp.default if "default" in publics else None
        for n in moved:
            if n.imported == "default":
                default = default or n.local
        moved_named = [n for n in moved if n.imported != "default"]
        rewritten = 0
        if imp.namespace:
            for m in _walk(tree.root_node):
                if m.type == "member_expression":
                    obj, prop = m.child_by_field_name("object"), m.child_by_field_name("property")
                elif m.type == "nested_type_identifier":
                    obj, prop = m.child_by_field_name("module"), m.child_by_field_name("name")
                else:
                    continue
                if obj is None or prop is None or _text(obj, data) != imp.namespace or _text(prop, data) not in publics - {"default"}:
                    continue
                public = _text(prop, data)
                if public in bound and not local:
                    conflicts.append(f"{_rel_any(path)} binds `{public}` already, so `{imp.namespace}.{public}` cannot follow the move.")
                    continue
                edits.append((m.start_byte, m.end_byte, name if local else public))
                rewritten += 1
                if not local:
                    adds.append((None, [public], imp.type_only, hit))
        uses = sum(1 for m in _walk(tree.root_node, ("import_statement",)) if m.type == "identifier" and _text(m, data) == imp.namespace) if rewritten else 0
        namespace = None if rewritten and rewritten == uses else imp.namespace
        if not moved and not default:
            if namespace != imp.namespace:
                edits.append(_reimport(data, imp, imp.default, None, [n.text for n in imp.named], semi))
            continue
        if local and any(n.local != name for n in moved_named):
            conflicts.append(f"{_rel_any(path)} imports `{name}` under another name.")
        keep_named = [n.text for n in imp.named if n not in moved]
        if not local and not keep_named and not namespace and (default or not imp.default):
            new_spec = spell(path.parent, target, _hit_for(target, hit.style, hit.alias, hit.ext if hit.style == "swap" else None))
            edits.append((imp.source.start_byte, imp.source.end_byte, imp.quote + new_spec + imp.quote))
            continue
        edits.append(_reimport(data, imp, None if default else imp.default, namespace, keep_named, semi))
        if not local:
            adds.append((default, [n.text for n in moved_named], imp.type_only, hit))
    for stmt in tree.root_node.named_children:
        src = stmt.child_by_field_name("source") if stmt.type == "export_statement" else None
        if src is None or src.type != "string":
            continue
        quote, spec = _value(src, data)
        hit = resolve_spec(path.parent, spec, alias_list)
        if hit is None or hit.path != source or local:
            continue
        new_spec = spell(path.parent, target, _hit_for(target, hit.style, hit.alias, hit.ext if hit.style == "swap" else None))
        clause = next((c for c in stmt.named_children if c.type == "export_clause"), None)
        end = ";" if _text(stmt, data).rstrip().endswith(";") else ""
        if clause is None:
            if any(c.type == "namespace_export" for c in stmt.named_children):
                continue
            names = sorted(publics - {"default"} - _reexported(tree, data))
            if names:
                s, e = _line_span(data, stmt)
                edits.append((e, e, f"export {{ {', '.join(names)} }} from {quote}{new_spec}{quote}{end}\n"))
            continue
        specs = [c for c in clause.named_children if c.type == "export_specifier" and _text(c.child_by_field_name("name"), data) in publics]
        if specs and len(specs) == len([c for c in clause.named_children if c.type == "export_specifier"]):
            edits.append((src.start_byte, src.end_byte, quote + new_spec + quote))
            continue
        if not specs:
            continue
        line = f"export {{ {', '.join(_text(c, data) for c in specs)} }} from {quote}{new_spec}{quote}{end}"
        rest = [c for c in clause.named_children if c.type == "export_specifier" and c not in specs]
        if not rest:
            edits.append((stmt.start_byte, stmt.end_byte, line))
        else:
            edits.append((clause.start_byte, clause.end_byte, _braces(data, clause, [_text(c, data) for c in rest])))
            s, e = _line_span(data, stmt)
            edits.append((e, e, line + "\n"))
    out = replace_ranges(data, edits)
    seen: set[str] = set()
    for default, named, type_only, hit in adds:
        if default in seen:
            default = None
        named = [n for n in named if n.split(" as ")[-1] not in seen]
        seen |= {n.split(" as ")[-1] for n in named} | ({default} if default else set())
        if not default and not named:
            continue
        spec = spell(path.parent, target, _hit_for(target, hit.style, hit.alias, hit.ext if hit.style == "swap" else None))
        out = add_import(out, path, target, spec, default=default, named=named, type_only=type_only, alias_list=alias_list)
    return out, conflicts


def _reexported(tree, data: bytes) -> set[str]:
    out = set()
    for stmt in tree.root_node.named_children:
        clause = next((c for c in stmt.named_children if c.type == "export_clause"), None) if stmt.type == "export_statement" else None
        for c in clause.named_children if clause is not None else ():
            if c.type == "export_specifier":
                out.add(_text(c.child_by_field_name("alias") or c.child_by_field_name("name"), data))
    return out


def _rel_any(path: Path) -> str:
    return str(path)
