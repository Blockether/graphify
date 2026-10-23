"""Python moves: move a module file, or move a top-level function, class or constant.

Imports follow the move: ``import m``, ``from m import x``, relative imports and
strings such as ``mock.patch("m.x")`` that spell the full dotted path.
"""
from __future__ import annotations

import re
from pathlib import Path

from graphify.moves import MovePlan, project_files, replace_ranges, resolve_path
from graphify.refactor import Mention, Unsupported, references, symbol_name

_PARSER = None
_IDENT = re.compile(r"^[A-Za-z_]\w*$")
_IMPORTS = ("import_statement", "import_from_statement", "future_import_statement")
_DEFS = ("function_definition", "class_definition", "decorated_definition")


def _parse(data: bytes):
    global _PARSER
    if _PARSER is None:
        import tree_sitter_python as tspy
        from tree_sitter import Language, Parser

        _PARSER = Parser(Language(tspy.language()))
    return _PARSER.parse(data)


def _text(node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _base(path: Path, root: Path) -> Path:
    d = path.parent
    while (d / "__init__.py").is_file() and d != root and d.parent != d:
        d = d.parent
    if d != path.parent:
        return d
    for b in (root / "src", root):
        if path.is_relative_to(b):
            return b
    return path.parent


def module_of(path: Path, root: Path) -> str:
    parts = list(path.relative_to(_base(path, root)).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def _absolute(node, data: bytes, package: str) -> str:
    """The absolute module of an import's ``module_name`` node."""
    text = _text(node, data)
    if node.type != "relative_import":
        return text
    dots = len(text) - len(text.lstrip("."))
    rest = text[dots:]
    parts = package.split(".") if package else []
    if dots > 1:
        parts = parts[: len(parts) - (dots - 1)]
    return ".".join([*parts, rest] if rest else parts)


def _spell(module: str, package: str, relative: bool) -> str:
    if relative and package and module.startswith(package + "."):
        return "." + module[len(package) + 1 :]
    return module


def _names(stmt):
    """``(name node, imported name, local name)`` of each name of a from-import."""
    out = []
    for c in stmt.children_by_field_name("name"):
        if c.type == "aliased_import":
            n = c.child_by_field_name("name")
            a = c.child_by_field_name("alias")
            out.append((c, n.text.decode(), a.text.decode()))
        else:
            out.append((c, c.text.decode(), c.text.decode().rsplit(".", 1)[-1]))
    return out


def _remove_name(data: bytes, stmt, node) -> tuple[int, int, str]:
    """The edit that drops one name from a from-import, or the whole statement."""
    names = [c for c, _, _ in _names(stmt)]
    if len(names) == 1:
        return _line_span(data, stmt) + ("",)
    i = next(k for k, c in enumerate(names) if c.id == node.id)
    if i + 1 < len(names):
        return node.start_byte, names[i + 1].start_byte, ""
    return names[i - 1].end_byte, node.end_byte, ""


def _line_span(data: bytes, node) -> tuple[int, int]:
    start = data.rfind(b"\n", 0, node.start_byte) + 1
    end = data.find(b"\n", node.end_byte)
    return start, (len(data) if end < 0 else end + 1)


def _insert_at(tree, data: bytes) -> int:
    """The byte after the last top-level import, or after the docstring."""
    at = 0
    for top in tree.root_node.children:
        if top.type in _IMPORTS:
            at = _line_span(data, top)[1]
        elif at == 0 and top.type == "expression_statement" and top.child_count and top.children[0].type == "string":
            at = _line_span(data, top)[1]
        elif top.type not in ("comment",) and at:
            break
    return at


def _add_import(data: bytes, module: str, name: str | None, alias: str | None = None) -> bytes:
    """Add ``from module import name`` or ``import module``, merged into an existing from-import."""
    tree = _parse(data)
    local = alias or name
    for top in tree.root_node.children:
        if top.type == "import_from_statement" and name:
            mod = top.child_by_field_name("module_name")
            if mod is not None and _text(mod, data) == module and not any(c.type == "wildcard_import" for c in top.children):
                names = _names(top)
                if any(loc == local for _, _, loc in names):
                    return data
                last = names[-1][0]
                piece = f"{name} as {alias}" if alias else name
                return data[: last.end_byte] + f", {piece}".encode() + data[last.end_byte :]
        if top.type == "import_statement" and not name:
            if any(_text(c, data) == module for c in top.children_by_field_name("name")):
                return data
    line = (f"from {module} import {name}" + (f" as {alias}" if alias else "")) if name else f"import {module}"
    at = _insert_at(tree, data)
    if at == 0:
        return f"{line}\n\n\n".encode() + data.lstrip(b"\n") if data.strip() else f"{line}\n".encode()
    return data[:at] + f"{line}\n".encode() + data[at:]


def _strings(data: bytes, old: str, new: str, exact_only: bool = False) -> list:
    """Edits of string literals whose whole content is ``old`` or starts with ``old.``."""
    edits = []
    for n in _walk(_parse(data).root_node):
        if n.type == "string_content":
            text = _text(n, data)
            if text == old or (not exact_only and text.startswith(old + ".")):
                edits.append((n.start_byte, n.start_byte + len(old.encode()), new))
    return edits


def _module_edits(data: bytes, path: Path, root: Path, old: str, new: str, moved: bool) -> tuple[list, int]:
    """Edits that point the imports of ``path`` from module ``old`` to ``new``."""
    tree = _parse(data)
    package = _package(module_of(path, root), path)
    old_pkg, _, old_leaf = old.rpartition(".")
    new_pkg, _, new_leaf = new.rpartition(".")
    edits, strings = [], 0
    for n in _walk(tree.root_node):
        if n.type == "import_statement":
            for c in n.children_by_field_name("name"):
                target = c.child_by_field_name("name") if c.type == "aliased_import" else c
                if _text(target, data) == old:
                    edits.append((target.start_byte, target.end_byte, new))
        elif n.type == "import_from_statement":
            mod = n.child_by_field_name("module_name")
            if mod is None:
                continue
            absolute = _absolute(mod, data, package)
            relative = mod.type == "relative_import"
            if absolute == old:
                edits.append((mod.start_byte, mod.end_byte, _spell(new, package, relative)))
            elif absolute == old_pkg:
                for c, name, local in _names(n):
                    if name != old_leaf:
                        continue
                    piece = new_leaf if local == new_leaf else f"{new_leaf} as {local}"
                    line = f"from {_spell(new_pkg, package, relative) or '.'} import {piece}"
                    if len(_names(n)) == 1:
                        edits.append((n.start_byte, n.end_byte, line))
                    else:
                        edits.append(_remove_name(data, n, c))
                        at = _line_span(data, n)[1]
                        edits.append((at, at, line + "\n"))
            elif moved and relative:
                edits.append((mod.start_byte, mod.end_byte, absolute))
        elif n.type == "attribute" and _text(n.child_by_field_name("object"), data) == old:
            obj = n.child_by_field_name("object")
            edits.append((obj.start_byte, obj.end_byte, new))
    for e in _strings(data, old, new):
        edits.append(e)
        strings += 1
    return _dedupe(edits), strings


def _dedupe(edits):
    out, seen = [], set()
    for e in sorted(edits):
        if any(a < e[1] and e[0] < b for a, b in seen if (a, b) != (e[0], e[1])):
            continue
        if (e[0], e[1]) in seen and e[0] != e[1]:
            continue
        seen.add((e[0], e[1]))
        out.append(e)
    return out


def _target_module(new: str, old: Path, root: Path) -> tuple[str, Path]:
    if new.endswith((".py", ".pyi")) or "/" in new:
        path = resolve_path(root, new)
        return None, path
    base = _base(old, root)
    return new, base / (new.replace(".", "/") + ".py")


def plan_move_file(graph: dict, old: Path, new: str, root: Path, tests: bool = True) -> MovePlan:
    if old.name == "__init__.py":
        raise Unsupported("Moving a package `__init__.py` moves a whole package; move its modules one by one.")
    old_mod = module_of(old, root)
    module, path = _target_module(new, old, root)
    conflicts, notes, created = [], [], {}
    if path.exists():
        conflicts.append(f"{path} already exists.")
    if old.parent != path.parent and (old.parent / "__init__.py").is_file():
        d = path.parent
        while d != root and not (d / "__init__.py").exists() and d.is_relative_to(_base(old, root)) and d != _base(old, root):
            created[str(d / "__init__.py")] = (None, b"")
            d = d.parent
    new_mod = module or _module_after(path, root, set(created))
    if not new_mod or not all(_IDENT.match(p) for p in new_mod.split(".")):
        conflicts.append(f"`{new_mod}` is not a valid module name.")
    contents: dict = {}
    strings = 0
    leaf = old_mod.rsplit(".", 1)[-1].encode()
    for f in project_files(root, graph, (".py", ".pyi")):
        data = f.read_bytes()
        if leaf not in data and f != old:
            continue
        edits, s = _module_edits(data, f, root, old_mod, new_mod, f == old)
        strings += s
        after = replace_ranges(data, edits)
        if after != data:
            contents[str(f)] = (data, after)
    before = old.read_bytes()
    contents[str(path)] = (None, contents.get(str(old), (before, before))[1])
    contents[str(old)] = (before, None)
    contents.update(created)
    if strings:
        notes.append(f"{strings} strings that spell `{old_mod}`, such as mock.patch targets, follow the module.")
    if created:
        notes.append("Created package files: " + ", ".join(sorted(created)) + ".")
    mentions = _mentions(contents, old_mod)
    return MovePlan(f"{old_mod} -> {new_mod}", str(old), str(path), contents, tuple(conflicts), tuple(notes), tuple(mentions), ((old_mod, new_mod),))


def _module_after(path: Path, root: Path, created) -> str:
    d = path.parent
    while ((d / "__init__.py").is_file() or str(d / "__init__.py") in created) and d != root:
        d = d.parent
    if d == path.parent:
        for b in (root / "src", root):
            if path.is_relative_to(b):
                d = b
                break
    return ".".join(path.relative_to(d).with_suffix("").parts)


def _mentions(contents: dict, old: str) -> list[Mention]:
    out = []
    pat = re.compile(r"(?<![\w.])" + re.escape(old) + r"(?![\w])")
    for path, (_, new) in contents.items():
        if not new:
            continue
        for i, line in enumerate(new.decode("utf-8", "replace").splitlines(), 1):
            if pat.search(line):
                out.append(Mention(path, i, line.strip()[:120], "text"))
    return out


# ---------------------------------------------------------------- definition move


def _definition(tree, data: bytes, name: str):
    for top in tree.root_node.children:
        node = top
        if top.type == "decorated_definition":
            node = top.child_by_field_name("definition")
        if node is not None and node.type in ("function_definition", "class_definition"):
            n = node.child_by_field_name("name")
            if n is not None and _text(n, data) == name:
                return top
        if top.type == "expression_statement" and top.child_count and top.children[0].type == "assignment":
            left = top.children[0].child_by_field_name("left")
            if left is not None and left.type == "identifier" and _text(left, data) == name:
                return top
    return None


def _span(data: bytes, node) -> tuple[int, int]:
    start = node.start_byte
    prev = node.prev_sibling
    while prev is not None and prev.type == "comment" and data[prev.end_byte : start].count(b"\n") <= 1:
        start = prev.start_byte
        prev = prev.prev_sibling
    return _line_span(data, node)[0] if start == node.start_byte else data.rfind(b"\n", 0, start) + 1, _line_span(data, node)[1]


def _cut(data: bytes, start: int, end: int) -> bytes:
    head, tail = data[:start].rstrip(b"\n"), data[end:].lstrip(b"\n")
    if not head.strip():
        return tail
    if not tail.strip():
        return head + b"\n"
    return head + b"\n\n\n" + tail


def _top_names(tree, data: bytes) -> dict[str, object]:
    """Top-level names of a module: definitions, assignments and imports -> the binding node."""
    out = {}
    for top in tree.root_node.children:
        if top.type in _IMPORTS:
            if top.type == "import_from_statement":
                for _, _, local in _names(top):
                    out[local] = top
            elif top.type == "import_statement":
                for c in top.children_by_field_name("name"):
                    if c.type == "aliased_import":
                        out[_text(c.child_by_field_name("alias"), data)] = top
                    else:
                        out[_text(c, data).split(".")[0]] = top
        else:
            d = _definition_name(top, data)
            if d:
                out[d] = top
    return out


def _definition_name(top, data: bytes) -> str | None:
    node = top.child_by_field_name("definition") if top.type == "decorated_definition" else top
    if node is not None and node.type in ("function_definition", "class_definition"):
        n = node.child_by_field_name("name")
        return _text(n, data) if n is not None else None
    if top.type == "expression_statement" and top.child_count and top.children[0].type == "assignment":
        left = top.children[0].child_by_field_name("left")
        if left is not None and left.type == "identifier":
            return _text(left, data)
    return None


def _free_names(node, data: bytes) -> set[str]:
    """Identifiers that the code reads, without attribute names, keyword names and its own bindings."""
    bound, used = set(), set()
    for n in _walk(node):
        if n.type != "identifier":
            continue
        parent = n.parent
        text = _text(n, data)
        if parent.type == "attribute" and parent.child_by_field_name("attribute") is not None and parent.child_by_field_name("attribute").id == n.id:
            continue
        if parent.type == "keyword_argument" and parent.child_by_field_name("name") is not None and parent.child_by_field_name("name").id == n.id:
            continue
        if parent.type in ("parameters", "default_parameter", "typed_parameter", "typed_default_parameter", "lambda_parameters", "list_splat_pattern", "dictionary_splat_pattern"):
            bound.add(text)
            continue
        if parent.type in ("function_definition", "class_definition") and parent.child_by_field_name("name").id == n.id:
            bound.add(text)
            continue
        if parent.type in ("assignment", "for_statement", "for_in_clause", "as_pattern_target", "augmented_assignment") and parent.child_by_field_name("left") is not None and parent.child_by_field_name("left").id == n.id:
            bound.add(text)
        used.add(text)
    return used - bound


def plan_move(graph: dict, node: dict, source: Path, to: str, root: Path) -> MovePlan:
    name = symbol_name(str(node.get("label", "")))
    module_old = module_of(source, root)
    tgt_module, target = _target_module(to, source, root)
    if tgt_module is None:
        tgt_module = _module_after(target, root, set())
    data = source.read_bytes()
    tree = _parse(data)
    top = _definition(tree, data, name)
    if top is None:
        raise Unsupported(f"`{name}` is not a top-level function, class or constant of {source}.")
    start, end = _span(data, top)
    conflicts, notes = [], []
    if target == source:
        conflicts.append(f"`{name}` is already in {source}.")
    sites = references(graph, node["id"], root)
    by_file: dict[str, list] = {}
    for s in sites:
        by_file.setdefault(str(resolve_path(root, s.file)), []).append(s)
    src_key, tgt_key = str(source), str(target)
    remaining = [s for s in by_file.get(src_key, ()) if not (start <= s.start < end) and s.role != "import"]
    # What the moved code needs.
    tops = _top_names(tree, data)
    free = _free_names(top, data) - {name}
    need_imports = sorted({t for t in free if t in tops and tops[t].type in _IMPORTS}, key=lambda t: (tops[t].start_byte, t))
    need_source = sorted({t for t in free if t in tops and tops[t].type not in _IMPORTS})
    if need_source and remaining:
        conflicts.append(f"`{name}` uses {', '.join(need_source)} of `{module_old}`, and `{module_old}` still uses `{name}`: a circular import. Move {', '.join(need_source)} first.")
    tgt_exists = target.is_file()
    tgt_data = target.read_bytes() if tgt_exists else b""
    if tgt_exists and name in _top_names(_parse(tgt_data), tgt_data):
        own = _top_names(_parse(tgt_data), tgt_data)[name]
        if own.type not in _IMPORTS:
            conflicts.append(f"{target} already defines `{name}`.")
    contents: dict = {}
    strings = 0
    for path, occs in sorted(by_file.items()):
        if path in (src_key, tgt_key):
            continue
        before = Path(path).read_bytes()
        after, s = _repoint(before, Path(path), root, occs, module_old, tgt_module, name)
        strings += s
        if after != before:
            contents[path] = (before, after)
    # Strings in files without code sites, such as mock.patch targets in tests.
    dotted = f"{module_old}.{name}"
    for f in project_files(root, graph, (".py",)):
        if str(f) in contents or str(f) in (src_key, tgt_key) or dotted.encode() not in f.read_bytes():
            continue
        before = f.read_bytes()
        after = replace_ranges(before, _strings(before, dotted, f"{tgt_module}.{name}", exact_only=True))
        if after != before:
            strings += 1
            contents[str(f)] = (before, after)
    # The moved code and its imports.
    code = data[start:end].strip(b"\n") + b"\n"
    import_lines = []
    future = [t for t in tree.root_node.children if t.type == "future_import_statement"]
    for t in future:
        import_lines.append(_text(t, data))
    for local in need_imports:
        stmt = tops[local]
        import_lines.append(_import_line(stmt, data, local, _package(module_old, source)))
    if need_source:
        import_lines.append(f"from {module_old} import {', '.join(need_source)}")
    if tgt_exists:
        t = tgt_data
        t = _drop_self(t, target, root, module_old, name)
        for line in import_lines:
            t = _merge_line(t, line)
        t = t.rstrip(b"\n") + b"\n\n\n" + code
        contents[tgt_key] = (tgt_data, t)
    else:
        head = "\n".join(dict.fromkeys(import_lines))
        contents[tgt_key] = (None, (head + "\n\n\n" if head else "").encode() + code)
        notes.append(f"Created {target}.")
        d = target.parent
        while (source.parent / "__init__.py").is_file() and not d.exists() and d != root:
            contents[str(d / "__init__.py")] = (None, b"")
            d = d.parent
    # The source module.
    s = _cut(data, start, end)
    leftover = _parse(s)
    used_after = _free_names(leftover.root_node, s)
    for local in need_imports:
        if local not in used_after or _only_in_imports(leftover, s, local):
            s = _drop_import(s, local)
    if remaining:
        s = _add_import(s, tgt_module, name)
    contents[src_key] = (data, s.lstrip(b"\n"))
    if strings:
        notes.append(f"{strings} strings that spell `{dotted}`, such as mock.patch targets, follow `{name}`.")
    mentions = [m for m in _mentions({k: v for k, v in contents.items()}, dotted)]
    return MovePlan(f"{module_old}.{name} -> {tgt_module}.{name}", src_key, tgt_key, contents, tuple(conflicts), tuple(notes), tuple(mentions), ((dotted, f"{tgt_module}.{name}"),))


def _only_in_imports(tree, data: bytes, local: str) -> bool:
    for n in _walk(tree.root_node):
        if n.type == "identifier" and _text(n, data) == local:
            p = n
            while p is not None and p.type not in _IMPORTS:
                p = p.parent
            if p is None:
                return False
    return True


def _import_line(stmt, data: bytes, local: str, package: str) -> str:
    if stmt.type == "import_from_statement":
        mod = stmt.child_by_field_name("module_name")
        module = _absolute(mod, data, package)
        for _, name, loc in _names(stmt):
            if loc == local:
                return f"from {module} import {name}" + (f" as {loc}" if loc != name.rsplit('.', 1)[-1] else "")
    for c in stmt.children_by_field_name("name"):
        if c.type == "aliased_import" and _text(c.child_by_field_name("alias"), data) == local:
            return f"import {_text(c.child_by_field_name('name'), data)} as {local}"
        if c.type != "aliased_import" and _text(c, data).split(".")[0] == local:
            return f"import {_text(c, data)}"
    return _text(stmt, data)


def _merge_line(data: bytes, line: str) -> bytes:
    m = re.match(r"from (\S+) import (.+)$", line)
    if m:
        for piece in m.group(2).split(", "):
            name, _, alias = piece.partition(" as ")
            data = _add_import(data, m.group(1), name, alias or None)
        return data
    if line.startswith("import ") and " as " not in line:
        return _add_import(data, line[7:], None)
    if line.encode() in data:
        return data
    at = _insert_at(_parse(data), data)
    return data[:at] + (line + "\n").encode() + data[at:]


def _drop_import(data: bytes, local: str) -> bytes:
    tree = _parse(data)
    for top in tree.root_node.children:
        if top.type == "import_from_statement":
            for c, _, loc in _names(top):
                if loc == local:
                    return replace_ranges(data, [_remove_name(data, top, c)])
        elif top.type == "import_statement":
            names = top.children_by_field_name("name")
            for c in names:
                loc = _text(c.child_by_field_name("alias"), data) if c.type == "aliased_import" else _text(c, data).split(".")[0]
                if loc == local and len(names) == 1:
                    return replace_ranges(data, [(*_line_span(data, top), "")])
    return data


def _drop_self(data: bytes, path: Path, root: Path, module_old: str, name: str) -> bytes:
    """Remove the target's own import of the moved name, and qualify-free its module uses."""
    tree = _parse(data)
    package = _package(module_of(path, root), path)
    edits = []
    for top in tree.root_node.children:
        if top.type == "import_from_statement":
            mod = top.child_by_field_name("module_name")
            if mod is not None and _absolute(mod, data, package) == module_old:
                for c, n, loc in _names(top):
                    if n == name:
                        edits.append(_remove_name(data, top, c))
    return replace_ranges(data, edits)


def _repoint(data: bytes, path: Path, root: Path, occs, old: str, new: str, name: str) -> tuple[bytes, int]:
    """Point the sites of ``name`` in one file from module ``old`` to ``new``."""
    tree = _parse(data)
    package = _package(module_of(path, root), path)
    edits, adds, strings = [], [], 0
    aliases = {}
    for top in tree.root_node.children:
        if top.type == "import_statement":
            for c in top.children_by_field_name("name"):
                if c.type == "aliased_import" and _text(c.child_by_field_name("name"), data) == old:
                    aliases[_text(c.child_by_field_name("alias"), data)] = top
                elif c.type != "aliased_import" and _text(c, data) == old:
                    aliases[old] = top
        elif top.type == "import_from_statement":
            mod = top.child_by_field_name("module_name")
            if mod is not None and old.rpartition(".")[0] == _absolute(mod, data, package):
                for _, imported, local in _names(top):
                    if imported == old.rpartition(".")[2]:
                        aliases[local] = top
    starts = {o.start for o in occs}
    for n in _walk(tree.root_node):
        if n.type == "import_from_statement":
            mod = n.child_by_field_name("module_name")
            if mod is None or _absolute(mod, data, package) != old:
                continue
            for c, imported, local in _names(n):
                if imported == name:
                    edits.append(_remove_name(data, n, c))
                    adds.append((new, name, local if local != name else None))
        elif n.type == "attribute":
            obj, attr = n.child_by_field_name("object"), n.child_by_field_name("attribute")
            if attr is not None and attr.start_byte in starts and _text(obj, data) in aliases:
                edits.append((obj.start_byte, obj.end_byte, new))
                adds.append((new, None, None))
    dotted = f"{old}.{name}"
    for e in _strings(data, dotted, f"{new}.{name}", exact_only=True):
        edits.append(e)
        strings += 1
    out = replace_ranges(data, _dedupe(edits))
    for module, nm, alias in dict.fromkeys(adds):
        out = _add_import(out, module, nm, alias)
    for alias, _ in aliases.items():
        t = _parse(out)
        if _only_in_imports(t, out, alias.split(".")[0]) and alias != old.split(".")[0]:
            out = _drop_import(out, alias)
    return out, strings
