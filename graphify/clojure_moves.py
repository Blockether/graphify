"""Clojure moves: rename a namespace with its file, or move a var to another namespace.

Both moves read the code with tree-sitter. Strings and comments stay unchanged; the
plan reports them as mentions. A var move keeps unqualified uses unqualified
through ``:refer`` and rewrites qualified uses to an alias of the new namespace.
"""
from __future__ import annotations

import re
from pathlib import Path

from graphify.moves import MovePlan, project_files, replace_ranges, resolve_path
from graphify.refactor import CLOJURE_SUFFIXES, Mention, Unsupported

_PARSER = None
_NS_NAME = re.compile(r"^[A-Za-z*+!?<>=_-][\w.*+!?<>=-]*$")
_VAL_SKIP = frozenset({"(", ")", "[", "]", "{", "}", "comment", "dis_expr"})


def _parse(data: bytes):
    global _PARSER
    if _PARSER is None:
        import tree_sitter_clojure as tsclj
        from tree_sitter import Language, Parser

        _PARSER = Parser(Language(tsclj.language()))
    return _PARSER.parse(data)


def munge(ns: str) -> str:
    return ns.replace("-", "_")


def ns_path(ns: str, suffix: str) -> str:
    """The file of a namespace, relative to its source root: ``a.my-ns`` -> ``a/my_ns.clj``."""
    return munge(ns).replace(".", "/") + suffix


def source_root(path: Path, ns: str) -> Path | None:
    """The source root that holds the file of ``ns``, or None when the path does not match it."""
    rel = Path(ns_path(ns, path.suffix)).parts
    if tuple(path.parts[-len(rel):]) != rel:
        return None
    return Path(*path.parts[: -len(rel)])


def path_ns(rel: Path) -> str:
    return ".".join(rel.with_suffix("").parts).replace("_", "-")


def _text(node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _values(node):
    return [c for c in node.children if c.type not in _VAL_SKIP]


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        if n.type != "comment":
            stack.extend(reversed(n.children))


def _sym(node, data: bytes):
    """``(ns, name, ns_node, name_node)`` of a ``sym_lit``."""
    ns = name = None
    for c in node.children:
        if c.type == "sym_ns":
            ns = c
        elif c.type == "sym_name":
            name = c
    return (_text(ns, data) if ns else None), (_text(name, data) if name else None), ns, name


def _kwd(node, data: bytes):
    """``(auto, ns, name, ns_node)`` of a ``kwd_lit``. ``auto`` is True for ``::``."""
    auto = any(c.type == "::" for c in node.children)
    ns = next((c for c in node.children if c.type == "kwd_ns"), None)
    name = next((c for c in node.children if c.type == "kwd_name"), None)
    return auto, (_text(ns, data) if ns else None), (_text(name, data) if name else None), ns


def _head(node, data: bytes) -> str | None:
    vals = _values(node)
    if node.type == "list_lit" and vals and vals[0].type == "sym_lit":
        return _sym(vals[0], data)[1]
    return None


def _ns_form(tree, data: bytes):
    for top in tree.root_node.children:
        if top.type == "list_lit" and _head(top, data) == "ns":
            return top
    return None


def file_ns(data: bytes) -> str | None:
    form = _ns_form(_parse(data), data)
    if form is None:
        return None
    vals = _values(form)
    return _sym(vals[1], data)[1] if len(vals) > 1 and vals[1].type == "sym_lit" else None


def _clauses(form, data: bytes, key: str) -> list:
    out = []
    for c in _values(form):
        vals = _values(c) if c.type == "list_lit" else []
        if vals and vals[0].type == "kwd_lit" and _kwd(vals[0], data)[2] == key:
            out.append(c)
    return out


class _Spec:
    """One libspec of a ``:require`` clause."""

    def __init__(self, node, ns, prefix=False):
        self.node, self.ns, self.prefix = node, ns, prefix
        self.alias = None
        self.alias_node = None
        self.refers: list = []  # (text, sym node)
        self.refer_vec = None
        self.refer_key = None
        self.refer_all = False
        self.rename = False


def _specs(clause, data: bytes) -> list[_Spec]:
    out = []
    for item in _values(clause)[1:]:
        if item.type == "sym_lit":
            out.append(_Spec(item, _sym(item, data)[1]))
        elif item.type == "vec_lit":
            vals = _values(item)
            if not vals or vals[0].type != "sym_lit":
                continue
            spec = _Spec(item, _sym(vals[0], data)[1])
            rest = vals[1:]
            for key, val in zip(rest[::2], rest[1::2]):
                k = _kwd(key, data)[2] if key.type == "kwd_lit" else None
                if k in ("as", "as-alias") and val.type == "sym_lit":
                    spec.alias, spec.alias_node = _sym(val, data)[1], val
                elif k == "refer":
                    spec.refer_key = key
                    if val.type == "vec_lit":
                        spec.refer_vec = val
                        spec.refers = [(_sym(v, data)[1], v) for v in _values(val) if v.type == "sym_lit"]
                    else:
                        spec.refer_all = True
                elif k == "rename":
                    spec.rename = True
            out.append(spec)
        elif item.type == "list_lit":
            vals = _values(item)
            if vals and vals[0].type == "sym_lit":
                out.append(_Spec(item, _sym(vals[0], data)[1], prefix=True))
    return out


def _all_specs(data: bytes):
    tree = _parse(data)
    form = _ns_form(tree, data)
    if form is None:
        return None, []
    return form, [s for c in _clauses(form, data, "require") for s in _specs(c, data)]


# ---------------------------------------------------------------- namespace rename


def _string_mentions(path: str, data: bytes, olds) -> list[Mention]:
    out = []
    tree = _parse(data)
    for n in _walk(tree.root_node):
        if n.type not in ("str_lit", "comment"):
            continue
        text = _text(n, data)
        for old in olds:
            if re.search(r"(?<![\w.-])" + re.escape(old) + r"(?![\w-])", text) or ns_path(old, "") in text:
                out.append(Mention(path, n.start_point[0] + 1, text.strip()[:120], "text"))
                break
    return out


def _rename_edits(data: bytes, mapping: dict[str, str]) -> tuple[list, int, list[str]]:
    """Edits that rename every namespace of ``mapping``, the count of keyword edits and conflicts."""
    tree = _parse(data)
    munged = {munge(o): munge(n) for o, n in mapping.items()}
    edits, keywords, conflicts = [], 0, []
    for n in _walk(tree.root_node):
        if n.type == "sym_lit":
            ns, name, ns_node, name_node = _sym(n, data)
            if ns is not None and ns in mapping:
                edits.append((ns_node.start_byte, ns_node.end_byte, mapping[ns]))
            elif ns is None and name in mapping:
                edits.append((name_node.start_byte, name_node.end_byte, mapping[name]))
            elif ns is None and name in munged and name not in mapping:
                edits.append((name_node.start_byte, name_node.end_byte, munged[name]))
            elif ns is None and name:
                for old, new in munged.items():
                    rest = name[len(old) + 1 :]
                    if name.startswith(old + ".") and rest[:1].isupper() and "." not in rest:
                        edits.append((name_node.start_byte, name_node.start_byte + len(old.encode()), new))
                        break
        elif n.type == "kwd_lit":
            auto, ns, _, ns_node = _kwd(n, data)
            if not auto and ns in mapping:
                edits.append((ns_node.start_byte, ns_node.end_byte, mapping[ns]))
                keywords += 1
        elif n.type == "list_lit" and _head(n, data) == "ns":
            for spec in (s for c in _clauses(n, data, "require") for s in _specs(c, data)):
                if spec.prefix and any(o.startswith(spec.ns + ".") for o in mapping):
                    conflicts.append(f"line {spec.node.start_point[0] + 1}: the prefix list `{_text(spec.node, data)[:60]}` can name a moved namespace; rewrite it as plain libspecs first.")
    return edits, keywords, conflicts


def _namespaces(files) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for f in files:
        data = f.read_bytes()
        if b"(ns" not in data:
            continue
        ns = file_ns(data)
        if ns and ns not in out:
            out[ns] = f
    return out


def _new_location(old: Path, ns: str, new: str, root: Path, roots) -> tuple[str | None, Path | None]:
    """The new namespace and path from ``new``: a namespace name or a file path."""
    here = source_root(old, ns)
    if new.lower().endswith(CLOJURE_SUFFIXES) or "/" in new:
        path = resolve_path(root, new)
        if path.suffix == "":
            path = path.with_suffix(old.suffix)
        for base in sorted({here, *roots} - {None}, key=lambda p: -len(p.parts)):
            if path.is_relative_to(base):
                return path_ns(path.relative_to(base)), path
        return None, path
    if here is None:
        return new, None
    return new, here / ns_path(new, old.suffix)


def plan_move_file(graph: dict, old: Path, new: str, root: Path, tests: bool = True) -> MovePlan:
    files = project_files(root, graph, CLOJURE_SUFFIXES)
    if old not in files:
        files.append(old)
    data = old.read_bytes()
    ns = file_ns(data)
    if not ns:
        raise Unsupported(f"{old} has no ns form.")
    known = _namespaces(files)
    roots = {source_root(p, n) for n, p in known.items()}
    new_ns, new_path = _new_location(old, ns, new, root, roots)
    conflicts: list[str] = []
    if new_path is None:
        conflicts.append(f"{old} is not at the path of `{ns}`; name the new file path.")
    if not new_ns or not _NS_NAME.match(new_ns) or ".." in new_ns or new_ns.endswith("."):
        conflicts.append(f"`{new_ns}` is not a valid namespace name.")
    pairs = [(ns, new_ns, old, new_path)]
    if tests and not ns.endswith("-test") and (ns + "-test") in known and new_ns:
        test = known[ns + "-test"]
        base = source_root(test, ns + "-test")
        pairs.append((ns + "-test", new_ns + "-test", test, base / ns_path(new_ns + "-test", test.suffix) if base else None))
        if base is None:
            conflicts.append(f"{test} is not at the path of `{ns}-test`; move it by hand.")
    for o, n, p, q in pairs:
        if o == n and p == q:
            conflicts.append(f"`{o}` already has this name and path.")
        if n in known and known[n] != p:
            conflicts.append(f"`{n}` already exists in {known[n]}.")
        if q is not None and q != p and q.exists():
            conflicts.append(f"{q} already exists.")
    mapping = {o: n for o, n, _, _ in pairs if n and o != n}
    contents: dict = {}
    mentions: list[Mention] = []
    keywords = 0
    needles = [x.encode() for o in mapping for x in (o, munge(o))]
    for f in files:
        before = f.read_bytes()
        if not any(x in before for x in needles):
            continue
        edits, kw, bad = _rename_edits(before, mapping)
        keywords += kw
        conflicts.extend(f"{f}: {c}" for c in bad)
        after = replace_ranges(before, edits)
        if after != before:
            contents[str(f)] = (before, after)
        mentions.extend(_string_mentions(str(f), after, mapping))
    for o, n, p, q in pairs:
        if q is None or q == p:
            continue
        before = p.read_bytes()
        after = contents.get(str(p), (before, before))[1]
        contents[str(p)] = (before, None)
        contents[str(q)] = (None, after)
    notes = []
    if len(pairs) > 1:
        notes.append(f"The test namespace `{pairs[1][0]}` moves to `{pairs[1][1]}` with its file.")
    if keywords:
        notes.append(f"{keywords} qualified keywords such as `:{ns}/k` follow the namespace, as `::k` does. Check stored data that uses the old keywords.")
    return MovePlan(f"{ns} -> {new_ns}", str(old), str(new_path or ""), contents, tuple(conflicts), tuple(notes), tuple(mentions), tuple((o, n) for o, n, _, _ in pairs))


# ---------------------------------------------------------------- var move


def _top_form(tree, start: int):
    for top in tree.root_node.children:
        if top.start_byte <= start < top.end_byte:
            return top
    return None


def _span(tree, data: bytes, form) -> tuple[int, int]:
    """The form with the comments directly above it, from line start to the end of its line."""
    start = form.start_byte
    prev = form.prev_sibling
    while prev is not None and prev.type == "comment" and data[prev.end_byte : start].count(b"\n") <= 1 and data[prev.end_byte : start].strip() == b"":
        start = prev.start_byte
        prev = prev.prev_sibling
    line = data.rfind(b"\n", 0, start) + 1
    if data[line:start].strip() == b"":
        start = line
    end = form.end_byte
    nl = data.find(b"\n", end)
    end = len(data) if nl < 0 else nl + 1
    if data[form.end_byte : end].strip():
        end = form.end_byte
    return start, end


def _cut(data: bytes, start: int, end: int) -> bytes:
    """Remove ``start:end`` and keep one blank line between the forms around it."""
    head, tail = data[:start].rstrip(b"\n"), data[end:].lstrip(b"\n")
    if not head.strip():
        return tail
    if not tail.strip():
        return head + b"\n"
    return head + b"\n\n" + tail


def _private(form, data: bytes) -> bool:
    vals = _values(form)
    if not vals:
        return False
    head = _sym(vals[0], data)[1] or ""
    name = _text(vals[1], data) if len(vals) > 1 else ""
    return head.endswith("-") or ":private" in name


def _alias_for(ns: str, taken: set[str]) -> str:
    parts = ns.split(".")
    for cand in (parts[-1], "-".join(parts[-2:]), ns.replace(".", "-")):
        if cand not in taken:
            return cand
    i = 2
    while f"{parts[-1]}{i}" in taken:
        i += 1
    return f"{parts[-1]}{i}"


def _remove_item(data: bytes, node) -> tuple[int, int]:
    """The range that removes ``node`` from its collection with its separating space."""
    sibs = _values(node.parent)
    i = next(k for k, s in enumerate(sibs) if s.id == node.id)
    if i + 1 < len(sibs):
        return node.start_byte, sibs[i + 1].start_byte
    if i > 0:
        return sibs[i - 1].end_byte, node.end_byte
    return node.start_byte, node.end_byte


def _uses(data: bytes, ns: str, alias: str | None) -> bool:
    """True when the code still names ``ns`` through its alias or its full name."""
    tree = _parse(data)
    form = _ns_form(tree, data)
    for n in _walk(tree.root_node):
        if form is not None and form.start_byte <= n.start_byte < form.end_byte:
            continue
        if n.type == "sym_lit":
            q = _sym(n, data)[0]
            if q is not None and q in (alias, ns):
                return True
        elif n.type == "kwd_lit":
            auto, q, _, _ = _kwd(n, data)
            if q is not None and ((auto and q == alias) or (not auto and q == ns)):
                return True
    return False


def _clean_spec(data: bytes, ns: str) -> bytes:
    """Drop an empty ``:refer []`` of ``ns``, and the libspec when nothing uses it any more."""
    _, specs = _all_specs(data)
    spec = next((s for s in specs if s.ns == ns and not s.prefix), None)
    if spec is None:
        return data
    if spec.refer_vec is not None and not spec.refers:
        start = spec.refer_key.prev_sibling.end_byte if spec.refer_key.prev_sibling is not None else spec.refer_key.start_byte
        data = data[:start] + data[spec.refer_vec.end_byte :]
        _, specs = _all_specs(data)
        spec = next(s for s in specs if s.ns == ns and not s.prefix)
    if spec.refers or spec.refer_all or spec.rename or _uses(data, ns, spec.alias):
        return data
    start, end = _remove_item(data, spec.node)
    data = data[:start] + data[end:]
    form, _ = _all_specs(data)
    for clause in _clauses(form, data, "require"):
        if len(_values(clause)) == 1:
            prev = clause.prev_sibling
            data = data[: prev.end_byte if prev is not None else clause.start_byte] + data[clause.end_byte :]
            break
    return data


def _indent(data: bytes, node) -> int:
    return node.start_byte - (data.rfind(b"\n", 0, node.start_byte) + 1)


def _add_requires(data: bytes, adds: dict, imports: dict | None = None) -> bytes:
    """Add libspecs to the ns form. ``adds`` maps a namespace to ``(alias or None, [refers])``."""
    for ns, (alias, refers) in sorted(adds.items()):
        form, specs = _all_specs(data)
        spec = next((s for s in specs if s.ns == ns and not s.prefix), None)
        refers = [r for r in refers if spec is None or r not in {t for t, _ in spec.refers}]
        if spec is not None and spec.node.type == "vec_lit":
            if spec.refer_all:
                refers = []
            edits = []
            if alias and not spec.alias:
                edits.append((_values(spec.node)[0].end_byte, _values(spec.node)[0].end_byte, f" :as {alias}"))
            if refers and spec.refer_vec is not None:
                at = spec.refer_vec.end_byte - 1
                edits.append((at, at, (" " if spec.refers else "") + " ".join(refers)))
            elif refers:
                at = spec.node.end_byte - 1
                edits.append((at, at, f" :refer [{' '.join(refers)}]"))
            data = replace_ranges(data, edits)
            continue
        text = "[" + ns + (f" :as {alias}" if alias else "") + (f" :refer [{' '.join(refers)}]" if refers else "") + "]"
        if spec is not None:  # a bare symbol libspec
            data = data[: spec.node.start_byte] + text.encode() + data[spec.node.end_byte :]
            continue
        data = _insert_spec(data, form, "require", text, ns)
    for pkg, classes in sorted((imports or {}).items()):
        form, _ = _all_specs(data)
        data = _insert_spec(data, form, "import", f"({pkg} {' '.join(sorted(classes))})", pkg)
    return data


def _insert_spec(data: bytes, form, key: str, text: str, sort_key: str) -> bytes:
    clauses = _clauses(form, data, key)
    if not clauses:
        last = _values(form)[-1]
        col = _indent(data, form) + 2
        return data[: last.end_byte] + f"\n{' ' * col}(:{key} {text})".encode() + data[last.end_byte :]
    clause = clauses[0]
    items = _values(clause)[1:]
    if not items:
        at = _values(clause)[0].end_byte
        return data[:at] + f" {text}".encode() + data[at:]
    keys = [_text(_values(i)[0] if i.child_count and _values(i) else i, data) for i in items]
    col = _indent(data, items[0])
    if keys == sorted(keys):
        later = next((i for i, k in zip(items, keys) if k > sort_key), None)
        if later is not None:
            at = later.start_byte
            return data[:at] + f"{text}\n{' ' * col}".encode() + data[at:]
    at = items[-1].end_byte
    return data[:at] + f"\n{' ' * col}{text}".encode() + data[at:]


def _import_map(form, data: bytes) -> dict[str, str]:
    """Simple class name -> package of the ``:import`` clause."""
    out = {}
    for clause in _clauses(form, data, "import") if form is not None else ():
        for item in _values(clause)[1:]:
            if item.type in ("list_lit", "vec_lit"):
                vals = [_text(v, data) for v in _values(item)]
                for cls in vals[1:]:
                    out[cls] = vals[0]
            elif item.type == "sym_lit":
                full = _text(item, data)
                if "." in full:
                    pkg, cls = full.rsplit(".", 1)
                    out[cls] = pkg
    return out


def _target_location(source: Path, ns: str, to: str, root: Path, known: dict) -> tuple[str | None, Path]:
    if to.lower().endswith(CLOJURE_SUFFIXES) or "/" in to:
        path = resolve_path(root, to)
        if path.is_file():
            return file_ns(path.read_bytes()), path
        roots = {source_root(p, n) for n, p in known.items()} | {source_root(source, ns)}
        for base in sorted(roots - {None}, key=lambda p: -len(p.parts)):
            if path.is_relative_to(base):
                return path_ns(path.relative_to(base)), path
        return None, path
    if to in known:
        return to, known[to]
    base = source_root(source, ns)
    return to, (base / ns_path(to, source.suffix)) if base else source.with_name(ns_path(to, source.suffix).rsplit("/", 1)[-1])


def plan_move(graph: dict, node: dict, source: Path, to: str, root: Path) -> MovePlan:
    from graphify import clojure_refactor
    from graphify.extractors.clojure import extract_clojure
    from graphify.refactor import _clojure

    var = _clojure(node)
    if not var:
        raise Unsupported(f"{node.get('label')} is not a Clojure var.")
    old_ns, name = var
    files = project_files(root, graph, CLOJURE_SUFFIXES)
    known = _namespaces(files)
    new_ns, target = _target_location(source, old_ns, to, root, known)
    conflicts: list[str] = []
    notes: list[str] = []
    if not new_ns or not _NS_NAME.match(new_ns):
        conflicts.append(f"{to} does not name a namespace. Give a namespace or a file under a source root.")
        new_ns = new_ns or "?"
    if new_ns == old_ns:
        conflicts.append(f"`{name}` is already in `{old_ns}`.")
    src_data = source.read_bytes()
    info = extract_clojure(source, positions=True).get("clojure") or {}
    tree = _parse(src_data)
    definition = next((o for o in info.get("occurrences", ()) if o["role"] == "definition" and o["name"] == name), None)
    if definition is None:
        raise Unsupported(f"No definition of `{old_ns}/{name}` in {source}.")
    form = _top_form(tree, definition["start"])
    if form is None or form.type != "list_lit":
        raise Unsupported(f"`{old_ns}/{name}` is not a top-level form; a reader conditional or a macro body cannot move.")
    start, end = _span(tree, src_data, form)
    private = _private(form, src_data)

    # Sites of the var in every file.
    parsed = clojure_refactor._scan(name, files)
    sites: dict[str, list[dict]] = {}
    for path, occ in clojure_refactor._sites(old_ns, name, parsed):
        sites.setdefault(path, []).append(occ)
    src_key = str(source)
    tgt_key = str(target)

    # What the moved form uses.
    deps_old: set[str] = set()
    refer_deps: dict[str, set[str]] = {}
    alias_deps: dict[str, str] = {}  # alias -> ns
    form_edits: list = []
    src_form, src_specs = _all_specs(src_data)
    by_alias = {s.alias: s.ns for s in src_specs if s.alias}
    for o in info.get("occurrences", ()):
        if not (start <= o["start"] < end) or o["role"] in ("definition", "declaration"):
            continue
        ns = (o.get("ns") or [None])[0]
        if o["local"] or ns == old_ns:
            if o["name"] != name and not o["qualified"]:
                deps_old.add(o["name"])
            continue
        if o["qualified"]:
            q = src_data[: o["start"]].rsplit(b"/", 1)[0].rsplit(None, 1)[-1].lstrip(b"(['#@`~^").decode()
            if q in by_alias:
                alias_deps[q] = by_alias[q]
        elif ns and ns != "clojure.core":
            refer_deps.setdefault(ns, set()).add(o["text"])
    for n in _walk(form):
        if n.type == "kwd_lit":
            auto, q, _, ns_node = _kwd(n, src_data)
            if auto and q is None:
                form_edits.append((n.start_byte, n.start_byte + 2, f":{old_ns}/"))
            elif auto and q in by_alias:
                alias_deps[q] = by_alias[q]
    src_imports = _import_map(src_form, src_data)
    need_imports: dict[str, set[str]] = {}
    for n in _walk(form):
        if n.type == "sym_lit":
            q, nm, _, _ = _sym(n, src_data)
            for cls in (q, (nm or "").rstrip(".")):
                if cls in src_imports:
                    need_imports.setdefault(src_imports[cls], set()).add(cls)

    # Private vars of the old namespace that the form uses.
    private_deps = sorted(d for d in deps_old if d in info.get("vars", {}) and _is_private_var(tree, src_data, info, d))
    if private_deps:
        conflicts.append(f"`{name}` uses private vars of `{old_ns}`: {', '.join(private_deps)}. Make them public or move them together.")
    deps_old = {d for d in deps_old if d in info.get("vars", {})}

    remaining_src = [o for o in sites.get(src_key, ()) if not (start <= o["start"] < end) and o["role"] not in ("definition", "declaration")]
    outside = [p for p in sites if p not in (src_key, tgt_key)]
    if private and (remaining_src or outside):
        conflicts.append(f"`{name}` is private and other code uses it. Make it public first.")
    tgt_exists = target.is_file()
    tgt_data = target.read_bytes() if tgt_exists else b""
    tgt_form, tgt_specs = _all_specs(tgt_data) if tgt_exists else (None, [])
    if tgt_exists and tgt_form is None:
        conflicts.append(f"{target} has no ns form.")
    if tgt_exists:
        tinfo = extract_clojure(target, positions=True).get("clojure") or {}
        if name in tinfo.get("vars", {}):
            conflicts.append(f"`{new_ns}` already defines `{name}` in {target}.")
    if deps_old and (remaining_src or outside and any(_requires(p, new_ns) for p in [src_key])):
        conflicts.append(
            f"`{name}` uses `{old_ns}` ({', '.join(sorted(deps_old))}) and `{old_ns}` still uses `{name}`: a require cycle. Move {', '.join(sorted(deps_old))} to `{new_ns}` first."
        )
    if deps_old and old_ns in known and _requires(src_key, new_ns):
        conflicts.append(f"`{old_ns}` already requires `{new_ns}`; `{name}` needs `{old_ns}`: a require cycle.")
    if remaining_src and tgt_exists and any(s.ns == old_ns for s in tgt_specs):
        conflicts.append(f"`{new_ns}` requires `{old_ns}`, which still uses `{name}`: a require cycle.")
    for path, occs in sites.items():
        for o in occs:
            if o["text"] not in (name, "->" + name, "map->" + name) and o["role"] != "definition":
                conflicts.append(f"{path}:{o['line']} names `{name}` as `{o['text']}` through :rename; edit it by hand.")

    contents: dict = {}

    # Other files.
    for path, occs in sorted(sites.items()):
        if path in (src_key, tgt_key):
            continue
        data = Path(path).read_bytes()
        _, specs = _all_specs(data)
        target_spec = next((s for s in specs if s.ns == new_ns and not s.prefix), None)
        taken = {s.alias for s in specs if s.alias}
        alias = target_spec.alias if target_spec is not None and target_spec.alias else None
        edits, refers, need_alias, full = [], [], False, False
        for o in occs:
            if o["role"] == "refer":
                sym = _find_sym(data, o["start"])
                edits.append((*_remove_item(data, sym), ""))
                refers.append(o["text"])
            elif o["qualified"]:
                sym = _find_sym(data, o["start"])
                ns_node = next((c for c in sym.children if c.type == "sym_ns"), None)
                if ns_node is None:
                    continue
                if _text(ns_node, data) == old_ns:
                    edits.append((ns_node.start_byte, ns_node.end_byte, new_ns))
                    full = True
                else:
                    if alias is None:
                        alias = _alias_for(new_ns, taken)
                    need_alias = True
                    edits.append((ns_node.start_byte, ns_node.end_byte, alias))
            elif o["role"] not in ("definition", "declaration"):
                refers.append(o["text"])
        data2 = _clean_spec(replace_ranges(data, edits), old_ns)
        spec_alias = alias if need_alias and not (target_spec is not None and target_spec.alias) else None
        add = {}
        if spec_alias or refers or (full and target_spec is None and any(s.ns == old_ns for s in specs)) or (need_alias and target_spec is None):
            add[new_ns] = (spec_alias, sorted(set(refers)))
        data2 = _add_requires(data2, add)
        contents[path] = (data, data2)

    # The moved text.
    moved = replace_ranges(src_data, form_edits)[start : end + sum(len(t) - (b - a) for a, b, t in form_edits)]
    moved = moved.strip(b"\n") + b"\n"
    adds: dict = {}
    t_alias = {s.alias: s.ns for s in tgt_specs if s.alias}
    t_by_ns = {s.ns: s for s in tgt_specs if not s.prefix}
    renames = {}
    for a, ns in sorted(alias_deps.items()):
        if ns == new_ns:
            renames[a] = None
            continue
        spec = t_by_ns.get(ns)
        if spec is not None and spec.alias:
            if spec.alias != a:
                renames[a] = spec.alias
        elif a in t_alias and t_alias[a] != ns:
            conflicts.append(f"`{new_ns}` uses the alias `{a}` for `{t_alias[a]}`; `{name}` uses it for `{ns}`.")
        else:
            adds[ns] = (a, [])
    for ns, names in refer_deps.items():
        if ns == new_ns:
            continue
        prev = adds.get(ns, (None, []))
        adds[ns] = (prev[0], sorted(set(prev[1]) | names))
    if deps_old:
        prev = adds.get(old_ns, (None, []))
        adds[old_ns] = (prev[0], sorted(set(prev[1]) | deps_old))
    if renames:
        mtree = _parse(moved)
        medits = []
        for n in _walk(mtree.root_node):
            if n.type in ("sym_lit", "kwd_lit"):
                q_node = next((c for c in n.children if c.type in ("sym_ns", "kwd_ns")), None)
                if q_node is not None and _text(q_node, moved) in renames:
                    new_q = renames[_text(q_node, moved)]
                    if new_q is None:
                        medits.append((q_node.start_byte, q_node.end_byte + 1, ""))
                    else:
                        medits.append((q_node.start_byte, q_node.end_byte, new_q))
        moved = replace_ranges(moved, medits)

    # Target file.
    if tgt_exists:
        data = tgt_data
        edits, cleaned = [], False
        for o in sites.get(tgt_key, ()):
            sym = _find_sym(data, o["start"])
            if o["role"] == "refer":
                edits.append((*_remove_item(data, sym), ""))
                cleaned = True
            elif o["qualified"]:
                ns_node = next((c for c in sym.children if c.type == "sym_ns"), None)
                if ns_node is not None:
                    edits.append((ns_node.start_byte, ns_node.end_byte + 1, ""))
                    cleaned = True
        data = replace_ranges(data, edits)
        if cleaned:
            data = _clean_spec(data, old_ns)
        new_imports = {p: c - set(k for k, v in _import_map(_all_specs(data)[0], data).items() if v == p) for p, c in need_imports.items()}
        data = _add_requires(data, adds, {p: c for p, c in new_imports.items() if c})
        data = data.rstrip(b"\n") + b"\n\n" + moved
        contents[tgt_key] = (tgt_data, data)
    else:
        head = f"(ns {new_ns})\n".encode()
        head = _add_requires(head, adds, need_imports)
        contents[tgt_key] = (None, head + b"\n" + moved)

    # Source file.
    data = _cut(src_data, start, end)
    decl = [o for o in sites.get(src_key, ()) if o["role"] == "declaration"]
    if decl:
        data = _drop_declares(data, name)
    src_add = {}
    if remaining_src:
        src_add[new_ns] = (None, [name])
    full_src = [o for o in remaining_src if o["qualified"]]
    if full_src:
        notes.append(f"{src_key} names `{name}` with a qualifier; check it.")
    kept = [o for o in info.get("occurrences", ()) if not (start <= o["start"] < end) and o["role"] != "refer"]
    for ns, names in refer_deps.items():
        for text in names:
            if not any(o["text"] == text and (o.get("ns") or [None])[0] == ns for o in kept):
                data = _drop_refer(data, ns, text)
    for ns in sorted(set(alias_deps.values()) | set(refer_deps)):
        data = _clean_spec(data, ns)
    data = _add_requires(data, src_add)
    contents[src_key] = (src_data, data)

    if any(e for e in form_edits):
        notes.append(f"Auto-resolved keywords `::k` in `{name}` now read `:{old_ns}/k`, so their value stays.")
    if not tgt_exists:
        notes.append(f"Created {target} for `{new_ns}`.")
    mentions = []
    for path, (_, new) in contents.items():
        if new is not None:
            mentions.extend(m for m in clojure_refactor._mentions(path, new, name, set()) if m.kind == "text")
    return MovePlan(f"{old_ns}/{name} -> {new_ns}/{name}", src_key, tgt_key, contents, tuple(dict.fromkeys(conflicts)), tuple(notes), tuple(mentions), ((f"{old_ns}/{name}", f"{new_ns}/{name}"),))


def _drop_refer(data: bytes, ns: str, text: str) -> bytes:
    """Remove ``text`` from the ``:refer`` vector of ``ns``."""
    _, specs = _all_specs(data)
    spec = next((s for s in specs if s.ns == ns and not s.prefix), None)
    sym = next((n for t, n in spec.refers if t == text), None) if spec is not None else None
    if sym is None:
        return data
    a, b = _remove_item(data, sym)
    return data[:a] + data[b:]


def _find_sym(data: bytes, start: int):
    tree = _parse(data)
    node = tree.root_node.descendant_for_byte_range(start, start + 1)
    while node is not None and node.type != "sym_lit":
        node = node.parent
    return node


def _requires(path: str, ns: str) -> bool:
    data = Path(path).read_bytes()
    return any(s.ns == ns for s in _all_specs(data)[1])


def _is_private_var(tree, data: bytes, info: dict, name: str) -> bool:
    for o in info.get("occurrences", ()):
        if o["role"] == "definition" and o["name"] == name:
            form = _top_form(tree, o["start"])
            return form is not None and form.type == "list_lit" and _private(form, data)
    return False


def _drop_declares(data: bytes, name: str) -> bytes:
    tree = _parse(data)
    for top in tree.root_node.children:
        if top.type == "list_lit" and _head(top, data) == "declare":
            syms = [v for v in _values(top)[1:] if v.type == "sym_lit" and _sym(v, data)[1] == name]
            if not syms:
                continue
            if len(_values(top)) == 2:
                start, end = _span(tree, data, top)
                return _cut(data, start, end)
            a, b = _remove_item(data, syms[0])
            return data[:a] + data[b:]
    return data
