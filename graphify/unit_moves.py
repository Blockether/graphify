"""Moves for languages that name a module after its file.

Erlang (``-module``), OCaml (the file is the module), Pascal (``unit``) and Apex (one
class in each file) use this model. A file move renames the module in the file, in each
qualified use (``m:f()``, ``M.x``, ``A.f()``), import, ``uses`` list and ``open``, and in
the paths of include files and project files. A definition move changes the qualifier of
each use and the export lists or interface sections. The moved code gets a qualifier or a
``uses`` entry for each name that it uses from its old module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from graphify import move_kit as kit
from graphify.moves import MovePlan, project_files, replace_ranges, resolve_path
from graphify.refactor import Unsupported

__all__ = ["LANGS", "plan_move", "plan_move_file"]


@dataclass
class Decl:
    name: str
    parts: list  # [(start, end)]: whole lines
    kind: str = "def"
    public: bool = True
    names: tuple = ()  # more names that move with it: constructors, fields
    extra: dict = field(default_factory=dict)


@dataclass
class Ctx:
    """The state of one plan."""

    lang: "Unit"
    root: Path
    files: list
    draft: kit.Draft = field(default_factory=kit.Draft)
    conflicts: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    renames: list = field(default_factory=list)

    def rel(self, p) -> str:
        return kit.rel(self.root, p)


def _blank(data: bytes, spans) -> bytes:
    out = bytearray(data)
    for s, e in spans:
        out[s:e] = re.sub(rb"[^\n]", b" ", bytes(out[s:e]))
    return bytes(out)


def _mask(tree, data: bytes, strings: bool = True) -> bytes:
    """``data`` with comments (and strings) blanked: a word in it is code."""
    words = ("comment", "string", "character") if strings else ("comment",)
    spans = []

    def rec(n):
        if any(w in n.type.lower() for w in words):
            spans.append((n.start_byte, n.end_byte))
            return
        for c in n.children:
            rec(c)

    rec(tree.root_node)
    return _blank(data, spans)


def _edit_all(data: bytes, masked: bytes, pattern, repl, flags=0):
    """``(start, end, text)`` for each match of ``pattern`` in the code of ``masked``.

    ``repl`` gets the match and returns the new text of group 1 (its span), or None.
    """
    out = []
    for m in re.finditer(pattern, masked, flags):
        new = repl(m)
        if new is not None:
            out.append((m.start(1), m.end(1), new if isinstance(new, bytes) else new.encode()))
    return out


def _apply(ctx: Ctx, path: Path, edits) -> None:
    if edits:
        data = ctx.draft.get(path)
        ctx.draft.put(path, replace_ranges(data, sorted(set(edits))))


def _cut(data: bytes, parts) -> bytes:
    return kit.tidy(kit.cut(data, parts)) if parts else data


def _block(data: bytes, parts) -> str:
    return "\n\n".join(kit.piece(data, s, e) for s, e in sorted(parts))


class Unit:
    name = ""
    suffixes: tuple = ()  # files that refer to modules of the language
    code: tuple = ()  # files that hold movable definitions
    moved_files: tuple = ()  # more files that only a file move takes
    sep = "."
    fold = False  # names ignore case

    @property
    def flags(self) -> int:
        return re.I if self.fold else 0

    def key(self, s: str) -> str:
        return s.lower() if self.fold else s

    def same(self, a: str, b: str) -> bool:
        return self.key(a) == self.key(b)

    def module_of(self, path: Path) -> str:
        return path.stem

    def node_name(self, node: dict) -> str:
        from graphify.named_moves import _name

        return _name(node)

    def decls(self, tree, data: bytes, path: Path) -> list[Decl]:
        return []

    def parse(self, ctx: Ctx, path: Path):
        data = ctx.draft.get(path)
        return data, kit.parse(path, data)

    def files_of(self, root: Path, graph: dict, *extra) -> list[Path]:
        files = set(project_files(root, graph, self.suffixes))
        return sorted(files | {p for p in extra if p.is_file()})

    def qualified(self, module: str, name: str | None = None) -> bytes:
        """A pattern of a use of ``module`` as a qualifier; group 1 is the module."""
        tail = rf"\s*{re.escape(self.sep)}\s*" + (re.escape(name) + r"(?![\w$])" if name else r"(?=[\w'(])")
        return rf"(?<![\w$'.@#?]){'(' + re.escape(module) + ')'}{tail}".encode()

    def requalify(self, data: bytes, masked: bytes, old: str, new: str, name: str | None = None):
        return _edit_all(data, masked, self.qualified(old, name), lambda m: new, self.flags)

    def companions(self, old: Path, new: Path) -> list[tuple[Path, Path]]:
        """Files that move with ``old``: an interface, a form, a metadata file."""
        return []


# -- Erlang --------------------------------------------------------------------------
def _arity(data: bytes, i: int) -> int:
    """The number of arguments in the parentheses that start at or after ``i``."""
    j = data.find(b"(", i)
    if j < 0:
        return 0
    depth, n, k = 0, 0, j
    seen = False
    while k < len(data):
        c = data[k:k + 1]
        if c in b"([{":
            depth += 1
        elif c in b")]}":
            depth -= 1
            if depth == 0:
                break
        elif c == b"," and depth == 1:
            n += 1
        elif not c.isspace() and depth >= 1:
            seen = True
        k += 1
    return n + 1 if seen else 0


_ERL_ATTR = re.compile(rb"(?s)-(export|export_type)\s*\(\s*\[(.*?)\]\s*\)\s*\.")
_ERL_IMPORT = re.compile(rb"(?s)-import\s*\(\s*'?(\w+)'?\s*,\s*\[(.*?)\]\s*\)\s*\.")
_ERL_INCLUDE = re.compile(rb'-include(_lib)?\s*\(\s*"([^"]+)"\s*\)\s*\.')


def _fa(entry: bytes) -> tuple[str, int] | None:
    m = re.fullmatch(rb"\s*'?(\w+)'?\s*/\s*(\d+)\s*", entry)
    return (m.group(1).decode(), int(m.group(2))) if m else None


class Erlang(Unit):
    name = "Erlang"
    suffixes = (".erl", ".hrl", ".escript", ".app.src", ".app")
    code = (".erl", ".hrl", ".escript")
    sep = ":"

    def module_of(self, path):
        return path.name.split(".")[0]

    def node_name(self, node):
        label = re.sub(r"\(\)$", "", re.sub(r"/\d+$", "", str(node.get("label", ""))))
        return re.split(r"[:#?]", label)[-1].strip("'")

    def decls(self, tree, data, path):
        found: dict[tuple, Decl] = {}
        for n in tree.root_node.children:
            kind, name = None, None
            if n.type == "fun_decl":
                cl = next((c for c in n.named_children if c.type == "function_clause"), None)
                a = cl.named_children[0] if cl is not None and cl.named_children else None
                if a is not None and a.type == "atom":
                    kind, name = "fun", kit.text(a, data).strip("'")
            elif n.type in ("type_alias", "opaque"):
                m = re.match(rb"-(?:type|opaque)\s+'?(\w+)'?", data[n.start_byte:n.end_byte])
                kind, name = "type", m and m.group(1).decode()
            elif n.type == "record_decl":
                m = re.match(rb"-record\s*\(\s*'?(\w+)'?", data[n.start_byte:n.end_byte])
                kind, name = "record", m and m.group(1).decode()
            elif n.type == "pp_define":
                m = re.match(rb"-define\s*\(\s*'?(\w+)'?", data[n.start_byte:n.end_byte])
                kind, name = "macro", m and m.group(1).decode()
            if not name:
                continue

            def above(p, name=name):
                t = data[p.start_byte:p.end_byte]
                return (p.type == "spec" and re.match(rb"-spec\s+'?" + re.escape(name.encode()) + rb"\b", t) is not None) or \
                    (t.startswith(b"-doc") and kind == "fun")

            s, e = kit.span(n, data, above)
            d = found.setdefault((kind, name), Decl(name, [], kind, extra={"arities": set()}))
            d.parts.append((s, e))
            if kind in ("fun", "type"):
                d.extra["arities"].add(_arity(data, n.start_byte + (data[n.start_byte:].find(name.encode()))))
        return list(found.values())

    def mod_name(self, data: bytes, path: Path) -> str:
        m = re.search(rb"-module\s*\(\s*'?(\w+)'?\s*\)", data)
        return m.group(1).decode() if m else self.module_of(path)

    # -- export lists
    def drop_exports(self, data: bytes, names: set[str], attr: str = "export") -> tuple[bytes, list[bytes]]:
        """``data`` without the export entries of ``names``, and those entries."""
        gone, edits = [], []
        for m in _ERL_ATTR.finditer(data):
            if m.group(1).decode() != attr:
                continue
            entries = [x for x in m.group(2).split(b",") if x.strip()]
            keep = [x for x in entries if (_fa(x) or ("", 0))[0] not in names]
            if len(keep) == len(entries):
                continue
            gone += [x.strip() for x in entries if x not in keep]
            if keep:
                edits.append((m.start(2), m.end(2), b",".join(keep).strip()))
            else:
                s, e = kit.line_start(data, m.start()), kit.line_end(data, m.end() - 1)
                edits.append((s, e, b""))
        return replace_ranges(data, edits), gone

    def add_exports(self, data: bytes, entries: list[str], attr: str = "export") -> bytes:
        have = {(_fa(x) or ("", 0)) for m in _ERL_ATTR.finditer(data) if m.group(1).decode() == attr
                for x in m.group(2).split(b",")}
        entries = [x for x in dict.fromkeys(entries) if _fa(x.encode()) not in have]
        if not entries:
            return data
        for m in _ERL_ATTR.finditer(data):
            if m.group(1).decode() == attr:
                body = m.group(2).rstrip()
                add = (b", " if body.strip() else b"") + ", ".join(entries).encode()
                return data[: m.start(2) + len(body)] + add + data[m.start(2) + len(body):]
        line = f"-{attr}([{', '.join(entries)}]).".encode()
        anchor = None
        for m in _ERL_ATTR.finditer(data):
            anchor = kit.line_end(data, m.end() - 1)
        if anchor is None:
            mm = re.search(rb"-module\s*\([^)]*\)\s*\.[^\n]*\n?", data)
            anchor = mm.end() if mm else 0
            return kit.insert_lines(data, anchor, [line.decode()], blank_before=bool(mm), blank_after=True)
        return kit.insert_lines(data, anchor, [line.decode()])

    # -- references
    def calls(self, name: str) -> bytes:
        """A bare call or ``fun f/N`` of ``name``; group 1 is the name."""
        n = re.escape(name)
        return rf"(?<![\w$'.:?#@])({n})(?=\s*\()|(?<=fun)\s+({n})(?=\s*/\s*\d)".encode()

    def bare_edits(self, masked: bytes, name: str, module: str, skip=()):
        out = []
        for m in re.finditer(self.calls(name), masked):
            g = 1 if m.group(1) else 2
            if any(s <= m.start(g) < e for s, e in skip):
                continue
            if g == 1 and re.search(rb"(?:^|\n)\s*$", masked[:m.start(g)][-200:]) and \
                    re.match(rb"\s*\([^\n]*?\)\s*(?:when\b[^\n]*)?->", masked[m.end(g):]):
                continue  # a clause head
            out.append((m.start(g), m.end(g), f"{module}:{name}".encode()))
        return out

    def mfa(self, old: str, new: str, name: str | None = None) -> tuple:
        n = re.escape(name) if name else r"'?[a-z]\w*'?"
        return rf"[({{,]\s*({re.escape(old)})\s*,\s*{n}\s*,".encode(), new

    # -- moves
    def move(self, ctx: Ctx, source: Path, target: Path, name: str, ds: list[Decl]) -> None:
        data, tree = self.parse(ctx, source)
        src = self.mod_name(data, source)
        all_decls = self.decls(tree, data, source)
        kinds = {d.kind for d in ds}
        if kinds & {"record", "macro"}:
            return self.move_header(ctx, source, target, name, ds, data, tree)
        if target.suffix != ".erl" and source.suffix != target.suffix:
            raise Unsupported(f"Move Erlang functions and types to a .erl file, not {ctx.rel(target)}.")
        dst = self.module_of(target)
        texists = ctx.draft.exists(target)
        tdata = ctx.draft.get(target) if texists else b""
        if texists:
            tt = kit.parse(target, tdata)
            if any(d.name == name and d.kind in kinds for d in self.decls(tt, tdata, target)):
                ctx.conflicts.append(f"{ctx.rel(target)} already defines `{name}`.")
                return
        parts = [p for d in ds for p in d.parts]
        masked = _mask(tree, data)
        inner = [(s, e) for s, e in parts]
        moved_block = data
        # 1. the moved code: qualify the names of the old module, keep its imports and includes.
        block_edits, need_src = [], set()
        others = [d for d in all_decls if d not in ds]
        for d in others:
            if d.kind == "fun":
                for s, e in inner:
                    for es, ee, txt in self.bare_edits(masked[:e], d.name, src):
                        if es >= s:
                            block_edits.append((es, ee, txt))
                            need_src |= {f"{d.name}/{a}" for a in d.extra["arities"]}
            elif d.kind == "type":
                pat = rf"(?<![\w$'.:?#@])({re.escape(d.name)})(?=\s*\()".encode()
                for s, e in inner:
                    for m in re.finditer(pat, masked[s:e]):
                        block_edits.append((s + m.start(1), s + m.end(1), f"{src}:{d.name}".encode()))
                        need_src |= {f"{d.name}/{a}" for a in d.extra["arities"]}
            elif d.kind in ("record", "macro"):
                pat = (rb"\?" if d.kind == "macro" else rb"#") + re.escape(d.name.encode()) + rb"\b"
                if any(re.search(pat, masked[s:e]) for s, e in inner):
                    ctx.conflicts.append(f"`{name}` uses the {d.kind} `{d.name}` of {ctx.rel(source)}: move it to a .hrl file first.")
        if any(re.search(rb"\?MODULE\b", masked[s:e]) for s, e in inner):
            ctx.conflicts.append(f"`{name}` uses ?MODULE, which names {dst} after the move.")
        moved_block = replace_ranges(data, block_edits)
        shift = _shifted(parts, block_edits)
        block = _block(moved_block, shift)
        heads = []
        bw = set(re.findall(rb"[a-z]\w*", block.encode()))
        for m in _ERL_IMPORT.finditer(masked):
            mod = data[m.start(1):m.end(1)].decode()
            names = [x for x in data[m.start(2):m.end(2)].split(b",") if _fa(x) and _fa(x)[0].encode() in bw]
            if names:
                heads.append(f"-import({mod}, [{', '.join(x.strip().decode() for x in names)}]).")
        for m in self.includes(data, masked):
            raw = data[m.start(2):m.end(2)].decode()
            hdr = self.include_target(ctx, source, raw, bool(m.group(1)))
            hd = hdr.read_bytes() if hdr is not None and hdr.is_file() else b""
            defs = set(re.findall(rb"-(?:define|record)\s*\(\s*'?(\w+)", hd))
            if not hdr or any(re.search(rb"[?#]" + re.escape(w) + rb"\b", block.encode()) for w in defs):
                raw2 = raw
                if hdr and not m.group(1) and hdr not in ((target.parent / raw).resolve(),
                                                          (target.parent.parent / "include" / raw).resolve()):
                    raw2 = kit.relative_path(target.parent, hdr)
                heads.append(f"-include{'_lib' if m.group(1) else ''}(\"{raw2}\").")
        # 2. the old module: cut the code, its exports and specs; its calls go to the new module.
        rest = _cut(data, parts)
        exported = set()
        for attr in ("export", "export_type"):
            rest, gone = self.drop_exports(rest, {name}, attr)
            exported |= {(attr, g.decode()) for g in gone}
        rm = _mask(kit.parse(source, rest), rest)
        rest_edits = self.bare_edits(rm, name, dst) if "fun" in kinds else []
        if "type" in kinds:
            pat = rf"(?<![\w$'.:?#@])({re.escape(name)})(?=\s*\()".encode()
            rest_edits += [(m.start(1), m.end(1), f"{dst}:{name}".encode()) for m in re.finditer(pat, rm)
                           if not re.match(rb"\s*\([^)]*\)\s*->", rm[m.end(1):])]
        rest = replace_ranges(rest, rest_edits)
        rest = self.add_exports(rest, sorted(need_src))
        ctx.draft.put(source, rest)
        # 3. the new module: the code and its exports.
        ar = sorted({(d.kind, a) for d in ds for a in d.extra["arities"]})
        want = {("export" if k == "fun" else "export_type", f"{name}/{a}") for k, a in ar}
        external = bool(rest_edits) or self.used_elsewhere(ctx, source, target, src, name)
        exports = sorted(want if external else exported)
        if texists:
            out = kit.append_block(tdata, block)
        else:
            out = f"-module({dst}).\n\n".encode() + (b"\n".join(h.encode() for h in heads) + b"\n\n" if heads else b"") \
                + block.encode() + b"\n"
        for attr in ("export", "export_type"):
            out = self.add_exports(out, [e for a, e in exports if a == attr], attr)
        if texists and heads:
            have = out
            new_heads = [h for h in heads if h.encode() not in have]
            if new_heads:
                tm = re.search(rb"(?s).*(?:-(?:module|export|export_type|import|include|include_lib)\b[^\n]*\.[ \t]*\n)", out)
                out = kit.insert_lines(out, tm.end() if tm else 0, new_heads)
        ctx.draft.put(target, out)
        # 4. the other files: remote calls, imports and MFA tuples.
        for f in ctx.files:
            if f in (source,) or f.suffix not in self.code:
                continue
            fd, ft = self.parse(ctx, f)
            fm = _mask(ft, fd)
            local = f == target
            if local:
                pat = rf"(?<![\w$'.@#?])({re.escape(src)}\s*:\s*){re.escape(name)}(?![\w$])".encode()
                edits = _edit_all(fd, fm, pat, lambda m: b"")
            else:
                edits = self.requalify(fd, fm, src, dst, name)
            pat, new = self.mfa(src, dst, name)
            edits += _edit_all(fd, fm, pat, lambda m: new)
            fd2 = replace_ranges(fd, edits)
            fd2 = self.move_imports(fd2, src, dst, name)
            if fd2 != fd:
                ctx.draft.put(f, fd2)

    def used_elsewhere(self, ctx, source, target, src, name) -> bool:
        pat = self.qualified(src, name)
        for f in ctx.files:
            if f != source and f.suffix in self.code:
                fd = ctx.draft.get(f)
                if re.search(pat, fd) or re.search(rb"-import\s*\(\s*" + re.escape(src.encode()) + rb"\b[^.]*\b"
                                                   + re.escape(name.encode()) + rb"\s*/", fd):
                    return True
        return False

    def move_imports(self, data: bytes, src: str, dst: str, name: str) -> bytes:
        edits = []
        for m in _ERL_IMPORT.finditer(data):
            if m.group(1).decode() != src:
                continue
            entries = [x for x in m.group(2).split(b",") if x.strip()]
            moved = [x.strip() for x in entries if (_fa(x) or ("", 0))[0] == name]
            if not moved:
                continue
            keep = [x for x in entries if x.strip() not in moved]
            line = f"-import({dst}, [{', '.join(x.decode() for x in moved)}]).".encode()
            if keep:
                edits.append((m.start(2), m.end(2), b",".join(keep).strip()))
                e = kit.line_end(data, m.end() - 1)
                edits.append((e, e, line + b"\n"))
            else:
                edits.append((m.start(), m.end(), line))
        return replace_ranges(data, edits)

    def includes(self, data: bytes, masked: bytes):
        """The ``-include`` attributes outside comments."""
        return [m for m in _ERL_INCLUDE.finditer(data) if masked[m.start():m.start() + 1] == b"-"]

    def include_target(self, ctx, path: Path, raw: str, lib: bool) -> Path | None:
        if lib:
            parts = Path(raw).parts
            for cand in [ctx.root / Path(*parts[1:]), ctx.root / "apps" / raw, ctx.root / "lib" / raw]:
                if cand.is_file() or ctx.draft.exists(cand):
                    return cand.resolve()
            return None
        for base in (path.parent, path.parent.parent / "include", ctx.root / "include", ctx.root):
            cand = (base / raw).resolve()
            if ctx.draft.exists(cand):
                return cand
        return None

    def render_include(self, raw: str, old: Path, new: Path, at: Path) -> str:
        """The path of the header ``new`` from ``at``, in the style of ``raw``, which reaches ``old``."""
        for base in (at.parent, at.parent.parent / "include"):
            if (base / raw).resolve() == old:
                rel = kit.relative_path(base, new)
                if not rel.startswith("../") or base == at.parent:
                    return rel
        return kit.relative_path(at.parent, new)

    def move_header(self, ctx, source, target, name, ds, data, tree):
        if target.suffix != ".hrl":
            raise Unsupported(f"Move Erlang records and macros to a .hrl file, not {ctx.rel(target)}.")
        parts = [p for d in ds for p in d.parts]
        block = _block(data, parts)
        rest = _cut(data, parts)
        texists = ctx.draft.exists(target)
        tdata = ctx.draft.get(target) if texists else b""
        ctx.draft.put(target, kit.append_block(tdata, block) if texists else block.encode() + b"\n")
        pat = (rb"\?" if ds[0].kind == "macro" else rb"#") + re.escape(name.encode()) + rb"\b"
        users = [source] if re.search(pat, _mask(kit.parse(source, rest), rest)) else []
        if source.suffix == ".hrl":
            for f in ctx.files:
                if f.suffix in self.code and f not in (source, target):
                    fd = ctx.draft.get(f)
                    if re.search(pat, fd) and any(self.include_target(ctx, f, m.group(2).decode(), bool(m.group(1))) == source
                                                  for m in _ERL_INCLUDE.finditer(fd)):
                        users.append(f)
        ctx.draft.put(source, rest)
        for f in users:
            fd = ctx.draft.get(f)
            if any(self.include_target(ctx, f, m.group(2).decode(), bool(m.group(1))) == target
                   for m in _ERL_INCLUDE.finditer(fd)):
                continue
            line = f'-include("{kit.relative_path(f.parent, target)}").'
            incs = list(_ERL_INCLUDE.finditer(fd))
            if incs:
                at = kit.line_end(fd, incs[-1].end() - 1)
            else:
                m = re.search(rb"(?s).*-(?:module|export|export_type|import)\b[^\n]*\.[ \t]*\n", fd)
                at = m.end() if m else 0
            ctx.draft.put(f, kit.insert_lines(fd, at, [line]))
            ctx.notes.append(f"{ctx.rel(f)} includes {ctx.rel(target)} now.")

    def move_file(self, ctx: Ctx, old: Path, new: Path) -> None:
        old_mod, new_mod = self.module_of(old), self.module_of(new)
        renamed = old.suffix in (".erl", ".escript") and old_mod != new_mod
        if renamed:
            clash = [f for f in ctx.files if f.suffix == ".erl" and self.module_of(f) == new_mod and f != old]
            if clash:
                ctx.conflicts.append(f"Module {new_mod} exists already: {ctx.rel(clash[0])}.")
            ctx.renames.append((old_mod, new_mod))
        for f in ctx.files:
            app = f.name.endswith((".app.src", ".app"))
            fd = ctx.draft.get(f)
            if not fd:
                continue
            fm = fd if app else _mask(kit.parse(f, fd), fd)
            at = new if f == old else f
            edits = []
            if renamed:
                q = re.escape(old_mod)
                if f == old:
                    edits += _edit_all(fd, fm, rf"-module\s*\(\s*'?({q})'?\s*\)".encode(), lambda m: new_mod)
                if app:
                    for m in re.finditer(rb"(?s)\{\s*modules\s*,\s*\[(.*?)\]", fd):
                        for w in re.finditer(rb"(?<![\w'])" + q.encode() + rb"(?![\w'])", m.group(1)):
                            edits.append((m.start(1) + w.start(), m.start(1) + w.end(), new_mod.encode()))
                    edits += _edit_all(fd, fd, rf"\{{\s*mod\s*,\s*\{{\s*({q})\b".encode(), lambda m: new_mod)
                else:
                    edits += self.requalify(fd, fm, old_mod, new_mod)
                    edits += _edit_all(fd, fm, rf"-(?:import|behaviou?r)\s*\(\s*'?({q})'?\s*[,)]".encode(), lambda m: new_mod)
                    pat, _ = self.mfa(old_mod, new_mod)
                    edits += _edit_all(fd, fm, pat, lambda m: new_mod)
                    left = [m for m in re.finditer(rb"(?<![\w'$?#])" + q.encode() + rb"(?![\w'(])", fm)]
                    done = {s for s, _, _ in edits}
                    left = [m for m in left if m.start() not in done and not (f == old and fm[:m.start()].rstrip().endswith(b"-module("))]
                    if left:
                        ctx.notes.append(f"{ctx.rel(f)} uses the atom `{old_mod}` {len(left)} more times: check them.")
            if not app:
                for m in self.includes(fd, fm):
                    raw = fd[m.start(2):m.end(2)].decode()
                    hdr = self.include_target(ctx, f, raw, bool(m.group(1)))
                    if hdr is None or m.group(1):
                        if m.group(1) and hdr == old and old.name != new.name:
                            edits.append((m.start(2), m.end(2), raw[: -len(old.name)].encode() + new.name.encode()))
                        continue
                    if hdr != old and f != old:
                        continue
                    nr = self.render_include(raw, hdr, new if hdr == old else hdr, at) if f != old or hdr == old else None
                    if f == old and hdr != old:
                        nr = kit.relative_path(new.parent, hdr) if not (new.parent / raw).resolve() == hdr else raw
                    if nr and nr != raw:
                        edits.append((m.start(2), m.end(2), nr.encode()))
            _apply(ctx, f, edits)


def _shifted(parts, edits):
    """``parts`` after ``edits`` on the same bytes."""
    out = []
    for s, e in parts:
        ds = sum(len(t) - (ee - es) for es, ee, t in edits if ee <= s)
        de = sum(len(t) - (ee - es) for es, ee, t in edits if ee <= e)
        out.append((s + ds, e + de))
    return out


# -- OCaml ---------------------------------------------------------------------------
_OC_PATHS = frozenset({"value_path", "constructor_path", "type_constructor_path", "module_path", "field_path",
                       "class_path", "extended_module_path"})
_OC_LEAVES = frozenset({"value_name", "constructor_name", "type_constructor", "module_name", "field_name"})
_OC_DEFS = frozenset({"let_binding", "value_specification", "external", "type_binding", "constructor_declaration",
                      "module_binding", "field_declaration", "module_type_definition"})


def _oc_uses(tree, data):
    """``(leaf, qualifier)`` for each name in a path; ``qualifier`` is the module leaf before it, or None."""
    for n in kit.walk(tree.root_node):
        if n.type not in _OC_LEAVES or n.parent is None:
            continue
        p = n.parent
        if p.type in _OC_DEFS:
            continue
        if p.type not in _OC_PATHS:
            if n.type == "module_name" and p.type in ("open_module", "include_module", "local_open_expression",
                                                      "let_open_expression", "package_expression"):
                yield n, None
            continue
        q = n.prev_named_sibling
        while q is not None and q.type in _OC_PATHS and q.named_children:
            q = q.named_children[-1]
        yield n, (q if q is not None and q.type == "module_name" else None)


class OCaml(Unit):
    name = "OCaml"
    suffixes = (".ml", ".mli")
    code = (".ml", ".mli")

    def module_of(self, path):
        s = path.name.split(".")[0]
        return s[:1].upper() + s[1:]

    def decls(self, tree, data, path):
        out = []
        kinds = {"value_definition": "def", "type_definition": "type", "module_definition": "module",
                 "exception_definition": "type", "external": "def", "value_specification": "val",
                 "module_type_definition": "module", "class_definition": "def"}
        for n in tree.root_node.children:
            if n.type not in kinds:
                continue
            names = []
            for c in n.named_children:
                if c.type in ("value_name", "parenthesized_operator") and n.type in ("external", "value_specification"):
                    names.append(kit.text(c, data))
                elif c.type in ("let_binding", "type_binding", "module_binding", "constructor_declaration",
                                "module_type_definition", "class_binding"):
                    first = c.named_children[0] if c.named_children else None
                    if first is not None and first.type in ("value_name", "parenthesized_operator", "type_constructor",
                                                            "module_name", "constructor_name", "module_type_name",
                                                            "class_name"):
                        names.append(kit.text(first, data))
                    if c.type == "type_binding":
                        names += [kit.text(x, data) for x in kit.walk(c) if x.type == "constructor_name"
                                  and x.parent is not None and x.parent.type == "constructor_declaration"]
                        names += [kit.text(x, data) for x in kit.walk(c) if x.type == "field_name"
                                  and x.parent is not None and x.parent.type == "field_declaration"]
                elif c.type == "type_constructor" and n.type == "type_definition":
                    names.append(kit.text(c, data))
            names = list(dict.fromkeys(x for x in names if x))
            if names:
                out.append(Decl(names[0], [kit.span(n, data)], kinds[n.type], names=tuple(names[1:]), extra={"node": n}))
        return out

    def interface(self, path: Path) -> Path:
        return path.with_suffix(".mli")

    def companions(self, old, new):
        return [(old.with_suffix(".mli"), new.with_suffix(".mli"))] if old.suffix == ".ml" else \
            [(old.with_suffix(".ml"), new.with_suffix(".ml"))]

    def opened(self, tree, data, module: str) -> bool:
        """True when the file opens ``module`` or names it with an alias."""
        return any(q is None and n.type == "module_name" and kit.text(n, data) == module and n.parent.type != "module_path"
                   for n, q in _oc_uses(tree, data)) or bool(re.search(
                       rb"\b(?:open!?|include)\s+(?:\w+\.)*" + re.escape(module.encode()) + rb"\b", _mask(tree, data)))

    def aliases(self, tree, data, module: str) -> set[str]:
        return {m.group(1).decode() for m in re.finditer(
            rb"\bmodule\s+([A-Z]\w*)\s*=\s*(?:\w+\.)*" + re.escape(module.encode()) + rb"\b(?!\s*\.)", _mask(tree, data))}

    def mentions(self, data: bytes, module: str) -> bool:
        tree = kit.parse(Path("x.ml"), data)
        return any(kit.text(n, data) == module for n, _ in _oc_uses(tree, data) if n.type == "module_name") or \
            any(q is not None and kit.text(q, data) == module for _, q in _oc_uses(tree, data))

    def move(self, ctx: Ctx, source: Path, target: Path, name: str, ds: list[Decl]) -> None:
        if source.suffix != target.suffix:
            raise Unsupported(f"Move OCaml code from a {source.suffix} file to a {source.suffix} file.")
        if source.suffix == ".mli":
            raise Unsupported("Move the definition in the .ml file: its .mli declaration follows it.")
        data, tree = self.parse(ctx, source)
        src, dst = self.module_of(source), self.module_of(target)
        d = ds[0]
        moved = {d.name, *d.names}
        all_decls = self.decls(tree, data, source)
        mine = {x for e in all_decls if e.parts != d.parts for x in (e.name, *e.names)}
        s0, e0 = d.parts[0]
        smli = self.interface(source)
        sm = ctx.draft.get(smli) if ctx.draft.exists(smli) else None
        public = None
        if sm is not None:
            st = kit.parse(smli, sm)
            public = {x for e in self.decls(st, sm, smli) for x in (e.name, *e.names)}
        # 1. the moved code: names of the old module get its qualifier
        block_edits, need_src = [], False
        for n, q in _oc_uses(tree, data):
            if s0 <= n.start_byte < e0 and q is None and kit.text(n, data) in mine and n.type != "field_name":
                w = kit.text(n, data)
                if public is not None and w not in public:
                    ctx.conflicts.append(f"`{name}` uses `{w}`, which {smli.name} does not export.")
                block_edits.append((n.start_byte, n.start_byte, f"{src}.".encode()))
                need_src = True
            elif s0 <= n.start_byte < e0 and q is not None and kit.text(q, data) == dst:
                block_edits.append((q.start_byte, n.start_byte, b""))
        tmp = replace_ranges(data, block_edits)
        block = _block(tmp, _shifted(d.parts, block_edits))
        # 2. the old module: its uses of the moved names get the new qualifier
        rest = _cut(data, d.parts)
        rt = kit.parse(source, rest)
        rest_edits = [(n.start_byte, n.start_byte, f"{dst}.".encode()) for n, q in _oc_uses(rt, rest)
                      if q is None and kit.text(n, rest) in moved and n.type != "field_name"]
        need_dst = bool(rest_edits)
        rest = replace_ranges(rest, rest_edits)
        texists = ctx.draft.exists(target)
        tdata = ctx.draft.get(target) if texists else b""
        if texists and any(x in moved for e in self.decls(kit.parse(target, tdata), tdata, target) for x in (e.name, *e.names)):
            ctx.conflicts.append(f"{ctx.rel(target)} already defines `{name}`.")
        t_edits = []
        if texists:
            tt = kit.parse(target, tdata)
            t_edits = [(q.start_byte, n.start_byte, b"") for n, q in _oc_uses(tt, tdata)
                       if q is not None and kit.text(q, tdata) == src and kit.text(n, tdata) in moved]
        if (need_src or (texists and self.mentions(replace_ranges(tdata, t_edits), src))) and \
                (need_dst or self.mentions(rest, dst)):
            ctx.conflicts.append(f"{src} and {dst} would use each other: OCaml modules cannot form a cycle.")
        ctx.draft.put(source, rest)
        # 3. the new module: the code goes before its first use
        if texists:
            first = min((n.start_byte for n, q in _oc_uses(tt, tdata) if kit.text(n, tdata) in moved), default=None)
            at = None
            if first is not None:
                top = next((c for c in tt.root_node.children if c.start_byte <= first < c.end_byte), None)
                at = kit.leading(top, tdata) if top is not None else None
            new_t = replace_ranges(tdata, t_edits)
            if at is not None:
                at += sum(len(t) - (e - s) for s, e, t in t_edits if e <= at)
            ctx.draft.put(target, kit.append_block(new_t, block, at))
        else:
            ctx.draft.put(target, block.encode() + b"\n")
            self.dune_add(ctx, source, target)
        # 4. the interface files
        self.move_specs(ctx, source, target, name, moved, texists, need_dst)
        # 5. the other files: qualified uses, and bare uses in files that open the old module
        for f in ctx.files:
            if f in (source, target) or f.suffix not in self.code or f == smli or f == self.interface(target):
                continue
            fd, ft = self.parse(ctx, f)
            names_of = {src} | self.aliases(ft, fd, src)
            opened = any(self.opened(ft, fd, m) for m in names_of)
            own = {x for e in self.decls(ft, fd, f) for x in (e.name, *e.names)}
            edits = []
            for n, q in _oc_uses(ft, fd):
                w = kit.text(n, fd)
                if w not in moved:
                    continue
                if q is not None and kit.text(q, fd) in names_of:
                    edits.append((q.start_byte, q.end_byte, dst.encode()))
                elif q is None and opened and w not in own and n.type != "field_name":
                    edits.append((n.start_byte, n.start_byte, f"{dst}.".encode()))
            _apply(ctx, f, edits)

    def move_specs(self, ctx, source, target, name, moved, texists, need_dst):
        smli, tmli = self.interface(source), self.interface(target)
        if not ctx.draft.exists(smli):
            return
        sm = ctx.draft.get(smli)
        st = kit.parse(smli, sm)
        specs = [e for e in self.decls(st, sm, smli) if {e.name, *e.names} & moved]
        if not specs:
            if need_dst and ctx.draft.exists(tmli):
                ctx.conflicts.append(f"Add a declaration of `{name}` to {ctx.rel(tmli)}: {ctx.rel(source)} uses it.")
            return
        parts = [p for e in specs for p in e.parts]
        block = _block(sm, parts)
        ctx.draft.put(smli, _cut(sm, parts))
        if ctx.draft.exists(tmli):
            ctx.draft.put(tmli, kit.append_block(ctx.draft.get(tmli), block))
        elif not texists:
            ctx.draft.put(tmli, block.encode() + b"\n")

    def dune(self, folder: Path) -> Path | None:
        p = folder / "dune"
        return p if p.is_file() else None

    def dune_add(self, ctx, source, target):
        """List the new module next to the old one in an explicit ``(modules ...)`` field."""
        if source.parent != target.parent:
            if self.dune(source.parent) or self.dune(target.parent):
                ctx.notes.append(f"{ctx.rel(target)} is in another dune folder: check that its library sees {self.module_of(source)}.")
            return
        dn = self.dune(source.parent)
        if dn is None:
            return
        text = ctx.draft.get(dn).decode()
        old = source.name.split(".")[0]
        new = target.name.split(".")[0]
        m = re.search(r"\(modules\b([^()]*)\)", text)
        if m and re.search(rf"(?<![\w-]){re.escape(old)}(?![\w-])", m.group(1), re.I) and \
                not re.search(rf"(?<![\w-]){re.escape(new)}(?![\w-])", m.group(1), re.I):
            body = m.group(1).rstrip()
            text = text[: m.start(1) + len(body)] + " " + new + text[m.start(1) + len(body):]
            ctx.draft.put(dn, text.encode())
            ctx.notes.append(f"Updated {ctx.rel(dn)}.")

    def move_file(self, ctx: Ctx, old: Path, new: Path) -> None:
        om, nm = self.module_of(old), self.module_of(new)
        if old.parent != new.parent and (self.dune(old.parent) or self.dune(new.parent)):
            ctx.notes.append(f"{ctx.rel(new)} is in another dune folder: check its library and qualifiers.")
        if om == nm:
            return
        if old.suffix == ".ml":
            ctx.renames.append((om, nm))
        clash = [f for f in ctx.files if f.suffix == old.suffix and self.module_of(f) == nm and f != old]
        if clash:
            ctx.conflicts.append(f"Module {nm} exists already: {ctx.rel(clash[0])}.")
        for f in ctx.files:
            if f.suffix not in self.code:
                continue
            fd, ft = self.parse(ctx, f)
            edits = [(n.start_byte, n.end_byte, nm.encode()) for n, q in _oc_uses(ft, fd)
                     if n.type == "module_name" and kit.text(n, fd) == om and (q is None or True)]
            _apply(ctx, f, edits)
        dn = self.dune(old.parent)
        if dn is not None and old.suffix == ".ml":
            text = ctx.draft.get(dn).decode()
            o, n = old.name.split(".")[0], new.name.split(".")[0]
            word = rf"(?<![\w-]){re.escape(o)}(?![\w-])"

            def fix(m):
                return re.sub(word, n, m.group(), flags=re.I)

            new_text = re.sub(r"\((?:modules|modules_without_implementation|private_modules|names?)\b[^()]*\)", fix, text)
            if new_text != text:
                ctx.draft.put(dn, new_text.encode())
                ctx.notes.append(f"Updated {ctx.rel(dn)}.")


# -- Pascal --------------------------------------------------------------------------
_PAS_USES = re.compile(rb"(?is)\b(uses|contains)\b(.*?);")
_PAS_ITEM = re.compile(rb"(?is)\s*([\w.]+)(?:\s+in\s+'([^']*)')?\s*")
_PAS_HEAD = re.compile(rb"(?is)^\s*(?:class\s+)?(?:procedure|function|constructor|destructor|operator)\s+([\w.]+)")
_PAS_INC = re.compile(rb"(?i)\{\$(?:I|INCLUDE)\s+([^}\s]+)\s*\}")
_PAS_GROUPS = {"declTypes": "type", "declConsts": "const", "declVars": "var"}


@dataclass
class _Uses:
    start: int  # the bytes after ``uses``
    end: int  # the ``;``
    section: str  # interface | implementation | program
    items: list  # [(name, path, start, end)]


class Pascal(Unit):
    name = "Pascal"
    suffixes = (".pas", ".pp", ".dpr", ".dpk", ".lpr", ".inc", ".lpi", ".lpk", ".dproj")
    code = (".pas", ".pp", ".dpr", ".dpk", ".lpr")
    moved_files = (".inc",)
    fold = True

    def masked(self, tree, data: bytes) -> bytes:
        """``data`` with comments and strings blanked, also in code that the grammar cannot read."""
        return re.sub(rb"'(?:[^'\n]|'')*'", lambda m: b" " * len(m.group()), _mask(tree, data))

    def sections(self, tree):
        unit = next((c for c in tree.root_node.children if c.type in ("unit", "program", "library", "package")), None)
        out = {}
        if unit is not None:
            for c in unit.children:
                if c.type in ("interface", "implementation", "initialization", "finalization"):
                    out[c.type] = c
                elif c.type == "kEnd":
                    out["end"] = c
        return unit, out

    def section_at(self, tree, i: int) -> str:
        _, secs = self.sections(tree)
        for k in ("interface", "implementation"):
            if k in secs and secs[k].start_byte <= i < secs[k].end_byte:
                return k
        return "program"

    def unit_name(self, tree, data, path) -> str:
        unit, _ = self.sections(tree)
        n = unit and next((c for c in unit.children if c.type == "moduleName"), None)
        return kit.text(n, data) if n is not None else path.stem

    def decls(self, tree, data, path):
        unit, secs = self.sections(tree)
        found: dict[str, Decl] = {}
        holders = [secs[k] for k in ("interface", "implementation") if k in secs]
        if unit is not None and not holders:
            holders = [unit]

        def add(name, node, sec, group=None):
            d = found.setdefault(name.lower(), Decl(name, [], "def", False, extra={"pieces": []}))
            if group is not None and len([c for c in group.named_children if c.type.startswith("decl")]) == 1:
                node, group = group, None
            s, e = kit.span(node, data)
            d.parts.append((s, e))
            d.extra["pieces"].append((sec, _PAS_GROUPS.get(group.type) if group is not None else None, s, e, node))
            if sec == "interface":
                d.public = True

        for h in holders:
            sec = h.type if h.type in ("interface", "implementation") else "program"
            for c in h.named_children:
                if c.type in ("declProc", "defProc"):
                    m = _PAS_HEAD.match(data[c.start_byte:c.end_byte])
                    if m:
                        name = m.group(1).decode()
                        owner = name.split(".")[0]
                        if "." in name and owner.lower() in found:
                            add(owner, c, sec)
                        elif "." not in name:
                            add(name, c, sec)
                elif c.type in _PAS_GROUPS:
                    for item in c.named_children:
                        if item.type.startswith("decl"):
                            ident = next((x for x in item.named_children if x.type == "identifier"), None)
                            if ident is not None:
                                add(kit.text(ident, data), item, sec, c)
        for d in found.values():
            d.kind = "proc" if any(p[4].type in ("declProc", "defProc") for p in d.extra["pieces"]) else "data"
        return list(found.values())

    def uses(self, tree, data: bytes) -> list[_Uses]:
        masked = _mask(tree, data, strings=False)
        out = []
        for m in _PAS_USES.finditer(masked):
            items, pos = [], m.start(2)
            for chunk in re.split(rb",", data[m.start(2):m.end(2)]):
                im = _PAS_ITEM.fullmatch(chunk)
                if im:
                    items.append((im.group(1).decode(), im.group(2) and im.group(2).decode(),
                                  pos + im.start(1), pos + (im.end(2) + 1 if im.group(2) else im.end(1))))
                pos += len(chunk) + 1
            out.append(_Uses(m.start(2), m.end(2), self.section_at(tree, m.start()), items))
        return out

    def used_words(self, tree, data, start=0, end=None) -> set[str]:
        masked = self.masked(tree, data)
        for u in self.uses(tree, data):
            masked = masked[:u.start] + b" " * (u.end - u.start) + masked[u.end:]
        end = len(masked) if end is None else end
        return {w.lower() for w in re.findall(rb"(?<![.\w])[A-Za-z_]\w*", masked[start:end]) for w in [w.decode()]}

    def add_use(self, data: bytes, tree, unit: str, section: str, path_for=None, after: str | None = None) -> bytes:
        """``data`` with ``unit`` in the ``uses`` list of ``section``; a new list when there is none."""
        lists = [u for u in self.uses(tree, data) if u.section == section]
        if any(self.same(n, unit) for u in self.uses(tree, data) for n, *_ in u.items if u.section in (section, "interface")):
            return data
        entry = unit if path_for is None else f"{unit} in '{path_for}'"
        if lists:
            u = lists[0]
            at = next((e for n, _, s, e in u.items if after and self.same(n, after)), None)
            if at is None:
                at = u.items[-1][3] if u.items else u.start
            sep = ", "
            if u.items and b"\n" in data[u.start:u.end]:
                ind = kit.indent_of(data, u.items[-1][2])
                sep = ",\n" + ind
            return data[:at] + (sep + entry).encode() + data[at:]
        _, secs = self.sections(tree)
        node = secs.get(section)
        if node is None and section == "program":
            unit_node, _ = self.sections(tree)
            if unit_node is None:
                return data
            head = next((c for c in unit_node.children if c.type == ";"), None)
            at = kit.line_end(data, head.end_byte - 1) if head is not None else 0
        elif node is None:
            return data
        else:
            at = kit.line_end(data, node.children[0].end_byte - 1)
        return kit.insert_lines(data, at, [f"uses {entry};"], blank_before=True)

    def put(self, data: bytes, tree, section: str, block: str) -> bytes:
        _, secs = self.sections(tree)
        node = secs.get(section)
        if node is None:
            raise Unsupported(f"Move Pascal code to a unit with an {section} section.")
        at = kit.line_end(data, node.end_byte - 1)
        return kit.insert_lines(data, at, [""] + block.split("\n"))

    def piece_text(self, data, pieces, section) -> str:
        out, last_kw = [], None
        for sec, kw, s, e, node in pieces:
            if sec != section:
                continue
            text = kit.piece(data, s, e)
            if kw and kw != last_kw:
                text = kw + "\n" + text
            out.append(text)
            last_kw = kw
        return "\n\n".join(out)

    def move(self, ctx, source, target, name, ds):
        if target.suffix.lower() not in (".pas", ".pp"):
            raise Unsupported(f"Move Pascal code to a .pas or .pp unit, not {ctx.rel(target)}.")
        data, tree = self.parse(ctx, source)
        src = self.unit_name(tree, data, source)
        dst = target.stem
        d = ds[0]
        pieces = d.extra["pieces"]
        others = [e for e in self.decls(tree, data, source) if not self.same(e.name, d.name)]
        mine = {e.name.lower(): e for e in others}
        # the parts of the moved code: a public declaration and a body
        intf = self.piece_text(data, pieces, "interface")
        impl = self.piece_text(data, pieces, "implementation") or self.piece_text(data, pieces, "program")
        rest = _cut(data, d.parts)
        rt = kit.parse(source, rest)
        rest_words = self.used_words(rt, rest)
        users = self.users(ctx, source, target, src, d.name)
        if not intf and d.kind == "proc" and (d.name.lower() in rest_words or users or not d.public):
            head = next((p for p in pieces if p[4].type == "defProc"), None)
            if head is not None:
                dp = next(c for c in head[4].named_children if c.type == "declProc")
                intf = kit.text(dp, data)
        elif not intf and d.kind == "data" and (d.name.lower() in rest_words or users):
            intf, impl = impl, ""
        bt = kit.parse(target.with_suffix(".pas"), (intf + "\n" + impl).encode())
        need = {w for w in self.used_words(bt, (intf + "\n" + impl).encode())} & set(mine)
        for w in sorted(need):
            if not mine[w].public and self.section_at(tree, mine[w].parts[0][0]) != "program":
                ctx.conflicts.append(f"`{name}` uses `{mine[w].name}`, which the implementation of {src} hides.")
            elif self.section_at(tree, mine[w].parts[0][0]) == "program":
                ctx.conflicts.append(f"`{name}` uses `{mine[w].name}` of the program {src}: a unit cannot use a program.")
        need_intf = bool(need & ({w for w in self.used_words(kit.parse(target.with_suffix(".pas"), intf.encode()), intf.encode())}))
        # the source: uses of the moved name need the new unit
        rest_intf = set()
        _, rsecs = self.sections(rt)
        if "interface" in rsecs:
            rest_intf = self.used_words(rt, rest, rsecs["interface"].start_byte, rsecs["interface"].end_byte)
        if d.name.lower() in rest_words:
            sec = "interface" if d.name.lower() in rest_intf else ("implementation" if "implementation" in rsecs else "program")
            rest = self.add_use(rest, rt, dst, sec, self.in_path(source, target, rt, rest))
            if sec == "interface" and need_intf:
                ctx.conflicts.append(f"The interfaces of {src} and {dst} would use each other.")
        ctx.draft.put(source, rest)
        # the target
        copied = [(u.section, n, p) for u in self.uses(tree, data) for n, p, *_ in u.items
                  if not self.same(n, dst) and not p]
        texists = ctx.draft.exists(target)
        if texists:
            tdata, tt = self.parse(ctx, target)
            if any(self.same(e.name, d.name) for e in self.decls(tt, tdata, target)):
                ctx.conflicts.append(f"{ctx.rel(target)} already declares `{d.name}`.")
                return
            out = tdata
            for sec, text in (("implementation", impl), ("interface", intf)):
                if text:
                    out = self.put(out, kit.parse(target, out), sec, text)
        else:
            lines = [kit.line_start(data, c.start_byte) for c in (self.sections(tree)[0] or tree.root_node).children
                     if c.type == "pp" and re.match(r"(?i)\{\$(?:mode|h\+|modeswitch|ifdef fpc)", kit.text(c, data))]
            dirs = "\n".join(kit.piece(data, i, kit.line_end(data, i)) for i in dict.fromkeys(lines))
            out = (f"unit {dst};\n\n" + (dirs + "\n\n" if dirs else "") + "interface\n\n" + (intf + "\n\n" if intf else "")
                   + "implementation\n\n" + (impl + "\n\n" if impl else "") + "end.\n").encode()
        if need:
            out = self.add_use(out, kit.parse(target, out), src, "interface" if need_intf else "implementation")
        sec_of = {"program": "implementation"}
        for sec, n, _ in copied:
            if self.same(n, src):
                continue
            words = self.used_words(bt, (intf + "\n" + impl).encode())
            if words:
                out = self.add_use(out, kit.parse(target, out), n, sec_of.get(sec, sec))
        if copied:
            ctx.notes.append(f"{ctx.rel(target)} got the uses list of {ctx.rel(source)}: remove the units that it does not need.")
        ctx.draft.put(target, out)
        # the other files
        for f in ctx.files:
            if f in (source, target) or f.suffix.lower() not in self.code:
                continue
            fd, ft = self.parse(ctx, f)
            fm = self.masked(ft, fd)
            edits = self.requalify(fd, fm, src, dst, d.name)
            fd2 = replace_ranges(fd, edits)
            ft2 = kit.parse(f, fd2)
            lists = [u for u in self.uses(ft2, fd2) if any(self.same(n, src) for n, *_ in u.items)]
            in_style = any(p for u in self.uses(ft2, fd2) for _, p, *_ in u.items)
            if lists and (edits or d.name.lower() in self.used_words(ft2, fd2) or (in_style and not texists)):
                u = lists[0]
                fd2 = self.add_use(fd2, ft2, dst, u.section, self.in_path(f, target, ft2, fd2), after=src)
            if fd2 != fd:
                ctx.draft.put(f, fd2)

    def users(self, ctx, source, target, src, name) -> bool:
        for f in ctx.files:
            if f in (source, target) or f.suffix.lower() not in self.code:
                continue
            fd, ft = self.parse(ctx, f)
            if any(self.same(n, src) for u in self.uses(ft, fd) for n, *_ in u.items) and \
                    name.lower() in self.used_words(ft, fd):
                return True
        return False

    def in_path(self, f: Path, unit: Path, tree, data) -> str | None:
        """The ``in 'path'`` of ``unit`` in the style of the uses lists of ``f``, or None."""
        paths = [p for u in self.uses(tree, data) for _, p, *_ in u.items if p]
        if not paths:
            return None
        rel = kit.relative_path(f.parent, unit)
        return rel.replace("/", "\\") if "\\" in paths[0] else rel

    def move_file(self, ctx, old, new):
        on, nn = old.stem, new.stem
        renamed = old.suffix.lower() in self.code and not self.same(on, nn)
        if renamed:
            ctx.renames.append((on, nn))
            clash = [f for f in ctx.files if f.suffix.lower() in self.code and self.same(f.stem, nn) and f != old]
            if clash:
                ctx.conflicts.append(f"Unit {nn} exists already: {ctx.rel(clash[0])}.")
        for f in ctx.files:
            fd = ctx.draft.get(f)
            if not fd:
                continue
            at = new if f == old else f
            edits = []
            if f.suffix.lower() in (".lpi", ".lpk", ".dproj"):
                if renamed:
                    edits += _edit_all(fd, fd, rb'(?i)<UnitName\s+Value="(' + re.escape(on.encode()) + rb')"', lambda m: nn)
                _apply(ctx, f, edits)
                continue
            ft = kit.parse(f, fd)
            fm = self.masked(ft, fd)
            if f == old and renamed:
                unit, _ = self.sections(ft)
                n = unit and next((c for c in unit.children if c.type == "moduleName"), None)
                if n is not None:
                    edits.append((n.start_byte, n.end_byte, nn.encode()))
            if renamed and f.suffix.lower() in self.code:
                edits += self.requalify(fd, fm, on, nn)
            for u in self.uses(ft, fd) if f.suffix.lower() in self.code else []:
                for n, p, s, e in u.items:
                    target = (f.parent / p.replace("\\", "/")).resolve() if p else None
                    if not (self.same(n, on) or target == old or (f == old and p)):
                        continue
                    name = nn if self.same(n, on) and renamed and (target in (None, old)) else n
                    path = p
                    if p and (target == old or f == old):
                        path = kit.relative_path(at.parent, new if target == old else target)
                        if "\\" in p:
                            path = path.replace("/", "\\")
                    text = name + (f" in '{path}'" if p else "")
                    if text != fd[s:e].decode():
                        edits.append((s, e, text.encode()))
            for m in _PAS_INC.finditer(fd):
                if fm[m.start():m.start() + 1] == b" ":
                    continue
                raw = m.group(1).decode().strip("'")
                hit = (f.parent / raw.replace("\\", "/")).resolve()
                if hit == old or (f == old and hit.is_file()):
                    rel = kit.relative_path(at.parent, new if hit == old else hit)
                    if "\\" in raw:
                        rel = rel.replace("/", "\\")
                    if rel != raw:
                        edits.append((m.start(1), m.end(1), rel.encode()))
            _apply(ctx, f, [e for e in dict.fromkeys(edits)])

    def companions(self, old, new):
        return [(old.with_suffix(s), new.with_suffix(s)) for s in (".lfm", ".dfm", ".fmx", ".xfm")]


# -- Apex ----------------------------------------------------------------------------
_APEX_MEMBERS = frozenset({"method_declaration", "field_declaration", "class_declaration", "enum_declaration",
                           "interface_declaration", "constructor_declaration"})
_APEX_WEB = (".js", ".ts", ".html", ".page", ".component", ".cmp", ".app", ".evt", ".flow-meta.xml")


class Apex(Unit):
    name = "Apex"
    suffixes = (".cls", ".trigger")
    code = (".cls", ".trigger")
    fold = True

    def top(self, tree):
        return next((c for c in tree.root_node.named_children if c.type in ("class_declaration", "interface_declaration",
                                                                            "enum_declaration", "trigger_declaration")), None)

    def body(self, tree):
        t = self.top(tree)
        return t and next((c for c in t.named_children if c.type.endswith("_body")), None)

    def member_name(self, node, data) -> str | None:
        if node.type == "field_declaration":
            v = next((c for c in node.named_children if c.type == "variable_declarator"), None)
            n = v and next((c for c in v.named_children if c.type == "identifier"), None)
        else:
            n = next((c for c in node.named_children if c.type == "identifier"), None)
        return kit.text(n, data) if n is not None else None

    def modifiers(self, node, data) -> str:
        m = next((c for c in node.named_children if c.type == "modifiers"), None)
        return kit.text(m, data).lower() if m is not None else ""

    def decls(self, tree, data, path):
        out = {}
        b = self.body(tree)
        for c in b.named_children if b is not None else ():
            if c.type not in _APEX_MEMBERS or c.type == "constructor_declaration":
                continue
            name = self.member_name(c, data)
            if not name:
                continue
            mods = self.modifiers(c, data)
            static = "static" in mods or c.type in ("class_declaration", "enum_declaration", "interface_declaration")
            d = out.setdefault(name.lower(), Decl(name, [], "static" if static else "instance",
                                                  not re.search(r"\bprivate\b", mods) and bool(mods)))
            d.parts.append(kit.span(c, data))
        return list(out.values())

    def class_name(self, tree, data, path) -> str:
        t = self.top(tree)
        n = t and next((c for c in t.named_children if c.type == "identifier"), None)
        return kit.text(n, data) if n is not None else path.stem

    def words(self, tree, data, keys: set[str]):
        """Identifier leaves whose name is in ``keys``: ``(node, after_dot, qualifier)``."""
        for n in kit.walk(tree.root_node):
            if n.child_count == 0 and n.type in ("identifier", "type_identifier") and kit.text(n, data).lower() in keys:
                p = n.prev_sibling
                q = p.prev_sibling if p is not None and p.type == "." else None
                yield n, p is not None and p.type == ".", q

    def companions(self, old, new):
        return [(old.with_name(old.name + "-meta.xml"), new.with_name(new.name + "-meta.xml"))]

    def web_files(self, ctx):
        from graphify.moves import _SKIP_DIRS
        import os

        out = []
        for d, dirs, names in os.walk(ctx.root):
            dirs[:] = [x for x in dirs if x not in _SKIP_DIRS and not x.startswith(".")]
            out += [Path(d) / n for n in names if n.endswith(_APEX_WEB)]
        return out

    def web_edits(self, ctx, old_cls, new_cls, member=None, new_member=None):
        m = re.escape(member) if member else r"\w+"
        pats = [rb"@salesforce/apex/(?:\w+\.)?(" + re.escape(old_cls).encode() + rb")\." + m.encode() + rb"\b"]
        if member is None:
            pats.append(rb'(?i)\b(?:controller|extensions)\s*=\s*"[^"]*?\b(' + re.escape(old_cls).encode() + rb')\b')
        for f in self.web_files(ctx):
            fd = ctx.draft.get(f)
            edits = []
            for p in pats:
                edits += _edit_all(fd, fd, p, lambda mm: new_cls, re.I)
            if edits:
                _apply(ctx, f, edits)
                ctx.notes.append(f"Updated {ctx.rel(f)}.")

    def move(self, ctx, source, target, name, ds):
        if source.suffix.lower() != ".cls" or target.suffix.lower() != ".cls":
            raise Unsupported("Move Apex members from a .cls class to a .cls class.")
        data, tree = self.parse(ctx, source)
        src, dst = self.class_name(tree, data, source), target.stem
        d = ds[0]
        if d.kind != "static":
            raise Unsupported(f"`{name}` is an instance member of {src}: only static members and inner types can move.")
        others = {e.name.lower(): e for e in self.decls(tree, data, source) if not self.same(e.name, d.name)}
        # the moved code: names of the old class get its qualifier
        edits = []
        for s, e in d.parts:
            for n, dot, _ in self.words(tree, data, set(others)):
                if s <= n.start_byte < e and not dot:
                    o = others[kit.text(n, data).lower()]
                    if o.kind != "static":
                        ctx.conflicts.append(f"`{name}` uses the instance member `{o.name}` of {src}.")
                    elif not o.public:
                        ctx.conflicts.append(f"`{name}` uses `{o.name}`, which is private in {src}.")
                    edits.append((n.start_byte, n.start_byte, f"{src}.".encode()))
                elif s <= n.start_byte < e and dot:
                    pass
        block = _block(replace_ranges(data, edits), _shifted(d.parts, edits))
        rest = _cut(data, d.parts)
        rt = kit.parse(source, rest)
        rest_edits = [(n.start_byte, n.start_byte, f"{dst}.".encode()) for n, dot, _ in self.words(rt, rest, {d.name.lower()})
                      if not dot]
        if (rest_edits or self.used_in_others(ctx, source, src, d.name)) and not d.public:
            block = re.sub(r"(?i)\bprivate\b", "public", block, count=1)
            if not re.search(r"(?i)\b(public|global)\b", block.split("\n")[0]):
                block = re.sub(r"^(\s*)", r"\1public ", block, count=1)
            ctx.notes.append(f"`{name}` is public in {dst} now: {src} uses it.")
        rest = re.sub(rb"(\{[ \t]*\n)(?:[ \t]*\n)+", rb"\1", replace_ranges(rest, rest_edits))
        ctx.draft.put(source, rest)
        meta = source.with_name(source.name + "-meta.xml")
        if ctx.draft.exists(target):
            tdata, tt = self.parse(ctx, target)
            if any(self.same(e.name, d.name) for e in self.decls(tt, tdata, target)):
                ctx.conflicts.append(f"{dst} already has `{d.name}`.")
            t_edits = [(q.start_byte, n.start_byte, b"") for n, dot, q in self.words(tt, tdata, {d.name.lower()})
                       if dot and q is not None and self.same(kit.text(q, tdata), src)]
            tdata = replace_ranges(tdata, t_edits)
            tt = kit.parse(target, tdata)
            b = self.body(tt)
            at = kit.line_start(tdata, b.end_byte - 1)
            out = kit.insert_lines(tdata, at, ([""] if b.named_children else []) + block.split("\n"))
        else:
            out = (f"public class {dst} {{\n" + block + "\n}\n").encode()
            if ctx.draft.exists(meta):
                ctx.draft.put(target.with_name(target.name + "-meta.xml"), ctx.draft.get(meta))
        ctx.draft.put(target, out)
        for f in ctx.files:
            if f in (source, target):
                continue
            fd, ft = self.parse(ctx, f)
            fe = [(q.start_byte, q.end_byte, dst.encode()) for n, dot, q in self.words(ft, fd, {d.name.lower()})
                  if dot and q is not None and self.same(kit.text(q, fd), src)]
            _apply(ctx, f, fe)
        self.web_edits(ctx, src, dst, d.name)

    def used_in_others(self, ctx, source, src, name) -> bool:
        pat = self.qualified(src, name)
        return any(re.search(pat, ctx.draft.get(f), re.I) for f in ctx.files if f != source)

    def move_file(self, ctx, old, new):
        if old.name.endswith("-meta.xml"):
            return
        on, nn = old.stem, new.stem
        if self.same(on, nn):
            return
        ctx.renames.append((on, nn))
        clash = [f for f in ctx.files if f.suffix.lower() == old.suffix.lower() and self.same(f.stem, nn) and f != old]
        if clash:
            ctx.conflicts.append(f"Class {nn} exists already: {ctx.rel(clash[0])}.")
        if old.suffix.lower() == ".trigger":
            fd, ft = self.parse(ctx, old)
            t = self.top(ft)
            n = t and next((c for c in t.named_children if c.type == "identifier"), None)
            if n is not None:
                _apply(ctx, old, [(n.start_byte, n.end_byte, nn.encode())])
            return
        for f in ctx.files:
            fd, ft = self.parse(ctx, f)
            _apply(ctx, f, [(n.start_byte, n.end_byte, nn.encode()) for n, dot, _ in self.words(ft, fd, {on.lower()})
                            if not dot])
        self.web_edits(ctx, on, nn)


# -- plans ---------------------------------------------------------------------------
LANGS: dict[str, Unit] = {}


def register(*langs: Unit) -> None:
    for lang in langs:
        for s in lang.code + lang.moved_files:
            LANGS[s] = lang


register(Erlang(), OCaml(), Pascal(), Apex())


def lang_of(path: Path) -> Unit:
    lang = LANGS.get(path.suffix.lower())
    if lang is None:
        raise Unsupported(f"No module moves for {path.suffix} files.")
    return lang


def _plan(ctx: Ctx, subject: str, source: Path, target: Path) -> MovePlan:
    return MovePlan(subject, str(source), str(target), ctx.draft.final(), tuple(dict.fromkeys(ctx.conflicts)),
                    tuple(dict.fromkeys(ctx.notes)), (), tuple(dict.fromkeys(ctx.renames)))


def plan_move(graph: dict, node: dict, source: Path, to: str, root: Path) -> MovePlan:
    lang = lang_of(source)
    name = lang.node_name(node)
    target = resolve_path(root, to)
    if target == source:
        raise Unsupported(f"`{name}` is already in {kit.rel(root, source)}.")
    if source.suffix.lower() not in lang.code:
        raise Unsupported(f"Move the file {kit.rel(root, source)} as a whole: it has no definitions to move.")
    if target.suffix.lower() not in lang.code:
        raise Unsupported(f"Move {lang.name} code to a {'/'.join(lang.code)} file, not {to}.")
    data = source.read_bytes()
    ds = [d for d in lang.decls(kit.parse(source, data), data, source)
          if any(lang.same(x, name) for x in (d.name, *d.names))]
    if not ds:
        raise Unsupported(f"`{name}` is not a top-level definition of {kit.rel(root, source)}.")
    ctx = Ctx(lang, root, lang.files_of(root, graph, source, target))
    lang.move(ctx, source, target, name, ds)
    return _plan(ctx, name, source, target)


def plan_move_file(graph: dict, old: Path, new: str, root: Path, tests: bool = True) -> MovePlan:
    from graphify.path_moves import _build_paths

    lang = lang_of(old)
    target = resolve_path(root, new)
    if target.suffix.lower() != old.suffix.lower():
        raise Unsupported(f"Keep the {old.suffix} suffix: {new}.")
    if target.exists():
        return MovePlan(kit.rel(root, old), str(old), str(target), {}, (f"{kit.rel(root, target)} already exists.",))
    ctx = Ctx(lang, root, lang.files_of(root, graph, old))
    pairs = [(old, target)] + [(o, n) for o, n in lang.companions(old, target) if o.is_file() and not n.exists()]
    if tests:
        pairs += [(t, n) for t, n, *_ in kit.test_pairs(root, old, target) if not n.exists() and t.suffix == old.suffix]
    for o, n in pairs:
        lang.move_file(ctx, o, n)
        if o != old:
            ctx.renames.append((ctx.rel(o), ctx.rel(n)))
    for o, n in pairs:
        data = ctx.draft.get(o)
        ctx.draft.remove(o)
        ctx.draft.put(n, data)
        _build_paths(ctx.draft, root, o, n, False, ctx.notes)
    return _plan(ctx, kit.rel(root, old), old, target)
