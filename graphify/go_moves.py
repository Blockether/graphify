"""Go moves: move a file, or move a top-level declaration to another file or package.

A Go package is a directory. A move inside one directory only moves code and its
imports. A move to another package qualifies the references (``q.Name``), points
the imports of other packages at the new package and moves a type's methods
along. ``gofmt`` formats every changed file when it is on the PATH.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from graphify.moves import MovePlan, project_files, replace_ranges, resolve_path
from graphify.refactor import Unsupported, symbol_name

_PARSER = None
_GROUPS = ("const_declaration", "var_declaration", "type_declaration")
_SPECS = ("const_spec", "var_spec", "type_spec", "type_alias")


def _parse(data: bytes):
    global _PARSER
    if _PARSER is None:
        import tree_sitter_go as tsgo
        from tree_sitter import Language, Parser

        _PARSER = Parser(Language(tsgo.language()))
    return _PARSER.parse(data)


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


def gofmt(data: bytes) -> bytes:
    """Format Go source with ``gofmt``. Return the input when gofmt is missing or fails."""
    exe = shutil.which("gofmt")
    if exe is None:
        return data
    try:
        done = subprocess.run([exe], input=data, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return data
    return done.stdout if done.returncode == 0 else data


def _module(d: Path) -> tuple[Path, str]:
    for p in (d, *d.parents):
        f = p / "go.mod"
        if f.is_file():
            m = re.search(r"^module\s+(\S+)", f.read_text(encoding="utf-8", errors="replace"), re.M)
            if m:
                return p, m.group(1).strip('"')
    raise Unsupported(f"No go.mod is above {d}.")


def _import_path(d: Path, mod_root: Path, mod_path: str) -> str:
    rel = d.relative_to(mod_root).as_posix()
    return mod_path if rel == "." else f"{mod_path}/{rel}"


def _package(data: bytes) -> str | None:
    for c in _parse(data).root_node.named_children:
        if c.type == "package_clause":
            return _text(c.named_children[0], data)
    return None


def _dir_package(d: Path, skip=()) -> str | None:
    tests = None
    for f in sorted(d.glob("*.go")) if d.is_dir() else ():
        if f in skip:
            continue
        name = _package(f.read_bytes())
        if name and not f.name.endswith("_test.go"):
            return name
        tests = tests or (name[:-5] if name and name.endswith("_test") else name)
    return tests


def _default_name(path: str) -> str:
    parts = path.split("/")
    last = parts[-1]
    if re.fullmatch(r"v\d+", last) and len(parts) > 1:
        last = parts[-2]
    last = re.sub(r"^go-|[-.]go$", "", last)
    return re.sub(r"\W", "_", last)


def _imports(tree, data: bytes) -> list[dict]:
    out = []
    for decl in tree.root_node.named_children:
        if decl.type != "import_declaration":
            continue
        for spec in _walk(decl):
            if spec.type == "import_spec":
                path = _text(spec.child_by_field_name("path"), data).strip('"`')
                name = spec.child_by_field_name("name")
                out.append({"spec": spec, "decl": decl, "path": path, "alias": _text(name, data) if name is not None else None})
    return out


def _local(imp: dict, names: dict) -> str:
    return imp["alias"] or names.get(imp["path"]) or _default_name(imp["path"])


def _units(tree, data: bytes):
    """``({name: (stmt, spec_or_None)}, {receiver_type: [method]})`` of a Go file."""
    units, methods = {}, {}
    for stmt in tree.root_node.named_children:
        if stmt.type == "function_declaration":
            units[_text(stmt.child_by_field_name("name"), data)] = (stmt, None)
        elif stmt.type == "method_declaration":
            recv = stmt.child_by_field_name("receiver")
            t = next((n for n in _walk(recv) if n.type == "type_identifier"), None)
            if t is not None:
                methods.setdefault(_text(t, data), []).append(stmt)
        elif stmt.type in _GROUPS:
            for spec in _walk(stmt):
                if spec.type in _SPECS:
                    for n in spec.children_by_field_name("name"):
                        units[_text(n, data)] = (stmt, spec)
    return units, methods


def _specs(stmt) -> list:
    return [s for s in _walk(stmt) if s.type in _SPECS]


def _span(data: bytes, node) -> tuple[int, int]:
    cur, prev = node, node.prev_named_sibling
    while prev is not None and prev.type == "comment" and data[prev.end_byte : cur.start_byte].count(b"\n") <= 1:
        cur, prev = prev, prev.prev_named_sibling
    start = data.rfind(b"\n", 0, cur.start_byte) + 1
    end = data.find(b"\n", node.end_byte)
    return start, len(data) if end < 0 else end + 1


def _bare_refs(tree, data: bytes, names, skip=()):
    """Unqualified references to package-level ``names``, outside the ``skip`` byte ranges."""
    for n in _walk(tree.root_node, ("import_declaration", "package_clause")):
        if n.type not in ("identifier", "type_identifier") or _text(n, data) not in names:
            continue
        if any(s <= n.start_byte < e for s, e in skip):
            continue
        p = n.parent
        if p is not None and p.type == "literal_element" and p.parent is not None and p.parent.type == "keyed_element" and p.parent.named_children[0] == p:
            continue
        if p is not None and p.type in ("function_declaration", "method_declaration", *_SPECS) and n in p.children_by_field_name("name"):
            continue
        yield n


def _qualified(tree, data: bytes, alias: str, names):
    """``alias.Name`` selectors and qualified types for ``names``: ``(node, name)``."""
    for n in _walk(tree.root_node, ("import_declaration",)):
        if n.type == "selector_expression":
            a, b = n.child_by_field_name("operand"), n.child_by_field_name("field")
        elif n.type == "qualified_type":
            a, b = n.child_by_field_name("package"), n.child_by_field_name("name")
        else:
            continue
        if a is not None and b is not None and a.type in ("identifier", "package_identifier") and _text(a, data) == alias and _text(b, data) in names:
            yield n, _text(b, data)


def _uses(tree, data: bytes, local: str) -> bool:
    return any(n.type in ("identifier", "package_identifier") and _text(n, data) == local for n in _walk(tree.root_node, ("import_declaration", "package_clause")))


def add_import(data: bytes, path: str, alias: str | None = None) -> bytes:
    tree = _parse(data)
    if any(i["path"] == path for i in _imports(tree, data)):
        return data
    line = (f"{alias} " if alias else "") + f'"{path}"'
    decls = [d for d in tree.root_node.named_children if d.type == "import_declaration"]
    for d in reversed(decls):
        lst = next((c for c in d.named_children if c.type == "import_spec_list"), None)
        if lst is not None:
            specs = [c for c in lst.named_children if c.type == "import_spec"]
            std = "." not in path.split("/")[0]
            same = [c for c in specs if ("." not in _text(c.child_by_field_name("path"), data).strip('"`').split("/")[0]) == std]
            if same:
                at = _span(data, same[-1])[1]
                return data[:at] + f"\t{line}\n".encode() + data[at:]
            close = lst.end_byte - 1
            gap = "\n" if specs else ""
            return data[:close] + f"{gap}\t{line}\n".encode() + data[close:]
    if decls:
        d = decls[-1]
        spec = next(c for c in d.named_children if c.type == "import_spec")
        gap = "" if ("." in path.split("/")[0]) == ("." in _text(spec.child_by_field_name("path"), data).strip('"`').split("/")[0]) else "\n"
        return data[: d.start_byte] + f"import (\n\t{_text(spec, data)}\n{gap}\t{line}\n)".encode() + data[d.end_byte :]
    pkg = next(c for c in tree.root_node.named_children if c.type == "package_clause")
    return data[: pkg.end_byte] + f"\n\nimport {line}".encode() + data[pkg.end_byte :]


def drop_import(data: bytes, path: str) -> bytes:
    tree = _parse(data)
    for i in _imports(tree, data):
        if i["path"] != path:
            continue
        lst = i["spec"].parent
        if lst.type == "import_spec_list" and len([c for c in lst.named_children if c.type == "import_spec"]) > 1:
            s, e = _span(data, i["spec"])
            return data[:s] + data[e:]
        s, e = _span(data, i["decl"])
        return data[:s] + data[e:]
    return data


def _drop_unused(data: bytes, keep=()) -> bytes:
    tree = _parse(data)
    for i in _imports(tree, data):
        local = _local(i, {})
        if i["alias"] in ("_", ".") or i["path"] in keep:
            continue
        if not _uses(tree, data, local):
            return _drop_unused(drop_import(data, i["path"]), keep)
    return data


def _drop_unused_of(data: bytes, candidates, names: dict) -> bytes:
    """Drop the imports of ``candidates`` paths that the file no longer uses."""
    tree = _parse(data)
    for i in _imports(tree, data):
        if i["path"] in candidates and i["alias"] not in ("_", ".") and not _uses(tree, data, _local(i, names)):
            return _drop_unused_of(drop_import(data, i["path"]), candidates, names)
    return data


def _names_of(paths, mod_root: Path, mod_path: str) -> dict:
    out = {}
    for p in paths:
        if p == mod_path or p.startswith(mod_path + "/"):
            d = mod_root / p[len(mod_path) :].lstrip("/")
            name = _dir_package(d)
            if name:
                out[p] = name
    return out


def _plan(graph: dict, root: Path, jobs, subject: str, renames, file_move: bool = False) -> MovePlan:
    """Move the ``names`` of each ``(source, names, target)`` job. All sources share one package, all targets another."""
    src_dir, t_dir = jobs[0][0].parent, jobs[0][2].parent
    sources = {j[0] for j in jobs}
    targets = {j[2] for j in jobs}
    mod_root, mod_path = _module(src_dir)
    p_name = _package(jobs[0][0].read_bytes()) or src_dir.name
    same = src_dir == t_dir
    q_name = p_name if same else (_dir_package(t_dir, skip=sources) or re.sub(r"\W", "_", t_dir.name))
    ip, iq = _import_path(src_dir, mod_root, mod_path), _import_path(t_dir, mod_root, mod_path)
    conflicts, notes = [], []
    p_files = [f.resolve() for f in sorted(src_dir.glob("*.go")) if _package(f.read_bytes()) == p_name]
    p_tops: set[str] = set()
    for f in p_files:
        p_tops |= set(_units(_parse(f.read_bytes()), f.read_bytes())[0])
    moved: dict[str, Path] = {}
    cuts: dict[Path, list] = {}
    pieces: dict[Path, list] = {}
    code_imports: dict[Path, dict] = {}
    for source, names, target in jobs:
        data = source.read_bytes()
        tree = _parse(data)
        units, methods = _units(tree, data)
        for name in names:
            if name not in units:
                raise Unsupported(f"`{name}` is not a top-level declaration of {source}.")
            moved[name] = source
    for source, names, target in jobs:
        data = source.read_bytes()
        tree = _parse(data)
        units, methods = _units(tree, data)
        imports = _imports(tree, data)
        locals_ = {_local(i, _names_of([i["path"]], mod_root, mod_path)): i for i in imports}
        seen: set[int] = set()
        for name in names:
            stmt, spec = units[name]
            whole = spec is not None and all(_text(n, data) in names for sp in _specs(stmt) for n in sp.children_by_field_name("name"))
            if whole and stmt.id in seen:
                continue
            seen.add(stmt.id)
            grouped = spec is not None and len(_specs(stmt)) > 1 and not whole
            if grouped and len(spec.children_by_field_name("name")) > 1:
                raise Unsupported(f"`{name}` shares one spec with other names. Split it first.")
            if grouped and stmt.type == "const_declaration" and b"iota" in data[stmt.start_byte : stmt.end_byte]:
                conflicts.append(f"`{name}` is in a const group with iota. Its value depends on its place in the group.")
            declared = [name] if spec is None else [_text(n, data) for sp in (_specs(stmt) if whole else [spec]) for n in sp.children_by_field_name("name")]
            items = [spec if grouped else stmt, *(m for nm in declared for m in methods.get(nm, ()))]
            if not same:
                for f in p_files:
                    if f != source and name in _units(_parse(f.read_bytes()), f.read_bytes())[1]:
                        conflicts.append(f"{_rel(root, f)} has methods of `{name}`. Methods stay in the package of their type: move them too.")
            for node in items:
                s, e = _span(data, node)
                cuts.setdefault(source, []).append((s, e))
                edits = []
                for ref in _bare_refs(_sub(tree, node), data, p_tops - set(moved)) if not same else ():
                    other = _text(ref, data)
                    if not other[0].isupper():
                        conflicts.append(f"`{name}` uses `{other}`, which is not exported from package {p_name}. Move or export `{other}` first.")
                    edits.append((ref.start_byte - s, ref.end_byte - s, f"{p_name}.{other}"))
                    code_imports.setdefault(target, {})[ip] = None
                for local, imp in locals_.items():
                    if imp["path"] == iq and not same:
                        for n, nm in _qualified(_sub(tree, node), data, local, _AnyName()):
                            edits.append((n.start_byte - s, n.end_byte - s, nm))
                    elif any(n.type in ("identifier", "package_identifier") and _text(n, data) == local and not (n.parent is not None and n.parent.type == "selector_expression" and n.parent.child_by_field_name("field") == n) for n in _walk(node)):
                        code_imports.setdefault(target, {})[imp["path"]] = imp["alias"]
                text = replace_ranges(data[s:e], edits).decode("utf-8", "replace").strip("\n")
                if grouped and node is spec:
                    keyword = _text(stmt.children[0], data)
                    lines = text.split("\n")
                    at = next(i for i, ln in enumerate(lines) if not ln.lstrip().startswith("//"))
                    lines[at] = keyword + " " + lines[at].lstrip()
                    text = "\n".join(ln.strip() if i <= at else ln for i, ln in enumerate(lines))
                pieces.setdefault(target, []).append(text)
    unexported = sorted(n for n in moved if not n[0].isupper())
    files = [f.resolve() for f in project_files(root, graph, (".go",)) if f.resolve().is_relative_to(mod_root)]
    files = sorted(set(files) | sources | {t for t in targets if t.is_file()})
    contents: dict = {}
    p_needs_q = False
    for f in files:
        data = f.read_bytes()
        tree = _parse(data)
        pkg = _package(data)
        edits = [(s, e, b"") for s, e in cuts.get(f, ())]
        needs_q = False
        if not same and f.parent == src_dir and pkg == p_name:
            for ref in _bare_refs(tree, data, set(moved), cuts.get(f, ())):
                nm = _text(ref, data)
                if nm in unexported:
                    conflicts.append(f"{_rel(root, f)} uses `{nm}`, which is not exported. Rename it to an exported name first.")
                edits.append((ref.start_byte, ref.end_byte, f"{q_name}.{nm}"))
                needs_q = p_needs_q = True
        elif not same:
            in_q = f.parent == t_dir and pkg == q_name
            names = _names_of([ip, iq], mod_root, mod_path)
            q_local = next((_local(i, names) for i in _imports(tree, data) if i["path"] == iq), q_name)
            for i in _imports(tree, data):
                if i["path"] != ip:
                    continue
                for n, nm in _qualified(tree, data, _local(i, names), set(moved)):
                    edits.append((n.start_byte, n.end_byte, nm if in_q else f"{q_local}.{nm}"))
                    needs_q = not in_q
        if f in targets and f.is_file():
            if set(moved) & set(_units(tree, data)[0]):
                conflicts.append(f"{_rel(root, f)} already declares {', '.join(sorted(set(moved) & set(_units(tree, data)[0])))}.")
        if not edits and f not in targets:
            continue
        after = replace_ranges(data, edits)
        if needs_q:
            after = add_import(after, iq, None if q_name == _default_name(iq) else q_name)
        names = _names_of([i["path"] for i in _imports(_parse(after), after)], mod_root, mod_path)
        after = _drop_unused_of(after, {ip} | ({i["path"] for i in _imports(tree, data)} if f in sources else set()), names)
        if f in targets:
            after = _append(after, f, pieces.get(f, ()), code_imports.get(f, {}))
        if f in sources and file_move and not _units(_parse(after), after)[0] and not any(_units(_parse(after), after)[1].values()):
            contents[str(f)] = (data, None)
            continue
        after = gofmt(after)
        if after != data:
            contents[str(f)] = (data, after)
    for t in sorted(targets):
        if not t.is_file():
            body = f"package {q_name}\n".encode()
            contents[str(t)] = (None, gofmt(_append(body, t, pieces.get(t, ()), code_imports.get(t, {}))))
            notes.append(f"Created {_rel(root, t)}.")
    if not same:
        if p_needs_q and any(ip in code_imports.get(t, {}) for t in targets):
            conflicts.append(f"Package {p_name} still uses the moved code, and the moved code uses package {p_name}: an import cycle.")
        q_files = [f for f in t_dir.glob("*.go") if f not in sources]
        if p_needs_q and any(i["path"] == ip for f in q_files for i in _imports(_parse(f.read_bytes()), f.read_bytes())):
            conflicts.append(f"Package {q_name} imports package {p_name}, and {p_name} would import {q_name}: an import cycle.")
    if shutil.which("gofmt") is None:
        notes.append("gofmt is not on the PATH. Format the changed files.")
    return MovePlan(subject, str(jobs[0][0]), str(jobs[0][2]), contents, tuple(dict.fromkeys(conflicts)), tuple(notes), (), tuple(renames))


class _AnyName:
    def __contains__(self, item) -> bool:
        return True


class _Sub:
    def __init__(self, node):
        self.root_node = node


def _sub(tree, node):
    return _Sub(node)


def _append(data: bytes, path: Path, pieces, imports: dict) -> bytes:
    out = data
    for p, alias in sorted(imports.items(), key=lambda kv: ("." in kv[0].split("/")[0], kv[0])):
        out = add_import(out, p, alias)
    if pieces:
        out = out.rstrip(b"\n") + b"\n\n" + "\n\n".join(pieces).encode() + b"\n"
    return out


def plan_move(graph: dict, node: dict, source: Path, to: str, root: Path) -> MovePlan:
    name = symbol_name(str(node.get("label", "")))
    target = resolve_path(root, to)
    if to.endswith("/") or target.is_dir():
        target = target / source.name
    elif target.suffix != ".go":
        target = target.with_suffix(".go")
    if target == source:
        raise Unsupported(f"`{name}` is already in {source}.")
    if target.name.endswith("_test.go") != source.name.endswith("_test.go"):
        raise Unsupported("Move test code only to a _test.go file, and other code only to other files.")
    subject = f"{_rel(root, source)}::{name}"
    return _plan(graph, root, [(source, [name], target)], f"{subject} -> {_rel(root, target)}", ((subject, f"{_rel(root, target)}::{name}"),))


def plan_move_file(graph: dict, old: Path, new: str, root: Path, tests: bool = True) -> MovePlan:
    target = resolve_path(root, new)
    if new.endswith("/") or target.is_dir():
        target = target / old.name
    elif target.suffix != ".go":
        target = target.with_suffix(".go")
    pairs = [(old, target)]
    companion = old.with_name(old.stem + "_test.go")
    if tests and not old.name.endswith("_test.go") and companion.is_file():
        pairs.append((companion.resolve(), target.with_name(target.stem + "_test.go")))
    conflicts = [f"{_rel(root, t)} already exists." for _, t in pairs if t.exists()]
    renames = tuple((_rel(root, a), _rel(root, b)) for a, b in pairs)
    subject = f"{_rel(root, old)} -> {_rel(root, target)}"
    if old.parent == target.parent or conflicts:
        contents = {}
        for a, b in pairs:
            data = a.read_bytes()
            contents[str(b)] = (None, data)
            contents[str(a)] = (data, None)
        return MovePlan(subject, str(old), str(target), contents, tuple(conflicts), (), (), renames)
    jobs = []
    external = []
    p_name = _package(old.read_bytes())
    for a, b in pairs:
        data = a.read_bytes()
        units, methods = _units(_parse(data), data)
        if _package(data) != p_name:
            external.append((a, b))
            continue
        orphan = sorted(t for t in methods if t not in units)
        if orphan:
            conflicts.append(f"{_rel(root, a)} has methods of {', '.join(orphan)}, which another file declares. Move them with their type.")
        jobs.append((a, list(units), b))
    plan = _plan(graph, root, jobs, subject, renames, file_move=True)
    contents = dict(plan.contents)
    notes = list(plan.notes)
    for a, b in external:
        data = contents.get(str(a), (a.read_bytes(), a.read_bytes()))[1] or a.read_bytes()
        q_name = _package(contents[str(b.with_name(target.name))][1]) if str(target) in contents else None
        q_name = q_name or re.sub(r"\W", "_", target.parent.name)
        data = re.sub(rb"^package\s+\w+", f"package {q_name}_test".encode(), data, count=1, flags=re.M)
        contents[str(b)] = (None, data)
        contents[str(a)] = (a.read_bytes(), None)
        notes.append(f"{_rel(root, a)} moves along as package {q_name}_test.")
    for src, _, _ in jobs:
        if str(src) in contents and contents[str(src)][1] is not None and src == old:
            conflicts.append(f"{_rel(root, old)} keeps code after the move.")
    return MovePlan(subject, str(old), str(target), contents, tuple(conflicts) + plan.conflicts, tuple(notes), (), renames)
