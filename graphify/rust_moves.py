"""Rust moves: move a module file, or move a top-level item to another module.

A Rust module is a file that its parent module declares with ``mod``. A file move
moves that declaration and the child module files. It points every ``crate::``,
``self::``, ``super::`` and crate-name path at the new module. An item move takes
the ``impl`` blocks of a moved type along and adds the ``use`` imports that the
item needs. Private items, fields and methods that the other module now uses
become ``pub(crate)``. ``rustfmt`` formats a changed file only when the file was
rustfmt-clean before the move.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from graphify.moves import MovePlan, project_files, replace_ranges, resolve_path
from graphify.refactor import Unsupported, symbol_name

_PARSER = None
_ITEMS = ("function_item", "function_signature_item", "struct_item", "enum_item", "union_item", "trait_item", "const_item", "static_item", "type_item")
_TYPES = ("struct_item", "enum_item", "union_item", "type_item")
_SEG = ("identifier", "type_identifier", "crate", "self", "super")
_SCOPED = ("scoped_identifier", "scoped_type_identifier")
_OPAQUE = ("line_comment", "block_comment", "string_literal", "raw_string_literal", "char_literal", "macro_definition")
_ATTACHED = ("attribute_item", "line_comment", "block_comment")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Traits whose methods a moved item can call without a bare mention of the trait.
_METHOD_TRAITS = frozenset({"Write", "Read", "BufRead", "Seek", "Hasher", "Borrow", "BorrowMut", "Itertools", "FutureExt", "StreamExt", "SinkExt", "TryFutureExt", "TryStreamExt", "AsyncReadExt", "AsyncWriteExt", "AsyncBufReadExt", "Context", "Rng", "SliceRandom", "Digest", "Buf", "BufMut", "FromStr", "ToString"})


def _parse(data: bytes):
    global _PARSER
    if _PARSER is None:
        import tree_sitter_rust as tsrust
        from tree_sitter import Language, Parser

        _PARSER = Parser(Language(tsrust.language()))
    return _PARSER.parse(data)


def _text(node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _rel(root: Path, path) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _walk(node, skip=()):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in skip:
            continue
        yield n
        stack.extend(reversed(n.children))


# --- Cargo packages and module trees ---------------------------------------------------------


def _toml(text: str) -> list:
    """Return ``(section, {key: value})`` entries. This reads only the simple keys that moves need."""
    out = [("", {})]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[\[?\s*([^\]]+?)\s*\]\]?$", line)
        if m:
            out.append((m.group(1), {}))
            continue
        m = re.match(r"^([\w.\-]+)\s*=\s*(.+?)\s*(#.*)?$", line)
        if m:
            value = m.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[-1][1][m.group(1)] = value
    return out


def _section(entries, name: str) -> dict:
    merged = {}
    for sec, values in entries:
        if sec == name:
            merged.update(values)
    return merged


@dataclass
class _Crate:
    root: Path
    name: str | None
    modules: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    decls: dict = field(default_factory=dict)
    path_attrs: set = field(default_factory=set)

    @property
    def dir(self) -> Path:
        return self.root.parent


@dataclass
class _Package:
    dir: Path
    edition: str
    lib: str | None
    crates: list


def _edition(entries, pkg_dir: Path) -> str:
    package = _section(entries, "package")
    value = package.get("edition", "")
    if value and "workspace" not in value:
        return value
    if value or package.get("edition.workspace") == "true":
        for d in [pkg_dir, *pkg_dir.parents]:
            manifest = d / "Cargo.toml"
            if manifest.is_file():
                ws = _section(_toml(manifest.read_text(encoding="utf-8", errors="replace")), "workspace.package")
                if ws.get("edition"):
                    return ws["edition"]
        return "2021"
    return "2015"


def _package(path: Path) -> _Package:
    for d in path.parents:
        manifest = d / "Cargo.toml"
        if not manifest.is_file():
            continue
        entries = _toml(manifest.read_text(encoding="utf-8", errors="replace"))
        package = _section(entries, "package")
        if not package.get("name"):
            continue
        edition = _edition(entries, d)
        if edition == "2015":
            raise Unsupported(f"{manifest} uses Rust 2015. Moves support Rust 2018 and later.")
        lib_entry = _section(entries, "lib")
        lib_path = (d / lib_entry.get("path", "src/lib.rs")).resolve()
        lib_name = lib_entry.get("name") or package["name"].replace("-", "_")
        roots = []
        if lib_path.is_file():
            roots.append((lib_path, lib_name))
        explicit = [(d / v["path"]).resolve() for sec, v in entries if sec in ("bin", "test", "example", "bench") and v.get("path")]
        found = [d / "src" / "main.rs"]
        for sub in ("src/bin", "tests", "examples", "benches"):
            found += sorted((d / sub).glob("*.rs")) + sorted((d / sub).glob("*/main.rs"))
        for p in explicit + found:
            p = p.resolve()
            if p.is_file() and p not in {r for r, _ in roots}:
                roots.append((p, None))
        crates = []
        taken = set()
        for root_file, name in roots:
            if root_file in taken:
                continue
            crate = _Crate(root_file, name)
            _load(crate, root_file, (), root_file.parent)
            taken |= set(crate.files)
            crates.append(crate)
        return _Package(d, edition, lib_name if lib_path.is_file() else None, crates)
    raise Unsupported(f"{path} is not inside a Cargo package.")


def _attached(node) -> list:
    """Return the attributes and comments directly above ``node``."""
    out = []
    top = node
    prev = node.prev_sibling
    while prev is not None and prev.type in _ATTACHED and prev.end_point[0] >= top.start_point[0] - 1:
        out.append(prev)
        top = prev
        prev = prev.prev_sibling
    return list(reversed(out))


def _load(crate: _Crate, file: Path, mod: tuple, mdir: Path) -> None:
    if file in crate.files:
        return
    crate.files[file] = mod
    crate.modules[mod] = file
    data = file.read_bytes()
    tree = _parse(data)

    def visit(container, mod, mdir):
        for ch in container.named_children:
            if ch.type != "mod_item":
                continue
            name_node = ch.child_by_field_name("name")
            if name_node is None:
                continue
            sub = mod + (_text(name_node, data),)
            body = ch.child_by_field_name("body")
            if body is not None:
                crate.modules.setdefault(sub, None)
                visit(body, sub, mdir / sub[-1])
                continue
            if any(a.type == "attribute_item" and re.search(r"#!?\[\s*path\b", _text(a, data)) for a in _attached(ch)):
                crate.path_attrs.add(sub)
                continue
            for cand in (mdir / f"{sub[-1]}.rs", mdir / sub[-1] / "mod.rs"):
                if cand.is_file():
                    crate.decls[sub] = file
                    _load(crate, cand.resolve(), sub, mdir / sub[-1])
                    break

    visit(tree.root_node, mod, mdir)


def _crate_of(pkg: _Package, file: Path) -> _Crate:
    for crate in pkg.crates:
        if file in crate.files:
            return crate
    raise Unsupported(f"{file} is not a module that a crate of {pkg.dir / 'Cargo.toml'} declares.")


def _module_for(crate: _Crate, path: Path) -> tuple:
    if path == crate.root:
        return ()
    if path in crate.files:
        return crate.files[path]
    try:
        rel = path.relative_to(crate.dir)
    except ValueError:
        raise Unsupported(f"{path} is outside the source directory {crate.dir} of its crate.") from None
    parts = rel.with_suffix("").parts
    if parts and parts[-1] == "mod":
        parts = parts[:-1]
    if not parts or not all(_IDENT.match(p) for p in parts):
        raise Unsupported(f"{path} is not a valid module file name.")
    mod = tuple(parts)
    if mod in crate.modules:
        where = crate.modules[mod]
        raise Unsupported(f"Module {'::'.join(mod)} is already {'inline' if where is None else 'in ' + str(where)}. Move to that file or choose another name.")
    return mod


# --- Paths and their resolution ---------------------------------------------------------------


@dataclass
class _Ctx:
    same: bool
    lib: str | None
    modules: set
    aliases: dict = field(default_factory=dict)
    renamed: dict = field(default_factory=dict)


def _head(names, scope: tuple, ctx: _Ctx):
    """Return ``(base, consumed, kind)`` for the module that the path head names, or None."""
    s0 = names[0]
    if ctx.same:
        if s0 == "crate":
            return (), 1, "crate"
        if s0 == "self":
            return scope, 1, "self"
        if s0 == "super":
            k = 0
            base = scope
            while k < len(names) and names[k] == "super":
                if not base:
                    return None
                base = base[:-1]
                k += 1
            return base, k, "super"
        if scope + (s0,) in ctx.modules:
            return scope + (s0,), 1, "bare"
    elif ctx.lib and s0 == ctx.lib:
        return (), 1, "lib"
    if s0 in ctx.aliases:
        return ctx.aliases[s0], 1, "alias"
    return None


def _map(path: tuple, mapping) -> tuple:
    for old, new in mapping:
        if path[: len(old)] == old:
            return new + path[len(old) :]
    return path


def _spell(path: tuple, ctx: _Ctx, kind: str = "crate", scope: tuple = (), supers: int = 0) -> str:
    if ctx.same:
        if kind == "self" and path[: len(scope)] == scope and len(path) > len(scope):
            return "::".join(("self",) + path[len(scope) :])
        if kind == "super" and supers and len(scope) >= supers:
            base = scope[:-supers]
            if path[: len(base)] == base and len(path) > len(base):
                return "::".join(("super",) * supers + path[len(base) :])
        if kind == "bare" and path[: len(scope)] == scope and len(path) > len(scope) and scope + (path[len(scope)],) in ctx.modules:
            return "::".join(path[len(scope) :])
        if kind == "alias":
            best = max(((k, v) for k, v in ctx.aliases.items() if path[: len(v)] == v), key=lambda kv: len(kv[1]), default=None)
            if best is not None:
                return "::".join((best[0],) + path[len(best[1]) :])
        return "::".join(("crate",) + path)
    if kind == "alias":
        best = max(((k, v) for k, v in ctx.aliases.items() if path[: len(v)] == v), key=lambda kv: len(kv[1]), default=None)
        if best is not None:
            return "::".join((best[0],) + path[len(best[1]) :])
    return "::".join((ctx.lib,) + path)


def _path_edit(names, scope_old, scope_new, ctx_old, ctx_new, mapping):
    """Return ``(count, new_path, spelling)`` when the first ``count`` segments need new text."""
    head = _head(names, scope_old, ctx_old)
    if head is None:
        return None
    base, k, kind = head
    n = len(names)
    i, new = n, None
    for j in range(k, n + 1):
        a = base + tuple(names[k:j])
        m = _map(a, mapping)
        if m != a:
            i, new = j, m
            break
    if new is None:
        new = base + tuple(names[k:])
    head2 = _head(names, scope_new, ctx_new)
    cur = None if head2 is None or head2[1] > i else head2[0] + tuple(names[head2[1] : i])
    if cur == new:
        return None
    spelled = _spell(new, ctx_new, kind, scope_new, k if kind == "super" else 0)
    return i, new, spelled


def _flat(node, data: bytes):
    if node is None:
        return None
    if node.type in _SEG:
        return [(_text(node, data), node.end_byte)]
    if node.type in _SCOPED:
        path = node.child_by_field_name("path")
        name = node.child_by_field_name("name")
        if path is None or name is None:
            return None
        head = _flat(path, data)
        return None if head is None else head + [(_text(name, data), name.end_byte)]
    return None


def _token_runs(tt, data: bytes):
    kids = tt.children
    i = 0
    while i < len(kids):
        c = kids[i]
        if c.type in _SEG and not (i > 0 and kids[i - 1].type in ("::", ".", "$")):
            segs = [(_text(c, data), c.end_byte)]
            j = i
            while j + 2 < len(kids) and kids[j + 1].type == "::" and kids[j + 2].type in _SEG:
                segs.append((_text(kids[j + 2], data), kids[j + 2].end_byte))
                j += 2
            if len(segs) > 1:
                yield c.start_byte, segs
            i = j + 1
        else:
            i += 1


def _scope_of(node, data: bytes) -> tuple:
    chain = []
    p = node.parent
    while p is not None:
        if p.type == "mod_item" and p.child_by_field_name("body") is not None:
            chain.append(_text(p.child_by_field_name("name"), data))
        p = p.parent
    return tuple(reversed(chain))


@dataclass
class _Leaf:
    segs: list
    kind: str
    alias: str | None
    node: object

    @property
    def local(self) -> str | None:
        if self.alias:
            return self.alias
        return self.segs[-1][0] if self.segs and self.kind != "glob" else None


def _leaves(node, data: bytes, prefix):
    t = node.type
    if t == "self" and prefix:
        yield _Leaf(list(prefix), "self", None, node)
    elif t in _SEG or t in _SCOPED:
        f = _flat(node, data)
        if f:
            yield _Leaf(list(prefix) + f, "path", None, node)
    elif t == "use_as_clause":
        path = node.child_by_field_name("path")
        alias = node.child_by_field_name("alias")
        if path is not None and path.type == "self" and prefix:
            yield _Leaf(list(prefix), "self", _text(alias, data), node)
        else:
            f = _flat(path, data)
            if f:
                yield _Leaf(list(prefix) + f, "path", _text(alias, data) if alias is not None else None, node)
    elif t == "use_wildcard":
        named = [c for c in node.named_children if c.type in _SEG or c.type in _SCOPED]
        f = _flat(named[0], data) if named else []
        if f is not None:
            yield _Leaf(list(prefix) + f, "glob", None, node)
    elif t == "scoped_use_list":
        path = node.child_by_field_name("path")
        f = _flat(path, data) if path is not None else []
        lst = node.child_by_field_name("list")
        if f is not None and lst is not None:
            for ch in lst.named_children:
                yield from _leaves(ch, data, list(prefix) + f)
    elif t == "use_list":
        for ch in node.named_children:
            yield from _leaves(ch, data, prefix)


def _uses(tree, data: bytes, top_only: bool = True):
    nodes = tree.root_node.named_children if top_only else [n for n in _walk(tree.root_node, _OPAQUE) if n.type == "use_declaration"]
    for n in nodes:
        if n.type == "use_declaration":
            arg = n.child_by_field_name("argument")
            if arg is not None:
                yield n, list(_leaves(arg, data, []))


def _aliases(tree, data: bytes, scope: tuple, ctx: _Ctx) -> dict:
    out = {}
    plain = _Ctx(ctx.same, ctx.lib, ctx.modules)
    for _, leaves in _uses(tree, data, top_only=False):
        for lf in leaves:
            if lf.kind == "glob" or not lf.local:
                continue
            names = [s for s, _ in lf.segs]
            head = _head(names, scope, plain)
            if head is not None:
                full = head[0] + tuple(names[head[1] :])
                if full in ctx.modules:
                    out[lf.local] = full
    return out


def _vis(node, data: bytes) -> str:
    for ch in node.children:
        if ch.type == "visibility_modifier":
            return _text(ch, data) + " "
    return ""


def _use_edits(decl, data, scope_old, scope_new, ctx_old, ctx_new, mapping):
    leaves = list(_leaves(decl.child_by_field_name("argument"), data, []))
    changed = {}
    dropped = set()
    for lf in leaves:
        names = [s for s, _ in lf.segs]
        e = _path_edit(names, scope_old, scope_new, ctx_old, ctx_new, mapping)
        if e is None:
            continue
        i, new, spelled = e
        full = new + tuple(names[i:])
        key = (lf.node.start_byte, lf.node.end_byte)
        if ctx_new.same and lf.kind != "glob" and full[:-1] == scope_new and lf.alias in (None, full[-1]):
            dropped.add(key)
        changed[key] = (lf, full, spelled + ("::" + "::".join(names[i:]) if names[i:] else ""))
    if not changed:
        return []
    arg = decl.child_by_field_name("argument")
    vis = _vis(decl, data)

    def line(lf, full, spelled):
        text = spelled
        if lf.kind == "glob":
            text += "::*"
        local = lf.segs[-1][0] if lf.segs else None
        if lf.alias:
            text += f" as {lf.alias}"
        elif lf.kind != "glob" and full and local and full[-1] != local and local not in ctx_new.renamed:
            text += f" as {local}"
        return f"{vis}use {text};"

    def gone(node):
        own = list(_leaves(node, data, []))
        return bool(own) and all((lf.node.start_byte, lf.node.end_byte) in changed for lf in own)

    indent = " " * decl.start_point[1]
    lines = [line(lf, full, sp) for key, (lf, full, sp) in changed.items() if key not in dropped]
    if len(changed) == len(leaves):
        if arg.type == "scoped_use_list" and not dropped:
            path = arg.child_by_field_name("path")
            p = len(_flat(path, data) or [])
            tails = {full[: len(full) - (len(lf.segs) - p)] if len(lf.segs) > p else full for lf, full, _ in changed.values()}
            suffixes_ok = all(full[len(full) - (len(lf.segs) - p) :] == tuple(s for s, _ in lf.segs[p:]) for lf, full, _ in changed.values())
            if len(tails) == 1 and suffixes_ok:
                new_prefix = tails.pop()
                first = next(iter(changed.values()))
                names = [s for s, _ in first[0].segs[:p]]
                head = _head(names, scope_old, ctx_old)
                kind = head[2] if head else "crate"
                edits = [(path.start_byte, path.end_byte, _spell(new_prefix, ctx_new, kind, scope_new, head[1] if head and kind == "super" else 0))]
                old_last = names[-1] if names else None
                for lf, full, _ in changed.values():
                    if lf.kind == "self" and not lf.alias and new_prefix and old_last and new_prefix[-1] != old_last and old_last not in ctx_new.renamed:
                        edits.append((lf.node.start_byte, lf.node.end_byte, f"self as {old_last}"))
                return edits
        return [(decl.start_byte, decl.end_byte, ("\n" + indent).join(lines))] if lines else [_line_cut(data, decl)]

    def render(node):
        return _render(node, data, gone)

    tail = "".join("\n" + indent + ln for ln in lines)
    return [(arg.start_byte, arg.end_byte, render(arg)), (decl.end_byte, decl.end_byte, tail)]


def _render(node, data: bytes, gone) -> str:
    """Return the text of a use tree without the elements for which ``gone`` is true."""
    if node.type == "use_list":
        parts = [_render(ch, data, gone) for ch in node.named_children if ch.type not in ("line_comment", "block_comment") and not gone(ch)]
        body = _text(node, data)
        if "\n" in body and node.named_children:
            inner = " " * node.named_children[0].start_point[1]
            return "{\n" + "".join(f"{inner}{p},\n" for p in parts) + " " * (node.end_point[1] - 1) + "}"
        return "{" + ", ".join(parts) + "}"
    if node.type == "scoped_use_list":
        lst = node.child_by_field_name("list")
        path = node.child_by_field_name("path")
        kept = [ch for ch in lst.named_children if ch.type not in ("line_comment", "block_comment") and not gone(ch)]
        if path is not None and len(kept) == 1 and kept[0].type in ("self", "use_as_clause") and _text(kept[0], data).split()[0] == "self":
            return _text(path, data) + _text(kept[0], data)[4:]
        return data[node.start_byte : lst.start_byte].decode("utf-8", "replace") + _render(lst, data, gone)
    return _text(node, data)


def _used(local: str, bare: set, methods: bool) -> bool:
    if local in bare:
        return True
    if local == "Write" and {"write!", "writeln!"} & bare:
        return True
    return local in _METHOD_TRAITS and local != "Write" and methods


def _prune(before, after: bytes) -> bytes:
    """Remove the imports that ``before`` used and ``after`` no longer uses."""
    if before is None or after is None:
        return after
    old_tree = _parse(before)
    old_bare = _bare_names(old_tree, before)
    old_methods = _calls_methods(old_tree, before, [(0, len(before))])
    tree = _parse(after)
    bare = _bare_names(tree, after)
    methods = _calls_methods(tree, after, [(0, len(after))])
    edits = []
    for decl, leaves in _uses(tree, after, top_only=False):
        if _vis(decl, after):
            continue
        dead = {(lf.node.start_byte, lf.node.end_byte) for lf in leaves if lf.kind != "glob" and lf.local and _used(lf.local, old_bare, old_methods) and not _used(lf.local, bare, methods)}
        if not dead:
            continue
        if len(dead) == len(leaves):
            edits.append(_line_cut(after, decl))
            continue

        def gone(node, dead=dead):
            own = list(_leaves(node, after, []))
            return bool(own) and all((lf.node.start_byte, lf.node.end_byte) in dead for lf in own)

        arg = decl.child_by_field_name("argument")
        edits.append((arg.start_byte, arg.end_byte, _render(arg, after, gone)))
    return replace_ranges(after, edits) if edits else after


def _line_cut(data: bytes, node):
    start = data.rfind(b"\n", 0, node.start_byte) + 1
    end = data.find(b"\n", node.end_byte)
    end = len(data) if end < 0 else end + 1
    if data[start : node.start_byte].strip() or data[node.end_byte : end].strip():
        return (node.start_byte, node.end_byte, "")
    return (start, end, "")


def _rewrite(data: bytes, tree, scope_old: tuple, scope_new: tuple, ctx_old: _Ctx, ctx_new: _Ctx, mapping, relocate=()) -> list:
    """Return the path edits of one file. ``relocate`` holds ``(start, end, scope)`` ranges of moved code."""
    edits = []

    def scopes(node):
        inner = _scope_of(node, data)
        for a, b, moved_scope in relocate:
            if a <= node.start_byte and node.end_byte <= b:
                return scope_old + inner, moved_scope + inner
        return scope_old + inner, scope_new + inner

    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        t = n.type
        if t in _OPAQUE:
            continue
        if t == "use_declaration":
            so, sn = scopes(n)
            edits.extend(_use_edits(n, data, so, sn, ctx_old, ctx_new, mapping))
            continue
        if t in _SCOPED:
            segs = _flat(n, data)
            if segs:
                so, sn = scopes(n)
                e = _path_edit([s for s, _ in segs], so, sn, ctx_old, ctx_new, mapping)
                if e is not None:
                    edits.append((n.start_byte, segs[e[0] - 1][1], e[2]))
                continue
        if t == "token_tree":
            so, sn = scopes(n)
            for start, segs in _token_runs(n, data):
                e = _path_edit([s for s, _ in segs], so, sn, ctx_old, ctx_new, mapping)
                if e is not None:
                    edits.append((start, segs[e[0] - 1][1], e[2]))
        stack.extend(n.children)
    return edits


# --- Formatting and insertion -----------------------------------------------------------------


def _rustfmt(data: bytes, edition: str):
    exe = shutil.which("rustfmt")
    if exe is None:
        return None
    try:
        done = subprocess.run([exe, "--edition", edition, "--emit", "stdout"], input=data, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _header_end(tree, data: bytes) -> int:
    end = 0
    for ch in tree.root_node.children:
        if ch.type == "inner_attribute_item" or (ch.type == "line_comment" and _text(ch, data).startswith("//!")) or (ch.type == "block_comment" and _text(ch, data).startswith("/*!")):
            end = ch.end_byte
        else:
            break
    return end


def _line_end(data: bytes, pos: int) -> int:
    nl = data.find(b"\n", pos)
    return len(data) if nl < 0 else nl + 1


def add_uses(data: bytes, lines) -> bytes:
    """Add top-level ``use`` lines that the file does not already have."""
    present = {re.sub(r"\s+", "", ln) for ln in data.decode("utf-8", "replace").splitlines()}
    new = [ln for ln in dict.fromkeys(lines) if re.sub(r"\s+", "", ln) not in present]
    if not new:
        return data
    tree = _parse(data)
    block = "".join(ln + "\n" for ln in new).encode()
    last = [ch for ch in tree.root_node.named_children if ch.type == "use_declaration"]
    if last:
        at = _line_end(data, last[-1].end_byte)
        return data[:at] + block + data[at:]
    head = _header_end(tree, data)
    if head:
        at = _line_end(data, head)
        rest = data[at:].lstrip(b"\n")
        return data[:at] + b"\n" + block + (b"\n" + rest if rest else b"")
    rest = data.lstrip(b"\n")
    return block + (b"\n" + rest if rest else b"")


def _add_mod(data: bytes, line: str) -> bytes:
    tree = _parse(data)
    kids = tree.root_node.named_children
    decls = [ch for ch in kids if ch.type == "mod_item" and ch.child_by_field_name("body") is None]
    if decls:
        at = _line_end(data, decls[-1].end_byte)
        return data[:at] + line.encode() + b"\n" + data[at:]
    uses = [ch for ch in kids if ch.type == "use_declaration"]
    anchor = uses[-1].end_byte if uses else _header_end(tree, data)
    if anchor:
        at = _line_end(data, anchor)
        rest = data[at:].lstrip(b"\n")
        return data[:at] + b"\n" + line.encode() + b"\n" + (b"\n" + rest if rest else b"")
    rest = data.lstrip(b"\n")
    return line.encode() + b"\n" + (b"\n" + rest if rest else b"")


def _span(data: bytes, node):
    first = (_attached(node) or [node])[0]
    start = data.rfind(b"\n", 0, first.start_byte) + 1
    end = _line_end(data, node.end_byte)
    if data[end : end + 1] == b"\n" and (start == 0 or data[max(0, start - 2) : start] == b"\n\n"):
        end += 1
    return start, end


class _Files:
    """Old and new bytes of the files of one plan."""

    def __init__(self):
        self.old = {}
        self.new = {}

    def get(self, path: Path) -> bytes:
        if path in self.new:
            return self.new[path]
        if path.is_file():
            data = path.read_bytes()
            self.old.setdefault(path, data)
            return data
        self.old.setdefault(path, None)
        return b""

    def put(self, path: Path, data) -> None:
        self.get(path)
        self.new[path] = data

    def contents(self) -> dict:
        return {str(p): (self.old.get(p), new) for p, new in self.new.items() if self.old.get(p) != new}


def _declare(files: _Files, crate: _Crate, mod: tuple, line_of, created: list, made: dict) -> None:
    """Declare the new module ``mod`` in its parent. Create missing parent modules too."""
    parent = mod[:-1]
    parent_file = made.get(parent) or crate.modules.get(parent)
    if parent not in made and parent not in crate.modules:
        parent_file = crate.dir.joinpath(*parent).with_suffix(".rs")
        if parent_file.exists():
            raise Unsupported(f"{parent_file} exists but no module declares it.")
        made[parent] = parent_file
        files.put(parent_file, b"")
        created.append(parent)
        _declare(files, crate, parent, line_of, created, made)
    if parent_file is None:
        raise Unsupported(f"Module {'::'.join(parent)} is inline. Move to a module file.")
    files.put(parent_file, _add_mod(files.get(parent_file), line_of(mod)))


def _mod_line(vis: str, mod: tuple) -> str:
    if not vis and len(mod) > 1:
        vis = "pub(crate) "
    return f"{vis}mod {mod[-1]};"


def _format(files: _Files, edition: str, clean_hint: dict) -> list:
    notes = []
    if shutil.which("rustfmt") is None:
        return ["rustfmt is not on the PATH. Format the changed files."]
    for path, new in list(files.new.items()):
        if new is None:
            continue
        before = files.old.get(path)
        new = files.new[path] = _prune(before, new)
        clean = clean_hint.get(path)
        if clean is None:
            clean = before is not None and _rustfmt(before, edition) == before
        if clean:
            out = _rustfmt(new, edition)
            if out is not None:
                files.new[path] = out
    return notes


# --- Plans ------------------------------------------------------------------------------------


def _world(pkg: _Package, crate: _Crate, root: Path, graph: dict, mapping, created=()):
    """Yield ``(path, data, tree, scope, ctx_old, ctx_new)`` for each Rust file that can see the crate."""
    modules = set(crate.modules)
    new_modules = {_map(m, mapping) for m in modules} | set(created)
    files = set(project_files(root, graph, (".rs",)))
    for c in pkg.crates:
        files |= set(c.files)
    for path in sorted(files):
        same = path in crate.files
        if not same and not crate.name:
            continue
        data = path.read_bytes()
        if not same and crate.name.encode() not in data:
            continue
        scope = crate.files.get(path, ())
        ctx_old = _Ctx(same, crate.name, modules)
        tree = _parse(data)
        ctx_old.aliases = _aliases(tree, data, scope, ctx_old)
        ctx_new = _Ctx(same, crate.name, new_modules)
        bare = None
        for k, v in ctx_old.aliases.items():
            m = _map(v, mapping)
            if m != v and v and m and k == v[-1] and m[-1] != k and m[-1] not in ctx_old.aliases:
                bare = _bare_names(tree, data) if bare is None else bare
                if m[-1] not in bare and scope + (m[-1],) not in new_modules:
                    ctx_new.renamed[k] = m[-1]
                    ctx_new.aliases[m[-1]] = m
                    continue
            ctx_new.aliases[k] = m
        yield path, data, tree, scope, ctx_old, ctx_new


def _target(root: Path, to: str, source: Path) -> Path:
    target = resolve_path(root, to)
    if to.endswith("/") or target.is_dir():
        name = source.name if source.name != "mod.rs" else source.parent.name + ".rs"
        target = target / name
    elif target.suffix != ".rs":
        target = target.with_suffix(".rs")
    return target


def plan_move_file(graph: dict, old: Path, new: str, root: Path, tests: bool = True) -> MovePlan:
    """Move a module file, its child module files and its ``mod`` declaration.

    Unit tests are inline or child modules in Rust, so they always move along.
    Integration tests follow through the crate-name paths.
    """
    pkg = _package(old)
    crate = _crate_of(pkg, old)
    m_old = crate.files[old]
    if old == crate.root or not m_old:
        raise Unsupported(f"{_rel(root, old)} is a crate root. Move it in Cargo.toml.")
    target = _target(root, new, old)
    subject = f"{_rel(root, old)} -> {_rel(root, target)}"
    m_new = _module_for(crate, target)
    if m_new[: len(m_old)] == m_old:
        raise Unsupported(f"Module {'::'.join(m_old)} cannot move into itself.")
    conflicts = []
    if any(m[: len(m_old)] == m_old for m in crate.path_attrs):
        conflicts.append(f"A module under {'::'.join(m_old)} has a #[path] attribute. Move it by hand.")
    old_dir = crate.dir.joinpath(*m_old)
    new_dir = crate.dir.joinpath(*m_new)
    moved = {}
    for f, m in crate.files.items():
        if m[: len(m_old)] != m_old:
            continue
        if f == old:
            moved[f] = target
            continue
        try:
            moved[f] = new_dir / f.relative_to(old_dir)
        except ValueError:
            conflicts.append(f"{_rel(root, f)} is outside {_rel(root, old_dir)}. Move it by hand.")
    for a, b in moved.items():
        if b.exists():
            conflicts.append(f"{_rel(root, b)} already exists.")
    mapping = [(m_old, m_new)]
    files = _Files()
    notes = []

    parent_file = crate.decls[m_old]
    pdata = files.get(parent_file)
    ptree = _parse(pdata)
    decl = _find_decl(ptree, pdata, m_old[len(crate.files[parent_file]) :])
    if decl is None:
        raise Unsupported(f"No `mod {m_old[-1]};` declaration found in {_rel(root, parent_file)}.")
    vis = _vis(decl, pdata)
    if vis.startswith(("pub(super)", "pub(in", "pub(self)")):
        notes.append(f"`{vis.strip()}` became `pub(crate)` on the moved module declaration.")
        vis = "pub(crate) "
    attrs = "".join(_text(a, pdata) + "\n" for a in _attached(decl))

    created = []
    made = {}
    clean = {}
    for path, data, tree, scope, ctx_old, ctx_new in _world(pkg, crate, root, graph, mapping, [m_new]):
        new_scope = _map(scope, mapping) if path in moved else scope
        edits = _rewrite(data, tree, scope, new_scope, ctx_old, ctx_new, mapping)
        if path == parent_file and m_old[:-1] == m_new[:-1]:
            edits.append((decl.child_by_field_name("name").start_byte, decl.child_by_field_name("name").end_byte, m_new[-1]))
        elif path == parent_file:
            start, end = _span(data, decl)
            edits = [e for e in edits if not (start <= e[0] < end)] + [(start, end, "")]
        out = replace_ranges(data, edits) if edits else data
        if path in moved:
            files.get(path)
            files.put(path, None)
            files.old.setdefault(moved[path], None)
            files.new[moved[path]] = out
            clean[moved[path]] = _rustfmt(data, pkg.edition) == data
        elif out != data:
            files.get(path)
            files.put(path, out)
    if m_old[:-1] != m_new[:-1]:
        _declare(files, crate, m_new, lambda mod: (attrs if mod == m_new else "") + _mod_line(vis, mod), created, made)
    renames = tuple((_rel(root, a), _rel(root, b)) for a, b in moved.items())
    notes += _format(files, pkg.edition, clean)
    for mod in created:
        notes.append(f"Created module {'::'.join(mod)} in {_rel(root, made[mod])}.")
    if crate.name and vis.strip() == "pub":
        notes.append(f"The public path {crate.name}::{'::'.join(m_old)} is now {crate.name}::{'::'.join(m_new)}.")
    return MovePlan(subject, str(old), str(target), files.contents(), tuple(dict.fromkeys(conflicts)), tuple(notes), (), renames)


def _find_decl(tree, data: bytes, chain: tuple):
    node = tree.root_node
    for name in chain[:-1]:
        nxt = None
        for ch in node.named_children:
            if ch.type == "mod_item" and ch.child_by_field_name("body") is not None and _text(ch.child_by_field_name("name"), data) == name:
                nxt = ch.child_by_field_name("body")
        if nxt is None:
            return None
        node = nxt
    for ch in node.named_children:
        if ch.type == "mod_item" and ch.child_by_field_name("body") is None and _text(ch.child_by_field_name("name"), data) == chain[-1]:
            return ch
    return None


def _item_name(node, data: bytes):
    name = node.child_by_field_name("name")
    return _text(name, data) if name is not None else None


def _impl_of(node, data: bytes):
    t = node.child_by_field_name("type")
    while t is not None and t.type in ("generic_type", "reference_type"):
        t = t.child_by_field_name("type")
    return _text(t, data) if t is not None and t.type == "type_identifier" else None


def _bare_names(tree, data: bytes, ranges=None) -> set:
    """Names that code uses without a path: identifiers and path heads, not path tails or fields."""
    out = set()
    for n in _walk(tree.root_node, ("line_comment", "block_comment", "string_literal", "raw_string_literal", "use_declaration")):
        if ranges is not None and not any(a <= n.start_byte and n.end_byte <= b for a, b in ranges):
            continue
        if n.type in ("identifier", "type_identifier"):
            p = n.parent
            if p is not None and p.type in _SCOPED and p.child_by_field_name("name") is not None and p.child_by_field_name("name").start_byte == n.start_byte:
                continue
            if p is not None and p.type == "token_tree":
                prev = n.prev_sibling
                if prev is not None and prev.type in ("::", "."):
                    continue
            out.add(_text(n, data))
        elif n.type == "macro_invocation":
            m = n.child_by_field_name("macro")
            if m is not None and m.type == "identifier":
                out.add(_text(m, data) + "!")
    return out


def _accessed(tree, data: bytes, ranges=None) -> set:
    """Field and method names that code reaches through a value or a type path."""
    out = set()
    for n in _walk(tree.root_node, ("line_comment", "block_comment", "string_literal", "raw_string_literal")):
        if ranges is not None and not any(a <= n.start_byte and n.end_byte <= b for a, b in ranges):
            continue
        if n.type == "field_expression":
            f = n.child_by_field_name("field")
            if f is not None:
                out.add(_text(f, data))
        elif n.type in ("field_initializer", "field_pattern"):
            f = n.child_by_field_name("field") or n.child_by_field_name("name")
            if f is not None:
                out.add(_text(f, data))
        elif n.type in ("shorthand_field_initializer",):
            out.add(_text(n, data))
        elif n.type == "token_tree":
            kids = n.children
            for i, k in enumerate(kids[1:], 1):
                if kids[i - 1].type == "." and k.type in ("identifier", "integer_literal"):
                    out.add(_text(k, data))
        elif n.type in _SCOPED:
            nm = n.child_by_field_name("name")
            if nm is not None:
                out.add(_text(nm, data))
    return out


def _privates(nodes, data: bytes):
    """Yield ``(name, insert_at)`` for private fields and inherent methods of these items."""
    for node in nodes:
        if node.type == "struct_item":
            body = node.child_by_field_name("body")
            if body is None:
                continue
            if body.type == "field_declaration_list":
                for f in body.named_children:
                    if f.type == "field_declaration" and not _vis(f, data):
                        yield _text(f.child_by_field_name("name"), data), f.start_byte
            elif body.type == "ordered_field_declaration_list":
                index = 0
                for ch in body.named_children:
                    if ch.type in ("attribute_item", "line_comment", "block_comment"):
                        continue
                    if ch.type == "visibility_modifier":
                        continue
                    prev = ch.prev_named_sibling
                    if prev is None or prev.type != "visibility_modifier":
                        yield str(index), ch.start_byte
                    index += 1
        elif node.type == "impl_item" and node.child_by_field_name("trait") is None:
            body = node.child_by_field_name("body")
            for f in body.named_children if body is not None else ():
                if f.type in ("function_item", "const_item") and not _vis(f, data):
                    yield _item_name(f, data), f.start_byte


def plan_move(graph: dict, node: dict, source: Path, to: str, root: Path) -> MovePlan:
    """Move one top-level item, with the ``impl`` blocks of a moved type, to another module."""
    name = symbol_name(str(node.get("label", "")))
    pkg = _package(source)
    crate = _crate_of(pkg, source)
    s_mod = crate.files[source]
    target = _target(root, to, source)
    if target == source:
        raise Unsupported(f"`{name}` is already in {source}.")
    if target.exists() and target not in crate.files:
        raise Unsupported(f"{_rel(root, target)} is not a module of the crate of {_rel(root, source)}.")
    new_module = not target.exists()
    t_mod = crate.files[target] if not new_module else _module_for(crate, target)
    subject = f"{_rel(root, source)}::{name} -> {_rel(root, target)}"
    data = source.read_bytes()
    tree = _parse(data)
    tops = tree.root_node.named_children
    items = [n for n in tops if n.type in _ITEMS and _item_name(n, data) == name]
    if not items:
        if any(n.type in ("macro_definition", "mod_item") and _item_name(n, data) == name for n in tops):
            raise Unsupported(f"`{name}` is a macro or a module. Move a module with move_file. Move a macro by hand.")
        raise Unsupported(f"`{name}` is not a top-level item of {_rel(root, source)}.")
    moved_nodes = list(items)
    if any(n.type in _TYPES for n in items):
        moved_nodes += [n for n in tops if n.type == "impl_item" and _impl_of(n, data) == name]
    moved_nodes.sort(key=lambda n: n.start_byte)
    spans = [_span(data, n) for n in moved_nodes]
    conflicts = []
    notes = []
    mapping = [(s_mod + (name,), t_mod + (name,))]
    files = _Files()
    clean = {}

    # What the moved code needs from the source module.
    bare = _bare_names(tree, data, spans)
    top_names = {}
    for n in tops:
        if n.type in _ITEMS and not any(a <= n.start_byte < b for a, b in spans):
            top_names[_item_name(n, data)] = n
    macros = {_item_name(n, data) + "!" for n in _walk(tree.root_node) if n.type == "macro_definition"}
    if bare & macros:
        conflicts.append(f"The moved code uses the macro {', '.join(sorted(bare & macros))} of the source file. Move it by hand.")
    src_ctx = _Ctx(True, crate.name, set(crate.modules))
    src_ctx.aliases = _aliases(tree, data, s_mod, src_ctx)
    t_ctx = _Ctx(True, crate.name, set(crate.modules) | ({t_mod} if new_module else set()), dict(src_ctx.aliases))
    uses = []
    for item in sorted(set(top_names) & bare):
        uses.append(f"use {_spell(s_mod + (item,), t_ctx)};")
    calls_methods = _calls_methods(tree, data, spans)
    for decl, leaves in _uses(tree, data):
        for lf in leaves:
            names = [s for s, _ in lf.segs]
            wanted = lf.kind == "glob" or lf.local in bare or (calls_methods and lf.local in _METHOD_TRAITS) or (lf.local == "Write" and bool({"write!", "writeln!"} & bare))
            if not wanted:
                continue
            head = _head(names, s_mod, _Ctx(True, crate.name, set(crate.modules)))
            if head is not None:
                full = head[0] + tuple(names[head[1] :])
                if lf.kind != "glob" and full[:-1] == t_mod and lf.alias in (None, full[-1]):
                    continue
                if lf.kind == "glob" and full == t_mod:
                    continue
                text = _spell(full, t_ctx)
            else:
                text = "::".join(names)
            text += "::*" if lf.kind == "glob" else (f" as {lf.alias}" if lf.alias else "")
            uses.append(f"use {text};")
    if any(lf.kind == "glob" for _, leaves in _uses(tree, data) for lf in leaves):
        notes.append("The target takes the glob imports of the source file. Remove the ones it does not use.")

    # Privacy: what crosses the module boundary now.
    edits_vis = []
    for item in sorted(set(top_names) & bare):
        n = top_names[item]
        if not _vis(n, data):
            edits_vis.append((n.start_byte, n.start_byte, "pub(crate) "))
            notes.append(f"`{item}` in {_rel(root, source)} is now pub(crate), because the moved code uses it.")
    rest = [(a, b) for a, b in _gaps(spans, len(data))]
    moved_access = _accessed(tree, data, spans)
    for fname, at in _privates([n for n in tops if not any(a <= n.start_byte < b for a, b in spans)], data):
        if fname in moved_access:
            edits_vis.append((at, at, "pub(crate) "))
            notes.append(f"Field or method `{fname}` is now pub(crate), because the moved code uses it.")
    rest_access = _accessed(tree, data, rest)
    inner_vis = []
    for fname, at in _privates(moved_nodes, data):
        if fname in rest_access:
            inner_vis.append((at, at, "pub(crate) "))
            notes.append(f"Field or method `{fname}` of `{name}` is now pub(crate), because {_rel(root, source)} uses it.")

    # Rewrite paths everywhere; the moved spans resolve from the target module.
    still_used = name in _bare_names(tree, data, rest)
    if any(n.type == "trait_item" for n in items):
        methods = {_item_name(f, data) for n in items for f in _walk(n) if f.type in ("function_item", "function_signature_item") and f.parent is not None and f.parent.parent is not None and f.parent.parent.type == "trait_item"}
        still_used = still_used or bool(methods & rest_access)
    external = still_used
    snippet = ""
    for path, fdata, ftree, scope, ctx_old, ctx_new in _world(pkg, crate, root, graph, mapping, [t_mod] if new_module else []):
        if path == source:
            edits = _rewrite(fdata, ftree, scope, scope, ctx_old, ctx_new, mapping, [(a, b, t_mod) for a, b in spans])
            inside = [e for e in edits if any(a <= e[0] < b for a, b in spans)] + inner_vis
            outside = [e for e in edits if not any(a <= e[0] < b for a, b in spans)] + edits_vis
            external = external or bool(outside)
            pieces = []
            for a, b in spans:
                local = [(s - a, e - a, t) for s, e, t in inside if a <= s < b]
                pieces.append(replace_ranges(fdata[a:b], local).decode("utf-8", "replace").strip("\n"))
            snippet = "\n\n".join(pieces)
            line = f"use {_spell(t_mod + (name,), ctx_new)};"
            inline = [n for n in tops if n.type == "mod_item" and n.child_by_field_name("body") is not None and not any(a <= n.start_byte < b for a, b in spans)]
            top_level = name in _bare_names(tree, data, list(_gaps(spans + [(n.start_byte, n.end_byte) for n in inline], len(data)))) or (still_used and name not in _bare_names(tree, data, rest))
            if still_used and not top_level:
                for m in inline:
                    if name in _bare_names(tree, data, [(m.start_byte, m.end_byte)]):
                        outside.append(_inner_use(fdata, m, line))
            out = replace_ranges(fdata, outside + [(a, b, "") for a, b in spans])
            if still_used and top_level:
                out = add_uses(out, [line])
            clean[source] = _rustfmt(fdata, pkg.edition) == fdata
            files.get(path)
            files.put(path, out)
            continue
        edits = _rewrite(fdata, ftree, scope, scope, ctx_old, ctx_new, mapping)
        out = replace_ranges(fdata, edits) if edits else fdata
        out = _glob_imports(out, scope, ctx_old, ctx_new, s_mod, t_mod, name)
        if out != fdata:
            external = True
            files.get(path)
            files.put(path, out)

    # The moved item keeps working for its users.
    for n in items:
        vis = _vis(n, data).strip()
        if vis in ("", "pub(self)") and external:
            notes.append(f"`{name}` is now pub(crate), because code outside {_rel(root, target)} uses it.")
            snippet = _with_vis(snippet, name, "pub(crate)")
        elif vis.startswith(("pub(super)", "pub(in")):
            notes.append(f"`{vis}` on `{name}` became `pub(crate)`.")
            snippet = _with_vis(snippet, name, "pub(crate)", vis)
    item_vis = _vis(items[0], data).strip()

    tdata = files.get(target)
    if not new_module:
        ttree = _parse(tdata)
        if any(n.type in _ITEMS and _item_name(n, tdata) == name for n in ttree.root_node.named_children):
            conflicts.append(f"{_rel(root, target)} already has `{name}`.")
        have = {lf.local for _, leaves in _uses(ttree, tdata) for lf in leaves if lf.local}
        uses = [u for u in uses if not (m := re.search(r"(\w+);$", u)) or m.group(1) not in have or u.endswith("*;")]
        clean[target] = _rustfmt(tdata, pkg.edition) == tdata
    else:
        clean[target] = clean.get(source, False)
    out = add_uses(tdata, uses) if uses else tdata
    out = _append(out, snippet)
    files.put(target, out)
    created = []
    made = {}
    if new_module:
        _declare(files, crate, t_mod, lambda mod: _mod_line("pub " if item_vis == "pub" else "", mod), created, made)
        for mod in created:
            notes.append(f"Created module {'::'.join(mod)} in {_rel(root, made[mod])}.")
        notes.append(f"Created module {'::'.join(t_mod)} in {_rel(root, target)}.")
    if crate.name and item_vis == "pub":
        notes.append(f"The public path of `{name}` is now {crate.name}::{'::'.join(t_mod + (name,))}.")
    notes += _format(files, pkg.edition, clean)
    renames = ((f"{_rel(root, source)}::{name}", f"{_rel(root, target)}::{name}"),)
    return MovePlan(subject, str(source), str(target), files.contents(), tuple(dict.fromkeys(conflicts)), tuple(dict.fromkeys(notes)), (), renames)


def _calls_methods(tree, data: bytes, ranges) -> bool:
    for n in _walk(tree.root_node, ("line_comment", "block_comment", "string_literal", "raw_string_literal")):
        if not any(a <= n.start_byte and n.end_byte <= b for a, b in ranges):
            continue
        if n.type == "call_expression" and n.child_by_field_name("function") is not None and n.child_by_field_name("function").type == "field_expression":
            return True
        if n.type == "token_tree":
            kids = n.children
            for i in range(2, len(kids)):
                if kids[i - 2].type == "." and kids[i - 1].type == "identifier" and kids[i].type == "token_tree" and _text(kids[i], data).startswith("("):
                    return True
    return False


def _inner_use(data: bytes, mod, line: str):
    body = mod.child_by_field_name("body")
    kids = [c for c in body.named_children if c.type not in ("line_comment", "block_comment")]
    indent = " " * (kids[0].start_point[1] if kids else mod.start_point[1] + 4)
    uses = [c for c in kids if c.type == "use_declaration"]
    at = uses[-1].end_byte if uses else body.start_byte + 1
    return (at, at, f"\n{indent}{line}")


def _gaps(spans, size: int):
    pos = 0
    for a, b in sorted(spans):
        if a > pos:
            yield pos, a
        pos = max(pos, b)
    if pos < size:
        yield pos, size


def _with_vis(snippet: str, name: str, vis: str, old: str = "") -> str:
    data = snippet.encode()
    tree = _parse(data)
    for n in tree.root_node.named_children:
        if n.type in _ITEMS and _item_name(n, data) == name:
            for ch in n.children:
                if ch.type == "visibility_modifier":
                    return replace_ranges(data, [(ch.start_byte, ch.end_byte, vis)]).decode()
            return replace_ranges(data, [(n.start_byte, n.start_byte, vis + " ")]).decode()
    return snippet


def _append(data: bytes, snippet: str) -> bytes:
    """Add the moved code at the end, before a trailing ``#[cfg(test)]`` module."""
    tree = _parse(data)
    kids = tree.root_node.named_children
    last = kids[-1] if kids else None
    piece = snippet.strip("\n").encode()
    if last is not None and last.type == "mod_item" and last.child_by_field_name("body") is not None and any(b"cfg(test)" in data[a.start_byte : a.end_byte] for a in _attached(last)):
        start = _span(data, last)[0]
        head = data[:start].rstrip(b"\n")
        return head + (b"\n\n" if head else b"") + piece + b"\n\n" + data[start:]
    head = data.rstrip(b"\n")
    return head + (b"\n\n" if head else b"") + piece + b"\n"


def _glob_imports(data: bytes, scope: tuple, ctx_old: _Ctx, ctx_new: _Ctx, s_mod: tuple, t_mod: tuple, name: str) -> bytes:
    """Import ``name`` explicitly after each glob import of the source module whose scope uses it."""
    tree = _parse(data)
    if name not in _bare_names(tree, data):
        return data
    edits = []
    for decl, leaves in _uses(tree, data, top_only=False):
        for lf in leaves:
            if lf.kind != "glob":
                continue
            names = [s for s, _ in lf.segs]
            inner = scope + _scope_of(decl, data)
            head = _head(names, inner, ctx_old)
            if head is None or head[0] + tuple(names[head[1] :]) != s_mod or inner == t_mod:
                continue
            indent = " " * decl.start_point[1]
            edits.append((decl.end_byte, decl.end_byte, f"\n{indent}use {_spell(t_mod + (name,), ctx_new)};"))
    return replace_ranges(data, edits) if edits else data
