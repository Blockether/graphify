"""Moves for languages with named module blocks inside files: Elixir and Fortran.

A function moves from one module block to another, and the calls follow it:
Elixir rewrites ``Module.fun`` and aliases, Fortran rewrites ``use m, only:`` lists.
A whole module block moves without a rename. A file move renames an Elixir module
whose name follows its path (``lib/a/util.ex`` and ``A.Util``).
"""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from graphify import move_kit as kit
from graphify.moves import MovePlan, project_files, replace_ranges, resolve_path
from graphify.refactor import Unsupported

__all__ = ["LANGS", "plan_move", "plan_move_file"]


@dataclass
class Proc:
    name: str
    start: int
    end: int
    public: bool = True
    node: object = None


@dataclass
class Mod:
    name: str
    start: int
    end: int
    head: int  # after the module line: aliases and uses go here
    tail: int  # the line start of the closing ``end``: new procedures go before it
    procs: list = field(default_factory=list)
    node: object = None


def _comment_at(tree, i):
    n = tree.root_node.descendant_for_byte_range(i, i)
    while n is not None:
        if "comment" in n.type or n.type in ("string", "string_literal", "charlist", "sigil"):
            return True
        n = n.parent
    return False


class Elixir:
    name = "elixir"
    suffixes = (".ex", ".exs")
    acyclic = False
    _DEFS = ("def", "defp", "defmacro", "defmacrop", "defguard", "defguardp", "defdelegate")

    def modules(self, tree, data, path):
        out = []
        for n in tree.root_node.named_children:
            if n.type != "call" or kit.text(n.child_by_field_name("target"), data) != "defmodule":
                continue
            args = next((c for c in n.named_children if c.type == "arguments"), None)
            body = next((c for c in n.named_children if c.type == "do_block"), None)
            if args is None or body is None:
                continue
            s, e = kit.span(n, data)
            end_kw = body.children[-1] if body.children else body
            mod = Mod(kit.text(args, data).strip(), s, e, kit.line_end(data, n.start_byte), kit.line_start(data, end_kw.start_byte),
                      node=n)
            for c in body.named_children:
                if c.type != "call":
                    continue
                kind = kit.text(c.child_by_field_name("target"), data)
                if kind not in self._DEFS:
                    continue
                a = next((x for x in c.named_children if x.type == "arguments"), None)
                head = a.named_children[0] if a is not None and a.named_children else None
                while head is not None and head.type == "binary_operator":  # def f(x) when guard
                    head = head.named_children[0]
                if head is None:
                    continue
                nm = head.child_by_field_name("target") if head.type == "call" else head
                ps, pe = kit.span(c, data, lambda p: p.type == "unary_operator" and
                                  re.match(r"@(doc|spec|impl|deprecated|dialyzer)\b", kit.text(p, data)) is not None)
                mod.procs.append(Proc(kit.text(nm, data), ps, pe, not kind.endswith("p"), c))
            out.append(mod)
        return out

    def derive(self, root, path):
        parts = list(Path(kit.rel(root, path)).with_suffix("").parts)
        for i, p in enumerate(parts):
            if p in ("lib", "test"):
                parts = parts[i + 1:]
                break
        return ".".join("".join(w.capitalize() for w in p.split("_")) for p in parts if p)

    def new_module(self, name, uses, block, like):
        head = "".join(f"  {u}\n" for u in uses)
        body = textwrap.indent(textwrap.dedent(block.strip("\n")), "  ")
        return f"defmodule {name} do\n" + (head + "\n" if head else "") + body + "\nend\n"

    def indent(self):
        return "  "

    def make_public(self, text, proc):
        return re.sub(r"\b(def|defmacro|defguard)p\b", r"\1", text, count=1)

    def deps(self, data, mod):
        """The alias, import and require lines of a module: (start, end, text, short word)."""
        out = []
        for m in re.finditer(rb"(?m)^[ \t]*(alias|import|require|use)[ \t]+([\w.]+)(?:[ \t]*,[ \t]*as:[ \t]*(\w+))?[^\n]*\n?",
                             data[mod.head:mod.tail]):
            s = mod.head + m.start()
            full = m.group(2).decode()
            short = (m.group(3) or m.group(2).split(b".")[-1]).decode() if m.group(1) == b"alias" else None
            out.append((s, mod.head + m.end(), m.group().decode().strip(), m.group(1).decode(), full, short))
        return out

    def qualify(self, block, names, module, tree_path):
        """Prefix the calls of ``names`` in ``block`` with ``module``."""
        data = block.encode()
        tree = kit.parse(tree_path, data)
        edits = []
        for n in kit.walk(tree.root_node):
            if n.type == "call":
                t = n.child_by_field_name("target")
                if t is not None and t.type == "identifier" and kit.text(t, data) in names:
                    edits.append((t.start_byte, t.start_byte, module + "."))
            elif n.type == "unary_operator" and kit.text(n, data).startswith("&"):
                m = re.match(r"&(\w+)/\d+", kit.text(n, data))
                if m and m.group(1) in names:
                    edits.append((n.start_byte + 1, n.start_byte + 1, module + "."))
        return replace_ranges(data, edits).decode() if edits else block

    def calls(self, tree, data, names, skip=()):
        """Unqualified calls of ``names``."""
        for n in kit.walk(tree.root_node):
            if n.type == "call":
                t = n.child_by_field_name("target")
                if t is not None and t.type == "identifier" and kit.text(t, data) in names and \
                        not any(s <= n.start_byte < e for s, e in skip):
                    p = n.parent.parent if n.parent is not None else None
                    if p is not None and p.type == "call" and kit.text(p.child_by_field_name("target"), data) in self._DEFS:
                        continue
                    yield t


class Fortran:
    name = "fortran"
    suffixes = (".f90", ".f95", ".f03", ".f08", ".f")
    acyclic = True

    def modules(self, tree, data, path):
        out = []
        for n in tree.root_node.named_children:
            if n.type != "module":
                continue
            stmt = next((c for c in n.named_children if c.type == "module_statement"), None)
            nm = next((c for c in stmt.named_children if c.type == "name"), None) if stmt is not None else None
            if nm is None:
                continue
            end = next((c for c in n.named_children if c.type == "end_module_statement"), None)
            s, e = kit.span(n, data)
            text = kit.text(n, data)
            default_private = re.search(r"(?im)^\s*private\s*$", text) is not None
            public = set(w.lower() for m in re.finditer(r"(?im)^\s*public\s*::\s*([^\n!]+)", text) for w in re.findall(r"\w+", m.group(1)))
            private = set(w.lower() for m in re.finditer(r"(?im)^\s*private\s*::\s*([^\n!]+)", text) for w in re.findall(r"\w+", m.group(1)))
            mod = Mod(kit.text(nm, data), s, e, kit.line_end(data, stmt.start_byte),
                      kit.line_start(data, end.start_byte) if end is not None else e, node=n)
            procs = next((c for c in n.named_children if c.type == "internal_procedures"), None)
            for c in procs.named_children if procs is not None else ():
                if c.type not in ("function", "subroutine", "module_procedure"):
                    continue
                st = c.named_children[0] if c.named_children else None
                pn = next((x for x in st.named_children if x.type == "name"), None) if st is not None else None
                if pn is None:
                    continue
                name = kit.text(pn, data)
                ps, pe = kit.span(c, data)
                pub = name.lower() in public or (not default_private and name.lower() not in private)
                mod.procs.append(Proc(name, ps, pe, pub, c))
            out.append(mod)
        return out

    def derive(self, root, path):
        return path.stem

    def new_module(self, name, uses, block, like):
        head = "".join(f"  {u}\n" for u in uses)
        return f"module {name}\n{head}  implicit none\ncontains\n\n{block.strip(chr(10))}\n\nend module {name}\n"

    def indent(self):
        return ""

    def make_public(self, text, proc):
        return text

    def deps(self, data, mod):
        out = []
        for m in re.finditer(rb"(?im)^[ \t]*use[ \t]*(?:,[ \t]*\w+[ \t]*::)?[ \t]*(\w+)[^\n]*\n?", data[mod.head:mod.tail]):
            out.append((mod.head + m.start(), mod.head + m.end(), m.group().decode().strip(), "use", m.group(1).decode(), None))
        return out


LANGS = {}
for _p in (Elixir(), Fortran()):
    for _s in _p.suffixes:
        LANGS[_s] = _p


def lang_of(path):
    p = LANGS.get(path.suffix.lower())
    if p is None:
        raise Unsupported(f"No module-block language for {path.suffix} files.")
    return p


def _files(prof, root, graph, *extra):
    files = set(project_files(root, graph, prof.suffixes))
    files |= {p for p in extra if p.is_file()}
    return sorted(files)


def _find_mod(prof, draft, path, name):
    data = draft.get(path)
    tree = kit.parse(path, data)
    return next((m for m in prof.modules(tree, data, path) if m.name.lower() == name.lower()), None)


def _in_access_list(n):
    while n is not None:
        if n.type in ("public_statement", "private_statement"):
            return True
        n = n.parent
    return False


def _ieq(a, b):
    return a.lower() == b.lower()


def _insert_deps(prof, data, mod, lines):
    have = {d[2] for d in prof.deps(data, mod)}
    lines = [ln for ln in dict.fromkeys(lines) if ln not in have]
    if not lines:
        return data
    deps = prof.deps(data, mod)
    at = max(d[1] for d in deps) if deps else mod.head
    ind = "  "
    return kit.insert_lines(data, at, [ind + ln for ln in lines], blank_after=not deps and isinstance(prof, Elixir))


def plan_move(graph, node, source, to, root):
    from graphify.named_moves import _name

    prof = lang_of(source)
    label = str(node.get("label", "")).strip()
    name = _name(node)
    target = resolve_path(root, to)
    if target.suffix.lower() not in prof.suffixes:
        raise Unsupported(f"Move {prof.name} code to a {'/'.join(prof.suffixes)} file, not {to}.")
    if target == source:
        raise Unsupported(f"`{name}` is already in {kit.rel(root, source)}.")
    files = _files(prof, root, graph, source, target)
    draft = kit.Draft()
    data = draft.get(source)
    tree = kit.parse(source, data)
    mods = prof.modules(tree, data, source)
    whole = next((m for m in mods if m.name == label or _ieq(m.name, name) or m.name.endswith("." + name)), None)
    if whole is not None:
        return _move_module(prof, draft, root, source, target, whole, files)
    hits = [(m, [p for p in m.procs if _ieq(p.name, name)]) for m in mods]
    hits = [(m, ps) for m, ps in hits if ps]
    if not hits:
        raise Unsupported(f"`{name}` is not a module or a module function of {kit.rel(root, source)}.")
    smod, procs = hits[0]
    notes, conflicts = [], []
    tdata = draft.get(target) if draft.exists(target) else b""
    tmod = None
    if tdata:
        tmods = prof.modules(kit.parse(target, tdata), tdata, target)
        if not tmods:
            return MovePlan(name, str(source), str(target), {}, (f"{kit.rel(root, target)} has no module.",))
        tmod = tmods[0]
        if any(_ieq(p.name, name) for p in tmod.procs):
            return MovePlan(name, str(source), str(target), {}, (f"{tmod.name} already defines `{name}`.",))
    tname = tmod.name if tmod is not None else prof.derive(root, target)
    if not tname:
        raise Unsupported(f"Name the module of {to}: no module name follows from its path.")
    if _ieq(tname, smod.name):
        return MovePlan(name, str(source), str(target), {}, (f"{to} holds the module {tname} too.",))
    ranges = [(p.start, p.end) for p in procs]
    block = "\n\n".join(kit.piece(data, s, e) for s, e in ranges)
    rest = kit.cut(data, ranges)
    rmod = next(m for m in prof.modules(kit.parse(source, rest), rest, source) if m.name == smod.name)
    remaining = {p.name.lower(): p for p in rmod.procs}
    btree = kit.parse(source, block.encode())
    used = {w.lower() for w in kit.words(btree.root_node, block.encode())} - {name.lower()}
    rtree = kit.parse(source, rest)
    rest_uses = any(True for n in kit.tokens(rtree.root_node, rest, {name, name.lower(), name.upper()})
                    if rmod.start <= n.start_byte < rmod.end and not _in_access_list(n))
    dep_lines, need_source = [], sorted(n for n in used if n in remaining)
    for d in prof.deps(data, smod):
        _, _, line, kind, full, short = d
        if kind == "alias" and short and short.lower() in used:
            dep_lines.append(line)
        elif kind in ("import", "require"):
            dep_lines.append(line)
        elif kind == "use" and isinstance(prof, Fortran):
            m = re.match(r"(?i)(use\b[^:]*?\w+)\s*,\s*only\s*:\s*(.*)", line)
            if m:
                items = [i.strip() for i in m.group(2).split(",") if i.strip()]
                keep = [i for i in items if re.split(r"\s*=>\s*", i)[0].lower() in used]
                if keep:
                    dep_lines.append(f"{m.group(1)}, only: {', '.join(keep)}")
            else:
                dep_lines.append(line)
    publish = [remaining[n] for n in need_source if not remaining[n].public]
    if isinstance(prof, Elixir):
        if need_source:
            block = prof.qualify(block, {remaining[n].name for n in need_source}, smod.name, source)
        if not all(p.public for p in procs) and rest_uses:
            block = prof.make_public(block, procs[0])
            notes.append(f"Made `{name}` public: {smod.name} still calls it.")
    else:
        spec = data[smod.head:smod.procs[0].start if smod.procs else smod.tail].decode(errors="replace")
        entities = {w.lower() for m in re.finditer(r"(?im)^[^!\n]*::\s*([^\n!]+)", spec) if not re.match(r"(?i)\s*(public|private)\b", m.group())
                    for w in re.findall(r"([A-Za-z_]\w*)(?:\s*(?:=|\(|,|$))", m.group(1))}
        need_source += sorted(n for n in used & entities if n not in need_source)
        if need_source:
            dep_lines.append(f"use {smod.name}, only: {', '.join(need_source)}")
            if rest_uses:
                conflicts.append(f"The move makes modules {smod.name} and {tname} use each other, and Fortran forbids that "
                                 f"cycle. Move what `{name}` needs from {smod.name} as well.")
    if conflicts:
        return MovePlan(name, str(source), str(target), {}, tuple(conflicts), tuple(notes))
    # the code that stays
    if isinstance(prof, Elixir):
        edits = [(t.start_byte, t.start_byte, tname + ".") for t in prof.calls(rtree, rest, {name})
                 if rmod.start <= t.start_byte < rmod.end]
        rest = replace_ranges(rest, edits)
    else:
        rest = _fortran_unpublish(rest, rmod.name, name, source)
        if rest_uses:
            m2 = next(m for m in prof.modules(kit.parse(source, rest), rest, source) if m.name == smod.name)
            rest = _insert_deps(prof, rest, m2, [f"use {tname}, only: {name}"])
    if publish:
        m2 = next(m for m in prof.modules(kit.parse(source, rest), rest, source) if m.name == smod.name)
        for p in sorted((x for x in m2.procs if x.name.lower() in {q.name.lower() for q in publish}), key=lambda p: -p.start):
            if isinstance(prof, Elixir):
                rest = rest[: p.start] + prof.make_public(rest[p.start:p.end].decode(), p).encode() + rest[p.end:]
            notes.append(f"Made `{p.name}` public in {smod.name}: the moved code uses it.")
    if isinstance(prof, Fortran) and need_source:
        opened = _fortran_publish(rest, smod.name, need_source, source)
        if opened != rest:
            rest = opened
            notes.append(f"Made {', '.join(f'`{n}`' for n in need_source)} public in {smod.name}: the moved code uses "
                         f"{'it' if len(need_source) == 1 else 'them'}.")
    draft.put(source, rest)
    # the target
    if tmod is None:
        new = prof.new_module(tname, list(dict.fromkeys(dep_lines)), block, smod)
        if isinstance(prof, Fortran) and re.search(r"(?im)^\s*private\s*$", kit.text(smod.node, data)):
            new = new.replace("  implicit none\n", f"  implicit none\n  private\n  public :: {name}\n", 1)
        draft.put(target, new.encode())
    else:
        ind = prof.indent()
        tblock = kit.reindent(block, ind, ind) if ind else block
        tdata = kit.append_block(tdata, tblock, tmod.tail if isinstance(prof, Elixir) or re.search(
            rb"(?im)^\s*contains\s*$", tdata[tmod.start:tmod.end]) else None)
        if isinstance(prof, Fortran) and not re.search(rb"(?im)^\s*contains\s*$", tdata):
            m3 = prof.modules(kit.parse(target, tdata), tdata, target)[0]
            tdata = tdata[: m3.tail] + b"contains\n\n" + tblock.strip("\n").encode() + b"\n\n" + tdata[m3.tail:]
        elif isinstance(prof, Fortran):
            m3 = prof.modules(kit.parse(target, tdata), tdata, target)[0]
            tdata = kit.append_block(tdata, tblock, m3.tail)
        else:
            m3 = prof.modules(kit.parse(target, tdata), tdata, target)[0]
            tdata = kit.append_block(tdata, tblock, m3.tail)
        m3 = prof.modules(kit.parse(target, tdata), tdata, target)[0]
        tdata = _insert_deps(prof, tdata, m3, dep_lines)
        if isinstance(prof, Fortran):
            tdata = _fortran_publish(tdata, tname, [name], target)
        draft.put(target, tdata)
    # every other file
    for f in files:
        if f == target and isinstance(prof, Fortran):
            continue
        if isinstance(prof, Elixir):
            _elixir_retarget(prof, draft, f, smod.name, tname, name)
        else:
            _fortran_retarget(prof, draft, f, smod.name, tname, name)
    if not Path(target).is_file():
        from graphify.path_moves import _build_paths

        _build_paths(draft, root, source, target, True, notes)
    return MovePlan(name, str(source), str(target), draft.final(), (), tuple(dict.fromkeys(notes)))


def _fortran_unpublish(data, module, name, path):
    """Drop ``name`` from the ``public ::`` lists of ``module``."""
    def fix(m):
        items = [i.strip() for i in m.group(2).split(",") if i.strip()]
        keep = [i for i in items if not _ieq(i, name)]
        if keep == items:
            return m.group()
        return f"{m.group(1)}{', '.join(keep)}\n" if keep else ""
    return re.sub(rb"(?im)^([ \t]*public[ \t]*::[ \t]*)([^\n!]*)\n", lambda m: fix(
        type("M", (), {"group": lambda self, i=0: m.group(i).decode()})()).encode(), data)


def _fortran_publish(data, module, names, path):
    """Add ``names`` to a ``public ::`` list when ``module`` is private by default."""
    tree = kit.parse(path, data)
    mod = next((m for m in Fortran().modules(tree, data, path) if _ieq(m.name, module)), None)
    if mod is None:
        return data
    text = data[mod.start:mod.end].decode()
    if not re.search(r"(?im)^\s*private\s*$", text):
        return data
    have = {w.lower() for m in re.finditer(r"(?im)^\s*public\s*::\s*([^\n!]+)", text) for w in re.findall(r"\w+", m.group(1))}
    add = [n for n in names if n.lower() not in have]
    if not add:
        return data
    m = re.search(rb"(?im)^([ \t]*)private[ \t]*\n", data[mod.start:mod.end])
    at = mod.start + m.end()
    return data[:at] + f"{m.group(1).decode()}public :: {', '.join(add)}\n".encode() + data[at:]


def _fortran_retarget(prof, draft, f, smod, tmod, name):
    data = draft.get(f)
    if smod.lower().encode() not in data.lower():
        return
    tree = kit.parse(f, data)
    out = data
    pat = re.compile(rb"(?im)^([ \t]*)use[ \t]*(?:,[ \t]*\w+[ \t]*::)?[ \t]*" + re.escape(smod.encode()) + rb"\b([^\n!]*)")
    for m in reversed(list(pat.finditer(data))):
        if _comment_at(tree, m.start()):
            continue
        ind, rest = m.group(1).decode(), m.group(2).decode()
        only = re.match(r"(?i)\s*,\s*only\s*:\s*(.*)", rest)
        if only:
            items = [i.strip() for i in only.group(1).split(",") if i.strip()]
            mine = [i for i in items if _ieq(re.split(r"\s*=>\s*", i)[-1], name)]
            if not mine:
                continue
            keep = [i for i in items if i not in mine]
            line = (f"{ind}use {smod}, only: {', '.join(keep)}\n" if keep else "") + f"{ind}use {tmod}, only: {', '.join(mine)}"
            out = out[: m.start()] + line.encode() + out[m.end():]
        else:
            uses = any(True for _ in kit.tokens(tree.root_node, data, {name, name.lower(), name.upper()}))
            if uses and not re.search(rb"(?im)^[ \t]*use[ \t]+" + re.escape(tmod.encode()) + rb"\b", data):
                out = out[: m.end()] + f"\n{ind}use {tmod}".encode() + out[m.end():]
    if out != data:
        draft.put(f, out)


def _elixir_retarget(prof, draft, f, smod, tmod, name):
    data = draft.get(f)
    if smod.split(".")[-1].encode() not in data:
        return
    tree = kit.parse(f, data)
    lines = data.decode()
    aliases = {}
    for m in re.finditer(r"(?m)^([ \t]*)alias[ \t]+([\w.]+)(?:[ \t]*,[ \t]*as:[ \t]*(\w+))?", lines):
        aliases[m.group(3) or m.group(2).split(".")[-1]] = (m.group(2), m.end(), m.group(1))
    t_short = next((s for s, (full, _, _) in aliases.items() if full == tmod), None)
    prefixes = [smod] + [s for s, (full, _, _) in aliases.items() if full == smod]
    edits, add_alias = [], None
    for pre in prefixes:
        for m in re.finditer(rf"(?<![\w.]){re.escape(pre)}\.{re.escape(name)}\b", lines):
            b = len(lines[: m.start()].encode())
            if _comment_at(tree, b):
                continue
            if pre == smod:
                new = t_short or tmod
            else:
                short = tmod.split(".")[-1]
                if t_short:
                    new = t_short
                elif short not in aliases or aliases[short][0] == tmod:
                    new, add_alias = short, aliases[pre]
                else:
                    new = tmod
            edits.append((b, b + len(pre.encode()), new))
    imp = re.search(rf"(?m)^([ \t]*)import[ \t]+{re.escape(smod)}\b[^\n]*", lines)
    out = replace_ranges(data, edits) if edits else data
    if add_alias is not None:
        _, end, ind = add_alias
        at = len(lines[:end].encode())
        at = kit.line_end(out, at)
        out = out[:at] + f"{ind}alias {tmod}\n".encode() + out[at:]
    if imp is not None and not re.search(rf"(?m)^[ \t]*import[ \t]+{re.escape(tmod)}\b", lines):
        if any(True for _ in prof.calls(kit.parse(f, out), out, {name})):
            at = kit.line_end(out, out.find(imp.group().encode()))
            out = out[:at] + f"{imp.group(1)}import {tmod}\n".encode() + out[at:]
    if out != data:
        draft.put(f, out)


def _move_module(prof, draft, root, source, target, mod, files):
    data = draft.get(source)
    block = kit.piece(data, mod.start, mod.end)
    rest = kit.cut(data, [(mod.start, mod.end)])
    notes = []
    if draft.exists(target):
        tdata = draft.get(target)
        if any(_ieq(m.name, mod.name) for m in prof.modules(kit.parse(target, tdata), tdata, target)):
            return MovePlan(mod.name, str(source), str(target), {}, (f"{kit.rel(root, target)} already defines {mod.name}.",))
        draft.put(target, kit.append_block(tdata, block))
    else:
        draft.put(target, (block.strip("\n") + "\n").encode())
    from graphify.path_moves import _build_paths

    if rest.strip():
        draft.put(source, rest)
        if not Path(target).is_file():
            _build_paths(draft, root, source, target, True, notes)
    else:
        draft.remove(source)
        notes.append(f"Deleted {kit.rel(root, source)}: no code stays in it.")
        _build_paths(draft, root, source, target, False, notes)
    if isinstance(prof, Fortran) and not Path(target).is_file() and not notes:
        notes.append(f"Compile {kit.rel(root, target)} before the files that use {mod.name}.")
    return MovePlan(mod.name, str(source), str(target), draft.final(), (), tuple(notes))


def plan_move_file(graph, old, new, root, tests=True):
    from graphify.path_moves import _build_paths

    prof = lang_of(old)
    target = resolve_path(root, new)
    if target.suffix.lower() not in prof.suffixes:
        raise Unsupported(f"Keep a {prof.name} suffix: {new}.")
    if target.exists():
        return MovePlan(kit.rel(root, old), str(old), str(target), {}, (f"{kit.rel(root, target)} already exists.",))
    files = _files(prof, root, graph, old)
    pairs = [(old, target)]
    renames, notes = [], []
    if tests:
        pairs += [(t, n) for t, n, *_ in kit.test_pairs(root, old, target) if not n.exists() and t.suffix in prof.suffixes]
    draft = kit.Draft()
    names = {}
    if isinstance(prof, Elixir):
        for o, n in pairs:
            data = draft.get(o)
            was, will = prof.derive(root, o), prof.derive(root, n)
            for m in prof.modules(kit.parse(o, data), data, o):
                if was and will and (m.name == was or m.name.startswith(was + ".")) and was != will:
                    names[was] = will
    for f in files:
        data = draft.get(f)
        text = data.decode()
        out = text
        for was, will in names.items():
            out = re.sub(rf"(?<![\w.]){re.escape(was)}(?![\w])", will, out)
            wl, nl = was.split(".")[-1], will.split(".")[-1]
            if wl != nl and re.search(rf"(?m)^[ \t]*alias[ \t]+{re.escape(will)}[ \t]*$", out):
                out = re.sub(rf"(?<![\w.]){re.escape(wl)}(?=\.)", nl, out)
        if out != text:
            draft.put(f, out.encode())
    for was, will in names.items():
        renames.append((was, will))
    for o, n in pairs:
        data = draft.get(o)
        draft.remove(o)
        draft.put(n, data)
        if o != old:
            renames.append((kit.rel(root, o), kit.rel(root, n)))
        _build_paths(draft, root, o, n, False, notes)
    return MovePlan(kit.rel(root, old), str(old), str(target), draft.final(), (), tuple(dict.fromkeys(notes)), (),
                    tuple(dict.fromkeys(renames)))
