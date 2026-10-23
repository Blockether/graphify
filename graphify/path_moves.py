"""Moves for languages that load code by file path.

C, C++, Objective-C and Metal (``#include``), Ruby (``require_relative``), shell
(``source``), PowerShell (dot-sourcing), Julia (``include``), Verilog (`` `include``),
Lua (``require``), Zig (``@import``), R (``source``), SQL, DM, Common Lisp (``.asd``),
Robot Framework (``Resource``), COBOL (``COPY``), EJS, Terraform, Dart, Solidity and
Gradle use this model.

Two models cover them:

* global: a load makes every top-level definition visible. A moved definition takes
  the loads it needs, and each file that reached it through a load gets a load of
  the new file. For C and C++, the declaration in the paired header moves too.
* binding: a load binds a name (``local a = require("a")``, ``const a =
  @import("a.zig")``), and code reaches members through it. Moved members are
  reached through a binding of the new file.

A file move rewrites every load path that reaches the file, the relative loads in
the file itself, and the path in build files (CMake, Make, Meson, ``build.zig``,
file lists).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from graphify import move_kit as kit
from graphify.moves import MovePlan, project_files, replace_ranges, resolve_path
from graphify.refactor import Unsupported

__all__ = ["LANGS", "plan_move", "plan_move_file"]

BUILD_FILES = ("CMakeLists.txt", "Makefile", "makefile", "GNUmakefile", "meson.build", "build.zig", "Rakefile",
               "Gemfile", "Project.toml", "BUILD", "BUILD.bazel", "SConstruct")
BUILD_SUFFIXES = (".cmake", ".mk", ".gemspec", ".f", ".flist", ".rockspec", ".bazel", ".asd", ".toc", ".psd1", ".lpk")


@dataclass
class Decl:
    name: str
    start: int
    end: int
    kind: str = "def"  # def | proto | type | var
    public: bool = True
    scope: tuple = ()  # C++ namespaces
    table: str = ""  # Lua module table


@dataclass
class Load:
    stmt: tuple  # the whole statement line
    lit: tuple  # the bytes of the path to rewrite
    raw: str
    target: Path | None
    style: str = "rel"  # rel: from the file's folder; base: from ``base``; dotted: Lua module name
    base: Path | None = None
    binding: str | None = None
    items: tuple | None = None  # the bytes of a name list: ``show A, B``, ``{A, B}``


def _comment_at(tree, i: int) -> bool:
    n = tree.root_node.descendant_for_byte_range(i, i)
    while n is not None:
        if "comment" in n.type:
            return True
        n = n.parent
    return False


class Profile:
    name = ""
    suffixes: tuple = ()
    family: tuple = ()  # suffixes that can load each other
    self_loads = True  # each file loads what it uses; False: an entry file loads all files
    binding = False
    exts: tuple = ()  # extensions a load can leave out
    word = "file"
    reserved: frozenset = frozenset()  # names a new binding must not hide
    listed = False  # build file lists compile each file: never add a load for a moved definition
    cycles = True  # files can load each other in a cycle
    scope = "file"  # "dir": the files of a folder share their definitions without loads
    inline = False  # a moved definition leaves a load of its new file in its place
    ordered = False  # a tool runs the files in name order, like database migrations
    idents: frozenset = frozenset()  # more identifier node types
    mask: bytes | None = None  # comments and strings: then a reference is a word outside them
    ident_re = rb"[A-Za-z_][A-Za-z0-9_]*"

    def __init__(self):
        self.files: list[Path] = []
        self.root = Path(".")

    # -- definitions --------------------------------------------------------------
    def decls(self, tree, data, path) -> list[Decl]:
        return []

    def node_name(self, node: dict) -> str:
        from graphify.named_moves import _name

        return _name(node)

    def key(self, name: str) -> str:
        """The form of a name that the language compares."""
        return name

    def _blank(self, data: bytes) -> bytes:
        return re.sub(self.mask, lambda m: re.sub(rb"[^\n]", b" ", m.group()), data)

    def refs(self, tree, data, names):
        """(start, end) of each use of ``names``, outside comments and strings."""
        keys = {self.key(n) for n in names}
        if self.mask is not None:
            for m in re.finditer(self.ident_re, self._blank(data)):
                if self.key(m.group().decode()) in keys:
                    yield m.start(), m.end()
            return
        for n in kit.walk(tree.root_node):
            if n.child_count == 0 and (n.type in kit.IDENT_TYPES or n.type in self.idents) and \
                    self.key(kit.text(n, data)) in keys:
                yield n.start_byte, n.end_byte

    def used(self, tree, data) -> set[str]:
        """The keys of the names that code uses."""
        if self.mask is not None:
            return {self.key(m.group().decode()) for m in re.finditer(self.ident_re, self._blank(data))}
        out = {self.key(w) for w in kit.words(tree.root_node, data)}
        for n in kit.walk(tree.root_node):
            if n.child_count == 0 and (n.type in kit.IDENT_TYPES or n.type in self.idents):
                out.add(self.key(kit.text(n, data)))
        return out

    def make_public(self, block: str, decl: Decl) -> str:
        return block

    # -- loads --------------------------------------------------------------------
    pattern: re.Pattern = re.compile(rb"(?!)")

    def loads(self, tree, data, path) -> list[Load]:
        out = []
        for m in self.pattern.finditer(data):
            if _comment_at(tree, m.start("path")):
                continue
            raw = m.group("path").decode()
            ld = self.resolve(raw, path)
            ld.raw = raw
            ld.stmt = (kit.line_start(data, m.start()), kit.line_end(data, m.end() - 1))
            ld.lit = (m.start("path"), m.end("path"))
            ld.binding = m.groupdict().get("bind") and m.group("bind").decode()
            out.append(ld)
        return out

    def _find(self, raw, bases, style="base"):
        exts = ("",) + tuple(self.exts)
        for base in bases:
            for ext in exts:
                p = Path(os.path.normpath(base / (raw + ext)))
                if p in self._known or self.exists(p):
                    return Load((0, 0), (0, 0), raw, p.resolve(), style, base)
        return None

    def resolve(self, raw, path) -> Load:
        rel = raw.replace("\\", "/")
        hit = self._find(rel, [path.parent], "rel")
        return hit or Load((0, 0), (0, 0), raw, None)

    def render(self, ld: Load, target: Path, from_path: Path) -> str:
        base = ld.base if ld.style == "base" and ld.base is not None else from_path.parent
        rel = kit.relative_path(base, target)
        if ld.style == "base" and rel.startswith("../"):
            rel = kit.relative_path(from_path.parent, target)
        if ld.style == "dotted":
            return self.dotted(rel)
        if not Path(ld.raw).suffix and Path(rel).suffix in self.exts:
            rel = rel[: -len(Path(rel).suffix)]
        if ld.raw.replace("\\", "/").startswith("./") and not rel.startswith("../"):
            rel = "./" + rel
        if "\\" in ld.raw and "/" not in ld.raw:
            rel = rel.replace("/", "\\")
        return rel

    def dotted(self, rel):
        return rel

    def load_line(self, data: bytes, from_path: Path, target: Path, like: Load | None, binding: str | None = None,
                  names=None) -> str:
        """A load of ``target`` in the style of ``like``, a load in the same file, when there is one.

        ``names`` replaces the name list of ``like`` (``show A``, ``{A}``); None drops the list.
        """
        if like is not None:
            raw = data[like.stmt[0]:like.stmt[1]]
            spans = [(like.lit, self.render(like, target, from_path).encode())]
            if like.items is not None:
                spans.append((like.items, ", ".join(names).encode() if names else None))
            for (s, e), new in sorted(spans, key=lambda x: -x[0][0]):
                s, e = s - like.stmt[0], e - like.stmt[0]
                raw = raw[:s] + new + raw[e:] if new is not None else self.unlist(raw, s, e)
            line = raw.decode().rstrip("\n")
            if binding and like.binding:
                line = re.sub(rf"\b{re.escape(like.binding)}\b", binding, line, count=1)
            return line
        return self.default_line(from_path, target, binding)

    def default_line(self, from_path, target, binding):
        raise NotImplementedError

    def unlist(self, line: bytes, s: int, e: int) -> bytes:
        """``line`` without the name list at ``s:e``: a load of every name."""
        return line

    def put_loads(self, data: bytes, at: int, lines: list[str]) -> bytes:
        return _put_loads(data, at, lines)

    def load_point(self, data: bytes, lds: list[Load]) -> int:
        """Where a new load goes: after the last load, else after the leading comments."""
        if lds:
            return max(ld.stmt[1] for ld in lds)
        i = 0
        for line in data.splitlines(keepends=True):
            s = line.strip()
            if s.startswith((b"#!", b"//", b"#", b"--", b"%")) and not s.startswith((b"#include", b"#import", b"#define", b"#if")):
                i += len(line)
                continue
            if not s and i:
                i += len(line)
                continue
            break
        return i

    # -- new files ----------------------------------------------------------------
    def new_file(self, target: Path, loads: list[str], block: str, source: Path, source_data: bytes) -> str:
        head = "\n".join(loads)
        return (head + "\n\n" if head else "") + block.strip("\n") + "\n"

    def insert_point(self, data: bytes, path: Path) -> int | None:
        """Where moved code goes in an existing file. None: at the end."""
        return None

    def place(self, data: bytes, block: str, path: Path, at: int | None) -> bytes:
        """``data`` of an existing file with the moved ``block`` in it."""
        return kit.append_block(data, block, at)

    def tidy_rest(self, rest: bytes, path: Path) -> bytes:
        """The code that stays in a file after a move."""
        return rest

    def inline_from(self, path: Path) -> bool:
        """With ``inline``: whether a move from ``path`` leaves a load in place of the code."""
        return True

    def check_move(self, source: Path, target: Path, name: str) -> str | None:
        """Why ``name`` cannot move from ``source`` to ``target``, or None."""
        return None

    def boiler(self, text: bytes) -> bytes:
        """``text`` without the lines that every file of the language has."""
        return text

    def file_plan(self, st, old: Path, new: Path, files: list, notes: list) -> None:
        """More changes of a file move: names that follow the file name."""

    def setup(self, root, files, exists=None):
        self.root = root
        self.files = files
        self._known = set(files)
        self.exists = exists or Path.is_file
        self.bases = set()
        self.pending: tuple = ()  # files that the plan creates


_C_HEADERS = (".h", ".hpp", ".hh", ".hxx", ".cuh")
_C_SOURCES = (".c", ".cpp", ".cc", ".cxx", ".cu", ".m", ".mm", ".metal")


def _c_declarator_name(node, data):
    d = node
    while d is not None and d.type not in ("identifier", "field_identifier", "qualified_identifier", "operator_name",
                                           "destructor_name", "type_identifier"):
        nxt = d.child_by_field_name("declarator")
        if nxt is None:
            nxt = next((c for c in d.named_children if c.type in ("identifier", "qualified_identifier", "function_declarator",
                                                                  "pointer_declarator", "reference_declarator",
                                                                  "init_declarator", "array_declarator")), None)
        d = nxt
    return kit.text(d, data).split("::")[-1] if d is not None else None


class C(Profile):
    name = "c"
    suffixes = _C_HEADERS + _C_SOURCES
    family = suffixes
    word = "header"
    pattern = re.compile(rb'(?m)^[ \t]*#[ \t]*(?:include|import)[ \t]*"(?P<path>[^"\n]+)"')
    _SYSTEM = re.compile(rb'(?m)^[ \t]*#[ \t]*(?:include|import)[ \t]*<[^>\n]+>[^\n]*\n?')
    _NESTED = ("preproc_ifdef", "preproc_if", "preproc_else", "preproc_elif", "linkage_specification", "declaration_list")

    def resolve(self, raw, path):
        hit = self._find(raw, [path.parent], "rel")
        if hit:
            return hit
        bases = self.__dict__.setdefault("bases", set())
        for base in sorted(bases):
            if self.exists(base / raw):
                return Load((0, 0), (0, 0), raw, (base / raw).resolve(), "base", base)
        for f in self.files:
            s = f.as_posix()
            if s.endswith("/" + raw):
                bases.add(Path(s[: -len(raw) - 1]))
                return Load((0, 0), (0, 0), raw, f, "base", Path(s[: -len(raw) - 1]))
        return Load((0, 0), (0, 0), raw, None)

    def default_line(self, from_path, target, binding):
        word = "import" if from_path.suffix in (".m", ".mm") else "include"
        return f'#{word} "{kit.relative_path(from_path.parent, target)}"'

    def decls(self, tree, data, path):
        out = []

        def visit(parent, scope):
            for n in parent.named_children:
                if n.type in self._NESTED:
                    visit(n, scope)
                    continue
                if n.type == "namespace_definition":
                    nm = n.child_by_field_name("name")
                    body = n.child_by_field_name("body")
                    if body is not None:
                        visit(body, scope + ((kit.text(nm, data) if nm is not None else ""),))
                    continue
                inner = n
                while inner.type == "template_declaration":
                    inner = next((c for c in reversed(inner.named_children) if c.type not in ("template_parameter_list",)), inner)
                    if inner is n:
                        break
                name, kind = self._name(inner, data)
                if not name:
                    continue
                start, end = kit.span(n, data)
                static = re.search(r"\bstatic\b", kit.text(n, data).split("{")[0].split("(")[0]) is not None
                out.append(Decl(name, start, end, kind, not static, scope))

        visit(tree.root_node, ())
        return out

    def _name(self, n, data):
        t = n.type
        if t == "function_definition":
            return _c_declarator_name(n.child_by_field_name("declarator"), data), "def"
        if t == "declaration":
            d = n.child_by_field_name("declarator")
            if d is None:
                inner = next((c for c in n.named_children if c.type in ("struct_specifier", "union_specifier", "enum_specifier",
                                                                     "class_specifier")), None)
                return self._name(inner, data) if inner is not None else (None, "")
            kind = "proto" if any(c.type == "function_declarator" for c in kit.walk(d)) else "var"
            if kind == "var" and re.search(r"\bextern\b", kit.text(n, data)):
                kind = "proto"
            return _c_declarator_name(d, data), kind
        if t in ("struct_specifier", "union_specifier", "enum_specifier", "class_specifier"):
            nm = n.child_by_field_name("name")
            if nm is None or n.child_by_field_name("body") is None:
                return None, ""
            return kit.text(nm, data), "type"
        if t == "type_definition":
            d = n.child_by_field_name("declarator")
            return (_c_declarator_name(d, data) if d is not None else None), "type"
        if t in ("preproc_def", "preproc_function_def"):
            nm = n.child_by_field_name("name")
            return (kit.text(nm, data) if nm is not None else None), "type"
        if t in ("class_interface", "class_implementation", "protocol_declaration", "category_interface",
                 "category_implementation"):
            nm = next((c for c in n.named_children if c.type == "identifier"), None)
            return (kit.text(nm, data) if nm is not None else None), ("def" if "implementation" in t else "type")
        return None, ""

    def make_public(self, block, decl):
        return block

    def pair(self, path: Path) -> Path | None:
        """The header of a source file, or the source of a header, with the same stem."""
        others = _C_SOURCES if path.suffix in _C_HEADERS else _C_HEADERS
        for suf in others:
            p = path.with_suffix(suf)
            if p in self._known or p.is_file():
                return p
        for f in self.files:
            if f.stem == path.stem and f.suffix in others and f != path:
                return f
        return None

    def system_loads(self, data: bytes) -> list[str]:
        return [m.group().decode().strip() for m in self._SYSTEM.finditer(data)]

    def new_file(self, target, loads, block, source, source_data):
        head = "\n".join(loads)
        body = (head + "\n\n" if head else "") + block.strip("\n") + "\n"
        if target.suffix not in _C_HEADERS:
            return body
        if re.search(rb"(?m)^[ \t]*#[ \t]*pragma[ \t]+once", source_data):
            return "#pragma once\n\n" + body
        m = re.search(rb"(?m)^[ \t]*#[ \t]*ifndef[ \t]+(\w+)[ \t]*\n[ \t]*#[ \t]*define[ \t]+\1\b", source_data)
        guard = re.sub(r"\W", "_", target.name).upper()
        if m:
            old = m.group(1).decode()
            mine = re.sub(r"\W", "_", source.name).upper()
            guard = old.replace(mine, guard) if mine in old else guard
        else:
            return body
        return f"#ifndef {guard}\n#define {guard}\n\n{body}\n#endif\n"

    def insert_point(self, data, path):
        if path.suffix not in _C_HEADERS:
            return None
        m = None
        for m in re.finditer(rb"(?m)^[ \t]*#[ \t]*endif\b[^\n]*\n?", data):
            pass
        if m is not None and not data[m.end():].strip():
            return m.start()
        return None

    def wrap(self, block: str, scope: tuple, data: bytes) -> tuple[str, int | None]:
        """Put ``block`` in its namespaces. Return the block and an insert point when the namespace is already open."""
        if not scope:
            return block, None
        tree = kit.parse(Path("x.cpp"), data)
        node = tree.root_node
        for name in scope:
            hit = None
            for c in node.named_children:
                if c.type == "namespace_definition":
                    nm = c.child_by_field_name("name")
                    if (kit.text(nm, data) if nm is not None else "") == name and c.child_by_field_name("body") is not None:
                        hit = c.child_by_field_name("body")
            if hit is None:
                break
            node = hit
        else:
            close = node.end_byte - 1
            return block, kit.line_start(data, close)
        ns = "::".join(scope)
        return f"namespace {ns} {{\n\n{block.strip(chr(10))}\n\n}}  // namespace {ns}", None


class Ruby(Profile):
    name = "ruby"
    suffixes = (".rb", ".rake")
    family = suffixes
    exts = (".rb",)
    pattern = re.compile(rb"""(?m)^[ \t]*(?P<verb>require_relative|require|load)[ \t]*\(?[ \t]*(?P<q>['"])(?P<path>[^'"\n]+)(?P=q)""")

    def loads(self, tree, data, path):
        out = []
        for m in self.pattern.finditer(data):
            raw = m.group("path").decode()
            verb = m.group("verb")
            if verb == b"require_relative":
                ld = self._find(raw, [path.parent], "rel") or Load((0, 0), (0, 0), raw, None)
            else:
                ld = self._find(raw, [self.root / "lib", self.root, path.parent], "base") or Load((0, 0), (0, 0), raw, None)
            ld.stmt = (kit.line_start(data, m.start()), kit.line_end(data, m.end() - 1))
            ld.lit = (m.start("path"), m.end("path"))
            out.append(ld)
        return out

    def default_line(self, from_path, target, binding):
        rel = kit.relative_path(from_path.parent, target)
        return f"require_relative '{rel[:-3] if rel.endswith('.rb') else rel}'"

    def decls(self, tree, data, path):
        out = []
        for n in tree.root_node.named_children:
            name = None
            if n.type in ("class", "module"):
                nm = n.child_by_field_name("name")
                name = kit.text(nm, data).split("::")[-1] if nm is not None else None
            elif n.type in ("method", "singleton_method"):
                nm = n.child_by_field_name("name")
                name = kit.text(nm, data) if nm is not None else None
            elif n.type == "assignment":
                left = n.child_by_field_name("left")
                if left is not None and left.type == "constant":
                    name = kit.text(left, data)
            if name:
                s, e = kit.span(n, data)
                out.append(Decl(name, s, e))
        return out


_SH_PATH = re.compile(rb"[\w.\-/\\]+\.(?:sh|bash|ps1|psm1)")
_SH_VAR = re.compile(rb"\$(?:\{[^}]*\}|\((?:[^()]|\([^()]*\))*\)|\w+)")


class Shell(Profile):
    name = "shell"
    suffixes = (".sh", ".bash")
    family = suffixes
    self_loads = False
    pattern = re.compile(rb"(?m)^[ \t]*(?:source|\.)[ \t]+(?P<arg>[^\n;&|#]+)")

    def loads(self, tree, data, path):
        out = []
        for m in self.pattern.finditer(data):
            arg = m.group("arg")
            var = list(_SH_VAR.finditer(arg))
            off = var[-1].end() if var else 0
            hits = list(_SH_PATH.finditer(arg, off))
            if not hits or _comment_at(tree, m.start("arg")):
                continue
            h = hits[-1]
            raw = h.group().decode()
            prefix = arg[: h.start()]
            lead = 0
            if var and h.start() == off and raw.startswith(("/", "\\")):
                raw, lead = raw[1:], 1
            bases = [path.parent] if b"$" in prefix or raw.startswith(("./", "../")) else [path.parent, self.root]
            ld = self._find(raw.replace("\\", "/"), bases, "rel") or Load((0, 0), (0, 0), raw, None)
            if ld.target is not None and ld.base == self.root and self.root != path.parent:
                ld.style = "base"
            s = m.start("arg") + h.start() + lead
            ld.stmt = (kit.line_start(data, m.start()), kit.line_end(data, m.end() - 1))
            ld.lit = (s, s + len(raw.encode()))
            out.append(ld)
        return out

    def default_line(self, from_path, target, binding):
        return f'source "$(dirname "${{BASH_SOURCE[0]}}")/{kit.relative_path(from_path.parent, target)}"'

    def decls(self, tree, data, path):
        out = []
        for n in tree.root_node.named_children:
            if n.type == "function_definition":
                nm = n.child_by_field_name("name")
                if nm is not None:
                    s, e = kit.span(n, data)
                    out.append(Decl(kit.text(nm, data), s, e))
        return out


class PowerShell(Shell):
    name = "powershell"
    suffixes = (".ps1", ".psm1")
    family = suffixes
    pattern = re.compile(rb"(?im)^[ \t]*(?:\.|Import-Module|using[ \t]+module)[ \t]+(?P<arg>[^\n;|#]+)")
    _DECL = re.compile(rb"(?im)^[ \t]*(function|filter|class|enum)[ \t]+([\w-]+)")

    def default_line(self, from_path, target, binding):
        return f'. "$PSScriptRoot/{kit.relative_path(from_path.parent, target)}"'

    def decls(self, tree, data, path):
        out = []
        for m in self._DECL.finditer(data):
            if _comment_at(tree, m.start(1)):
                continue
            n = tree.root_node.descendant_for_byte_range(m.start(1), m.start(1))
            while n is not None and n.type not in ("function_statement", "class_statement", "enum_statement"):
                n = n.parent
            if n is None:
                continue
            s, e = kit.span(n, data)
            out.append(Decl(m.group(2).decode(), s, e))
        return out


class Julia(Profile):
    name = "julia"
    suffixes = (".jl",)
    family = suffixes
    self_loads = False
    pattern = re.compile(rb'(?m)^[ \t]*include\([ \t]*"(?P<path>[^"\n]+)"[ \t]*\)')

    def default_line(self, from_path, target, binding):
        return f'include("{kit.relative_path(from_path.parent, target)}")'

    def decls(self, tree, data, path):
        out = []
        for n in tree.root_node.named_children:
            name = None
            if n.type in ("function_definition", "macro_definition"):
                sig = next((c for c in n.named_children if c.type in ("signature", "call_expression", "identifier")), None)
                ident = next((c for c in kit.walk(sig) if c.type == "identifier"), None) if sig is not None else None
                name = kit.text(ident, data) if ident is not None else None
            elif n.type in ("struct_definition", "abstract_definition", "primitive_definition", "module_definition"):
                head = next((c for c in n.named_children if c.type in ("type_head", "identifier")), None)
                ident = next((c for c in kit.walk(head) if c.type == "identifier"), None) if head is not None else None
                name = kit.text(ident, data) if ident is not None else None
            elif n.type in ("assignment", "const_statement"):
                a = n if n.type == "assignment" else next((c for c in n.named_children if c.type == "assignment"), None)
                left = a.named_children[0] if a is not None and a.named_children else None
                if left is not None and left.type == "call_expression":
                    left = left.named_children[0]
                name = kit.text(left, data) if left is not None and left.type == "identifier" else None
            if name:
                s, e = kit.span(n, data, lambda p: p.type == "string_literal" and kit.text(p, data).startswith('"""'))
                out.append(Decl(name, s, e))
        return out


class Verilog(Profile):
    name = "verilog"
    suffixes = (".v", ".sv", ".svh", ".vh")
    family = suffixes
    self_loads = False
    listed = True
    pattern = re.compile(rb'(?m)^[ \t]*`include[ \t]+"(?P<path>[^"\n]+)"')

    resolve = C.resolve

    def default_line(self, from_path, target, binding):
        return f'`include "{kit.relative_path(from_path.parent, target)}"'

    def decls(self, tree, data, path):
        out = []
        for n in tree.root_node.named_children:
            if n.type in ("module_declaration", "interface_declaration", "package_declaration", "program_declaration",
                          "class_declaration", "checker_declaration"):
                ident = next((c for c in kit.walk(n) if c.type == "simple_identifier"), None)
                if ident is not None:
                    s, e = kit.span(n, data)
                    out.append(Decl(kit.text(ident, data), s, e))
        return out


class Lua(Profile):
    name = "lua"
    suffixes = (".lua", ".luau")
    family = suffixes
    binding = True
    exts = (".lua", ".luau", "/init.lua")
    word = "module"
    cycles = False
    reserved = frozenset({"math", "string", "table", "os", "io", "coroutine", "debug", "utf8", "package", "bit", "bit32",
                          "jit", "ffi", "vim", "require", "print", "type", "pairs", "ipairs", "next", "error", "assert",
                          "select", "load", "self", "M", "_G", "_ENV"})
    pattern = re.compile(rb"""(?m)(?:local[ \t]+(?P<bind>\w+)[ \t]*=[ \t]*)?require[ \t]*\(?[ \t]*(?P<q>['"])(?P<path>[^'"\n]+)(?P=q)[ \t]*\)?""")

    def resolve(self, raw, path):
        rel = raw.replace(".", "/")
        bases = [self.root, self.root / "lua", self.root / "src", self.root / "lib", *path.parents]
        for base in bases:
            hit = self._find(rel, [base], "dotted")
            if hit:
                return hit
        return Load((0, 0), (0, 0), raw, None)

    def dotted(self, rel):
        for ext in (".lua", ".luau"):
            if rel.endswith(ext):
                rel = rel[: -len(ext)]
        if rel.endswith("/init"):
            rel = rel[:-5]
        return rel.replace("/", ".")

    def render(self, ld, target, from_path):
        base = ld.base or self.root
        return self.dotted(kit.relative_path(base, target))

    def default_line(self, from_path, target, binding):
        base = next((ld.base for f in self.files for ld in self._loads_of(f) if ld.base is not None), self.root)
        return f'local {binding} = require("{self.dotted(kit.relative_path(base, target))}")'

    def _loads_of(self, f):
        data = f.read_bytes() if f.is_file() else b""
        return self.loads(kit.parse(f, data), data, f)

    def table(self, tree, data):
        """The module table that the file returns: ``return M``."""
        for n in reversed(tree.root_node.named_children):
            if n.type == "return_statement":
                m = re.fullmatch(r"return\s+(\w+)\s*;?", kit.text(n, data).strip())
                return m.group(1) if m else ""
        return ""

    def decls(self, tree, data, path):
        out = []
        table = self.table(tree, data)
        for n in tree.root_node.named_children:
            name, tab, public = None, "", True
            if n.type == "function_declaration":
                nm = n.child_by_field_name("name")
                if nm is None:
                    continue
                t = kit.text(nm, data)
                m = re.fullmatch(r"(\w+)[.:](\w+)", t)
                if m:
                    tab, name = m.group(1), m.group(2)
                    public = tab == table
                else:
                    name = t
                    public = not kit.text(n, data).lstrip().startswith("local")
            elif n.type == "variable_declaration" and b"require" not in data[n.start_byte:n.end_byte]:
                m = re.match(r"local\s+(\w+)\s*=", kit.text(n, data))
                if m and m.group(1) != table:
                    name, public = m.group(1), False
            elif n.type == "assignment_statement":
                m = re.match(r"(\w+)\.(\w+)\s*=", kit.text(n, data))
                if m and m.group(1) == table:
                    tab, name = m.group(1), m.group(2)
            if name:
                s, e = kit.span(n, data)
                out.append(Decl(name, s, e, "def", public, (), tab))
        return out

    def new_file(self, target, loads, block, source, source_data):
        head = "\n".join(loads)
        table = self.table(kit.parse(source, source_data), source_data) or "M"
        return (head + "\n\n" if head else "") + f"local {table} = {{}}\n\n" + block.strip("\n") + f"\n\nreturn {table}\n"

    def insert_point(self, data, path):
        tree = kit.parse(path, data)
        last = tree.root_node.named_children[-1] if tree.root_node.named_children else None
        if last is not None and last.type == "return_statement":
            return kit.line_start(data, last.start_byte)
        return None

    def member_refs(self, tree, data, binding):
        """``binding.name`` member reads: (start of binding, end of binding, name)."""
        for n in kit.walk(tree.root_node):
            if n.type in ("dot_index_expression", "method_index_expression"):
                t = n.child_by_field_name("table")
                f = n.child_by_field_name("field") or n.child_by_field_name("method")
                if t is not None and f is not None and kit.text(t, data) == binding:
                    yield t.start_byte, t.end_byte, kit.text(f, data)


class Zig(Profile):
    name = "zig"
    suffixes = (".zig",)
    family = suffixes
    binding = True
    word = "file"
    reserved = frozenset({"std", "builtin", "root"})
    pattern = re.compile(rb'(?m)(?:(?:pub[ \t]+)?const[ \t]+(?P<bind>\w+)[ \t]*=[ \t]*)?@import\([ \t]*"(?P<path>[^"\n]+\.zig)"[ \t]*\)[ \t]*;?')

    def default_line(self, from_path, target, binding):
        return f'const {binding} = @import("{kit.relative_path(from_path.parent, target)}");'

    def decls(self, tree, data, path):
        out = []
        for n in tree.root_node.named_children:
            if n.type not in ("function_declaration", "variable_declaration"):
                continue
            if n.type == "variable_declaration" and b"@import" in data[n.start_byte:n.end_byte]:
                continue
            nm = next((c for c in n.named_children if c.type == "identifier"), None)
            if nm is None:
                continue
            s, e = kit.span(n, data)
            public = kit.text(n, data).lstrip().startswith(("pub ", "pub\t"))
            out.append(Decl(kit.text(nm, data), s, e, "def", public))
        return out

    def make_public(self, block, decl):
        return re.sub(r"(?m)^(\s*)(?=(?:export\s+|extern\s+|inline\s+)?(?:fn|const|var)\b)", r"\1pub ", block, count=1)

    def load_point(self, data, lds):
        ends = [kit.line_end(data, m.end() - 1) for m in re.finditer(rb"(?m)^[ \t]*(?:pub[ \t]+)?const[ \t]+\w+[ \t]*=[ \t]*@import\([^\n]*", data)]
        return max(ends) if ends else super().load_point(data, lds)

    def member_refs(self, tree, data, binding):
        for n in kit.walk(tree.root_node):
            if n.type == "field_expression":
                obj = n.child_by_field_name("object") or (n.named_children[0] if n.named_children else None)
                mem = n.child_by_field_name("member") or (n.named_children[-1] if len(n.named_children) > 1 else None)
                if obj is not None and mem is not None and obj.type == "identifier" and kit.text(obj, data) == binding:
                    yield obj.start_byte, obj.end_byte, kit.text(mem, data)

    def bare_refs(self, tree, data, names, skip=()):
        """Identifiers that name ``names``, outside member access and the declarations in ``skip``."""
        for n in kit.tokens(tree.root_node, data, names, {"identifier"}):
            p = n.parent
            if p is not None and p.type == "field_expression" and p.named_children and p.named_children[0] is not n:
                continue
            if any(s <= n.start_byte < e for s, e in skip):
                continue
            if p is not None and p.type in ("function_declaration", "variable_declaration") and \
                    next((c for c in p.named_children if c.type == "identifier"), None) == n:
                continue
            yield n.start_byte, n.end_byte, kit.text(n, data)


def _label(node: dict) -> str:
    """The node label without a call suffix ``()`` or a kind note `` (macro)``."""
    return re.sub(r"\s*\((?:\w+)?\)$", "", str(node.get("label", "")).strip())


class R(Profile):
    name = "r"
    suffixes = (".r",)
    family = suffixes
    self_loads = False
    pattern = re.compile(rb"""(?m)^[ \t]*(?:sys\.)?source\([ \t]*(?:file[ \t]*=[ \t]*)?(?P<q>['"])(?P<path>[^'"\n]+)(?P=q)""")

    def node_name(self, node):
        return _label(node)

    def resolve(self, raw, path):
        rel = raw.replace("\\", "/")
        return self._find(rel, [path.parent], "rel") or self._find(rel, [self.root]) or Load((0, 0), (0, 0), raw, None)

    def default_line(self, from_path, target, binding):
        return f'source("{kit.relative_path(self.root, target)}")'

    def decls(self, tree, data, path):
        out = []
        for n in tree.root_node.named_children:
            if n.type != "binary_operator" or not n.named_children:
                continue
            left = n.child_by_field_name("lhs") or n.named_children[0]
            op = n.child_by_field_name("operator")
            if left.type == "identifier" and (op is None or kit.text(op, data) in ("<-", "=", "<<-")):
                s, e = kit.span(n, data)
                out.append(Decl(kit.text(left, data), s, e))
        return out


_SQL_CREATE = re.compile(
    rb"(?is)create\s+(?:or\s+replace\s+)?(?:(?:temp|temporary|unique|materialized|recursive|global|local|unlogged)\s+)*"
    rb"(table|view|function|procedure|index|type|trigger|sequence|schema|domain|extension|aggregate|policy|rule)\s+"
    rb"(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?((?:[\w$]+|\"[^\"]+\")(?:\.(?:[\w$]+|\"[^\"]+\"))*)")
_SQL_SKIP = re.compile(rb"(?s)--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\"[^\"]*\"|\$(\w*)\$.*?\$\1\$")


def _sql_statements(data: bytes):
    """(start, end) of each statement: the text up to and with its ``;``."""
    i = start = 0
    while i < len(data):
        m = _SQL_SKIP.match(data, i)
        if m:
            i = m.end()
            continue
        if data[i:i + 1] == b";":
            yield start, i + 1
            start = i + 1
        i += 1
    if data[start:].strip():
        yield start, len(data)


class SQL(Profile):
    name = "sql"
    suffixes = (".sql",)
    family = suffixes
    self_loads = False
    listed = True
    ordered = True
    mask = rb"(?s)--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'"
    pattern = re.compile(rb"""(?im)^[ \t]*(?:\\(?:ir?|include(?:_relative)?|\.)[ \t]+|source[ \t]+|\.read[ \t]+|@@?[ \t]*)"""
                         rb"""(?P<q>['"]?)(?P<path>[^'"\s;]+)(?P=q)""")

    def node_name(self, node):
        return _label(node).split(".")[-1]

    def key(self, name):
        return name[1:-1] if name.startswith('"') else name.lower()

    def resolve(self, raw, path):
        rel = raw.replace("\\", "/")
        return self._find(rel, [path.parent], "rel") or self._find(rel, [self.root]) or Load((0, 0), (0, 0), raw, None)

    def default_line(self, from_path, target, binding):
        return f"\\ir {kit.relative_path(from_path.parent, target)}"

    def decls(self, tree, data, path):
        out = []
        for a, b in _sql_statements(data):
            body = data[a:b]
            lead = re.match(rb"(?:\s+|--[^\n]*|/\*.*?\*/)*", body, re.S).end()
            m = _SQL_CREATE.match(body, lead)
            if not m:
                continue
            name = re.split(rb"\.(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", m.group(2))[-1].decode()
            s = kit.line_start(data, a + lead)
            while s > 0:
                prev = kit.line_start(data, s - 1)
                if not data[prev:s].strip().startswith(b"--"):
                    break
                s = prev
            out.append(Decl(name, s, kit.line_end(data, b - 1), m.group(1).decode().lower()))
        return out


class DM(Profile):
    name = "dm"
    suffixes = (".dm", ".dme")
    family = suffixes
    self_loads = False
    mask = rb'(?s)//[^\n]*|/\*.*?\*/|"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\''
    pattern = re.compile(rb'(?m)^[ \t]*#[ \t]*include[ \t]+"(?P<path>[^"\n]+)"')

    def node_name(self, node):
        return _label(node)

    def key(self, name):
        return re.sub(r"/(?:proc|verb)(?=/)", "", "/" + name.strip("/")).strip("/")

    def resolve(self, raw, path):
        rel = raw.replace("\\", "/")
        return self._find(rel, [path.parent, self.root], "rel") or Load((0, 0), (0, 0), raw, None)

    def default_line(self, from_path, target, binding):
        return f'#include "{kit.relative_path(from_path.parent, target)}"'

    def refs(self, tree, data, names):
        return super().refs(tree, data, {self.key(n).split("/")[-1] for n in names})

    def decls(self, tree, data, path):
        out = []
        for n in tree.root_node.named_children:
            if n.type not in ("type_definition", "proc_definition", "var_definition"):
                continue
            head = kit.text(n, data).split("\n", 1)[0]
            name = head.split("(", 1)[0].strip()
            if name:
                s, e = kit.span(n, data)
                out.append(Decl(name, s, e))
        return out


class Lisp(Profile):
    name = "common lisp"
    suffixes = (".lisp", ".lsp", ".cl", ".asd")
    family = suffixes
    self_loads = False
    listed = True
    mask = rb'(?s);[^\n]*|#\|.*?\|#|"(?:[^"\\]|\\.)*"'
    ident_re = rb"[^\s()'\"`,;#|:]+"
    pattern = re.compile(rb'(?im)\((?:cl:)?load[ \t]+(?:#p)?"(?P<path>[^"\n]+)"')
    _DEF = re.compile(r"\(\s*(?:cl:)?(def[\w-]*|define-[\w-]+)\s+\(?\s*(?:setf\s+)?([^\s()]+)", re.I)
    _IN = re.compile(rb"(?im)^[ \t]*\((?:cl:)?in-package[^\n]*\)[ \t]*$")

    def node_name(self, node):
        return _label(node)

    def key(self, name):
        return name[1:-1] if name.startswith("|") else name.lower()

    def default_line(self, from_path, target, binding):
        return f'(load "{kit.relative_path(from_path.parent, target)}")'

    def decls(self, tree, data, path):
        out = []
        for n in tree.root_node.named_children:
            m = self._DEF.match(kit.text(n, data))
            if m and m.group(1).lower() not in ("defpackage", "define-package"):
                s, e = kit.span(n, data)
                out.append(Decl(m.group(2), s, e, m.group(1).lower()))
        return out

    def boiler(self, text):
        return self._IN.sub(b"", text)

    def new_file(self, target, loads, block, source, source_data):
        m = self._IN.search(source_data)
        head = [m.group().decode().strip()] if m else []
        return "\n\n".join(x for x in ("\n".join(head + loads), block.strip("\n")) if x) + "\n"


_ROBOT_SECTION = re.compile(rb"(?im)^\*+[ \t]*(settings?|keywords?|variables?|test cases?|tasks?|comments?)[ \t]*\**[ \t]*$")
_ROBOT_CELL = re.compile(rb"\S(?:\S| (?=\S))*")


def _robot_sections(data: bytes):
    """(name, start of the header, start of the body, end) of each section."""
    heads = list(_ROBOT_SECTION.finditer(data))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(data)
        kind = m.group(1).decode().lower().rstrip("s")
        yield kind, m.start(), kit.line_end(data, m.end() - 1), end


class Robot(Profile):
    name = "robot"
    suffixes = (".robot", ".resource")
    family = suffixes
    pattern = re.compile(rb"(?im)^Resource(?:[ \t]{2,}|\t)[ \t]*(?:\$\{CURDIR\}[/\\])?(?P<path>\S(?:[^\n]*?\S)?)[ \t]*$")

    def key(self, name):
        return re.sub(r"[\s_]", "", name).lower()

    def resolve(self, raw, path):
        rel = raw.replace("\\", "/")
        return self._find(rel, [path.parent], "rel") or self._find(rel, [self.root]) or Load((0, 0), (0, 0), raw, None)

    def default_line(self, from_path, target, binding):
        return f"Resource    {kit.relative_path(from_path.parent, target)}"

    def _section(self, data, kind):
        return next((s for s in _robot_sections(data) if s[0] == kind), None)

    def decls(self, tree, data, path):
        out = []
        for kind, _, body, end in _robot_sections(data):
            if kind not in ("keyword", "variable"):
                continue
            starts = [m.start() for m in re.finditer(rb"(?m)^(?![ \t#\n]|\.\.\.)[^\n]+", data[body:end])]
            for i, s in enumerate(starts):
                s += body
                e = starts[i + 1] + body if i + 1 < len(starts) else end
                if kind == "variable":
                    e = kit.line_end(data, s)
                chunk = data[s:e].rstrip()
                name = re.split(rb"  |\t| \| ", data[s:kit.line_end(data, s)].rstrip(b"\n"))[0].strip().decode()
                if name:
                    out.append(Decl(name, s, s + len(chunk) + (1 if data[s + len(chunk):s + len(chunk) + 1] == b"\n" else 0),
                                    "var" if kind == "variable" else "def"))
        return out

    def _cells(self, data):
        blank = re.sub(rb"(?m)(?:^|(?<=  )|(?<=\t))#[^\n]*", lambda m: b" " * len(m.group()), data)
        for m in _ROBOT_CELL.finditer(blank):
            if _ROBOT_SECTION.match(data, kit.line_start(data, m.start())) or m.group() in (b"|", b"..."):
                continue
            cell = m.group().decode()
            bare = re.sub(r"^(?:given|when|then|and|but)\s+", "", cell, flags=re.I)
            yield m.end() - len(bare.encode()), m.end(), bare

    def refs(self, tree, data, names):
        keys = {self.key(n) for n in names}
        for s, e, cell in self._cells(data):
            k = self.key(cell)
            if k in keys or (k.split(".", 1)[-1] in keys and "." in k):
                yield s, e
            for v in re.finditer(r"[$@&%]\{([^}]+)\}", cell):
                if self.key(v.group()) in keys or self.key("${" + v.group(1) + "}") in keys:
                    yield s, e

    def used(self, tree, data):
        out = set()
        for _, _, cell in self._cells(data):
            k = self.key(cell)
            out |= {k, k.split(".", 1)[-1]}
            out |= {self.key("${" + v + "}") for v in re.findall(r"[$@&%]\{([^}]+)\}", cell)}
        return out

    def load_point(self, data, lds):
        if lds:
            return max(ld.stmt[1] for ld in lds)
        sec = self._section(data, "setting")
        return sec[2] if sec else 0

    def put_loads(self, data, at, lines):
        if self._section(data, "setting") is None:
            return ("*** Settings ***\n" + "\n".join(lines) + "\n\n").encode() + data.lstrip(b"\n")
        return kit.insert_lines(data, at, lines)

    def place(self, data, block, path, at):
        kind = "variable" if re.match(r"[$@&]\{", block) else "keyword"
        sec = self._section(data, kind)
        if sec is None:
            head = "*** Variables ***" if kind == "variable" else "*** Keywords ***"
            return data.rstrip(b"\n") + f"\n\n{head}\n{block.strip(chr(10))}\n".encode()
        end = sec[3]
        body = data[:end].rstrip(b"\n")
        sep = b"\n" if kind == "variable" else b"\n\n"
        return body + sep + block.strip("\n").encode() + b"\n" + (b"\n" + data[end:] if end < len(data) else b"")

    def new_file(self, target, loads, block, source, source_data):
        kind = "Variables" if re.match(r"[$@&]\{", block) else "Keywords"
        head = "*** Settings ***\n" + "\n".join(loads) + "\n\n" if loads else ""
        return f"{head}*** {kind} ***\n{block.strip(chr(10))}\n"

    def tidy_rest(self, rest, path):
        for kind, head, body, end in reversed(list(_robot_sections(rest))):
            if kind in ("keyword", "variable") and not rest[body:end].strip():
                rest = rest[:head] + rest[end:]
        rest = re.sub(rb"(?m)^(\*+[^\n]*\*[ \t]*\n)(?:[ \t]*\n)+", rb"\1", rest)
        return kit.tidy(rest).rstrip(b"\n") + b"\n" if rest.strip() else b""

    def boiler(self, text):
        for kind, head, _, end in reversed(list(_robot_sections(text))):
            if kind in ("setting", "keyword", "variable", "comment"):
                text = text[:head] + (text[head:end] if kind in ("keyword", "variable") else b"") + text[end:]
        return _ROBOT_SECTION.sub(b"", text)


_COBOL_LEVEL = re.compile(rb"(?im)^(?P<pre>[ \t]*(?:\d{6}[ \t])?[ \t]*)(?P<lvl>01|77)[ \t]+(?P<name>[A-Z0-9][A-Z0-9-]*)")
_COBOL_HEAD = re.compile(rb"(?im)^[ \t]*(?:\d{6}[ \t])?[ \t]*[A-Z0-9][A-Z0-9-]*[ \t]+(?:SECTION|DIVISION)\b[^\n]*")
_COBOL_VERBS = frozenset({"EXIT", "GOBACK", "CONTINUE", "STOP", "END-IF", "END-PERFORM", "END-EVALUATE", "END-READ",
                          "END-CALL", "END-COMPUTE", "ELSE", "END-EXEC", "END-SEARCH", "END-STRING", "END-WRITE"})


class Cobol(Profile):
    name = "cobol"
    suffixes = (".cbl", ".cob", ".cobol", ".cpy")
    family = suffixes
    self_loads = False
    inline = True
    word = "copybook"
    mask = kit.TEXT_SYNTAX[".cbl"]
    ident_re = rb"[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9]|[A-Za-z]"
    pattern = re.compile(rb"""(?im)\bCOPY[ \t]+(?P<q>["']?)(?P<path>[\w$#@][\w.$#@-]*?)(?P=q)(?=[ \t.\n]|$)""")

    def key(self, name):
        return name.upper()

    def inline_from(self, path):
        return path.suffix.lower() != ".cpy"

    def check_move(self, source, target, name):
        if target.suffix.lower() != ".cpy":
            return f"COBOL programs share code through copybooks. Move `{name}` to a .cpy file."
        return None

    def resolve(self, raw, path):
        rel = raw.replace("\\", "/")
        hit = self._find(rel, [path.parent], "rel")
        if hit:
            return hit
        want = rel.upper()
        found = sorted((f for f in [*self.files, *self.pending] if (f.name.upper() == want or (f.stem.upper() == want and not Path(rel).suffix))),
                       key=lambda f: (f.parent != path.parent, len(f.parts), f))
        if found:
            return Load((0, 0), (0, 0), raw, found[0].resolve(), "name")
        return Load((0, 0), (0, 0), raw, None)

    def render(self, ld, target, from_path):
        if ld.style != "name":
            return super().render(ld, target, from_path)
        out = target.name if Path(ld.raw).suffix else target.stem
        return out.upper() if ld.raw.isupper() else out

    def default_line(self, from_path, target, binding):
        return f"COPY {target.stem.upper()}."

    def load_line(self, data, from_path, target, like, binding=None, names=None):
        line = super().load_line(data, from_path, target, like, binding, names) if like is not None else \
            self.default_line(from_path, target, binding)
        m = re.search(r"COPY\s+\S+?(?=[ \t.]|$)", line, re.I)
        return line[: m.end()] + "." if m and not line[m.end():].strip().startswith((".", "REPLACING", "OF", "IN")) and \
            not line.rstrip().endswith(".") else line

    def decls(self, tree, data, path):
        out = []
        heads = [m.start() for m in _COBOL_HEAD.finditer(data)]
        levels = [m for m in _COBOL_LEVEL.finditer(data) if not _comment_at(tree, m.start("lvl"))]
        proc = re.search(rb"(?im)^[^*\n]*\bPROCEDURE[ \t]+DIVISION\b", data)
        paras = []
        if proc is not None:
            ind = len(re.match(rb"[ \t]*(?:\d{6})?[ \t]*", data[kit.line_start(data, proc.start()):]).group())
            for m in re.finditer(rb"(?im)^(?P<pre>[ \t]*(?:\d{6}[ \t])?[ \t]*)(?P<name>[A-Z0-9][A-Z0-9-]*)(?:[ \t]+SECTION)?[ \t]*\.[ \t]*$",
                                 data[proc.end():]):
                if len(m.group("pre")) <= max(ind, 11) and m.group("name").decode().upper() not in _COBOL_VERBS:
                    paras.append((proc.end() + m.start(), m.group("name").decode()))
        stops = sorted(set(heads + [m.start() for m in levels] + [p for p, _ in paras] + [len(data)]))
        if path.suffix.lower() == ".cpy" and not levels and not paras:
            return out
        for start, name in [(m.start(), m.group("name").decode()) for m in levels] + paras:
            end = next(s for s in stops if s > start)
            out.append(Decl(name, start, end))
        return out

    def file_plan(self, st, old, new, files, notes):
        """Rename the PROGRAM-ID that names the file, and the CALLs of it."""
        if old.stem.upper() == new.stem.upper():
            if old.parent != new.parent and old.suffix.lower() == ".cpy":
                if not any(f.parent == new.parent and f.suffix.lower() == ".cpy" for f in files if f != old):
                    notes.append(f"Add {kit.rel(self.root, new.parent) or '.'} to the copybook path of your build (cobc -I).")
            return
        data = st.data(old)
        m = re.search(rb"(?im)\bPROGRAM-ID\.[ \t]*(?P<q>[\"']?)(?P<id>[\w-]+)(?P=q)", data)
        if m is None or m.group("id").upper() != old.stem.upper().encode():
            return
        old_id = m.group("id").decode()
        new_id = new.stem.upper() if old_id.isupper() else new.stem
        st.draft.put(old, data[:m.start("id")] + new_id.encode() + data[m.end("id"):])
        for f in files:
            d = st.data(f)
            call = re.compile(rb"(?i)(\bCALL[ \t]+[\"'])" + re.escape(old_id.encode()) + rb"(?=[\"'])")
            if f != old and call.search(d):
                st.draft.put(f, call.sub(lambda c: c.group(1) + new_id.encode(), d))
        notes.append(f"Renamed PROGRAM-ID {old_id} to {new_id} and its CALLs.")


class Ejs(Profile):
    name = "ejs"
    suffixes = (".ejs",)
    family = suffixes
    self_loads = False
    exts = (".ejs",)
    word = "template"
    pattern = re.compile(rb"""(?:\binclude\(\s*|<%[-=]?\s*include\s+)(?P<q>['"]?)(?P<path>[^'"\s)%]+)(?P=q)""")

    def resolve(self, raw, path):
        rel = raw.replace("\\", "/")
        return self._find(rel, [path.parent], "rel") or self._find(rel, [self.root / "views", self.root]) or \
            Load((0, 0), (0, 0), raw, None)

    def default_line(self, from_path, target, binding):
        rel = kit.relative_path(from_path.parent, target)
        return f"<%- include('{rel[:-4] if rel.endswith('.ejs') else rel}') %>"


class Terraform(Profile):
    name = "terraform"
    suffixes = (".tf", ".tfvars", ".hcl")
    family = suffixes
    scope = "dir"
    self_loads = False

    def node_name(self, node):
        return _label(node)

    def check_move(self, source, target, name):
        if target.suffix.lower() != ".tf":
            return f"Move Terraform blocks to a .tf file, not {target.name}."
        return None

    def _addr(self, n, data):
        kind = next((c for c in n.named_children if c.type == "identifier"), None)
        labels = [kit.text(c, data).strip('"') for c in n.named_children if c.type in ("string_lit", "identifier")][1:]
        if kind is None:
            return None
        k = kit.text(kind, data)
        if k == "resource" and len(labels) == 2:
            return ".".join(labels)
        if k == "data" and len(labels) == 2:
            return "data." + ".".join(labels)
        if k in ("variable", "module", "output", "provider") and labels:
            return {"variable": "var"}.get(k, k) + "." + labels[0]
        return None

    def decls(self, tree, data, path):
        out = []
        body = next((c for c in tree.root_node.named_children if c.type == "body"), tree.root_node)
        for n in body.named_children:
            if n.type == "block":
                addr = self._addr(n, data)
                if addr:
                    s, e = kit.span(n, data)
                    out.append(Decl(addr, s, e))
        return out

    def file_plan(self, st, old, new, files, notes):
        if old.parent == new.parent:
            return
        names = [d.name for d in self.decls(st.tree(old), st.data(old), old)]
        refs = [n if not n.startswith("output.") else None for n in names]
        users = sorted({kit.rel(self.root, f) for f in files if f.parent == old.parent and f != old and f.suffix == ".tf"
                        for n in refs if n and re.search(rb"(?<![\w.])" + re.escape(n.encode()) + rb"\b", st.data(f))})
        if users:
            raise Unsupported(f"{', '.join(users)} {'uses' if len(users) == 1 else 'use'} the blocks of {old.name}, and a Terraform module is a folder. "
                              f"Move the blocks they share, or move the whole folder.")


class Dart(Profile):
    name = "dart"
    suffixes = (".dart",)
    family = suffixes
    pattern = re.compile(rb"""(?m)^[ \t]*(?:import|export|part)[ \t]+(?P<q>['"])(?P<path>[^'"\n]+)(?P=q)(?P<rest>[^;\n]*);""")

    def _package(self):
        spec = self.root / "pubspec.yaml"
        m = re.search(r"(?m)^name:\s*(\S+)", spec.read_text()) if spec.is_file() else None
        return m.group(1) if m else None

    def loads(self, tree, data, path):
        out = []
        pkg = self._package()
        for m in self.pattern.finditer(data):
            raw = m.group("path").decode()
            if raw.startswith("package:") and pkg and raw.startswith(f"package:{pkg}/"):
                ld = self._find(raw[len(pkg) + 9:], [self.root / "lib"]) or Load((0, 0), (0, 0), raw, None)
                ld.raw = raw
            elif raw.startswith(("dart:", "package:")):
                ld = Load((0, 0), (0, 0), raw, None)
            else:
                ld = self._find(raw, [path.parent], "rel") or Load((0, 0), (0, 0), raw, None)
            ld.stmt = (kit.line_start(data, m.start()), kit.line_end(data, m.end() - 1))
            ld.lit = (m.start("path"), m.end("path"))
            show = re.search(rb"\bshow\s+([\w\s,]+?)\s*(?:\bhide\b|$)", m.group("rest"))
            if show:
                ld.items = (m.start("rest") + show.start(1), m.start("rest") + show.end(1))
            out.append(ld)
        return out

    def render(self, ld, target, from_path):
        if ld.raw.startswith("package:"):
            rel = kit.relative_path(self.root / "lib", target)
            if not rel.startswith("../"):
                return f"package:{self._package()}/{rel}"
            return kit.relative_path(from_path.parent, target)
        return super().render(ld, target, from_path)

    def unlist(self, line, s, e):
        return re.sub(rb"\s+show\s*$", b"", line[:s]) + line[e:]

    def default_line(self, from_path, target, binding):
        return f"import '{kit.relative_path(from_path.parent, target)}';"

    def load_point(self, data, lds):
        lds = [ld for ld in lds if not data[ld.stmt[0]:ld.stmt[1]].lstrip().startswith(b"part")]
        return super().load_point(data, lds)

    _TYPES = ("class_definition", "enum_declaration", "mixin_declaration", "extension_declaration", "type_alias",
              "extension_type_declaration")

    def decls(self, tree, data, path):
        out = []
        kids = tree.root_node.named_children
        extra = lambda n: n.type in ("annotation", "marker_annotation", "documentation_comment")  # noqa: E731
        for i, n in enumerate(kids):
            name, last = None, n
            if n.type in self._TYPES:
                nm = n.child_by_field_name("name") or next((c for c in n.named_children if c.type in ("identifier", "type_identifier")), None)
                name = kit.text(nm, data) if nm is not None else None
            elif n.type in ("function_signature", "getter_signature", "setter_signature"):
                nm = n.child_by_field_name("name") or next((c for c in n.named_children if c.type == "identifier"), None)
                name = kit.text(nm, data) if nm is not None else None
                if i + 1 < len(kids) and kids[i + 1].type == "function_body":
                    last = kids[i + 1]
            elif n.type in ("static_final_declaration_list", "initialized_identifier_list"):
                nm = next((c for c in kit.walk(n) if c.type == "identifier"), None)
                name = kit.text(nm, data) if nm is not None else None
            if not name:
                continue
            first = n
            while first.prev_sibling is not None and first.prev_sibling.is_named and \
                    first.prev_sibling.type not in self._TYPES + ("function_body", "import_or_export", "comment",
                                                                  "documentation_comment") and \
                    kit.line_start(data, first.prev_sibling.end_byte) == kit.line_start(data, n.start_byte):
                first = first.prev_sibling
            s = kit.span(first, data, extra)[0]
            e = kit.line_end(data, last.end_byte - 1)
            if data[last.end_byte:e].strip() not in (b"", b";"):
                e = last.end_byte
            out.append(Decl(name, s, e, "def", not name.startswith("_")))
        return out


class Solidity(Profile):
    name = "solidity"
    suffixes = (".sol",)
    family = suffixes
    pattern = re.compile(rb"""(?m)^[ \t]*import[ \t]+(?:\{(?P<items>[^}]*)\}[ \t]*from[ \t]+|\*[ \t]+as[ \t]+\w+[ \t]+from[ \t]+)?"""
                         rb"""(?P<q>["'])(?P<path>[^"'\n]+)(?P=q)(?:[ \t]+as[ \t]+\w+)?[ \t]*;""")
    _TYPES = ("contract_declaration", "interface_declaration", "library_declaration", "struct_declaration",
              "enum_declaration", "function_definition", "error_declaration", "event_definition",
              "user_defined_type_definition", "constant_variable_declaration")

    def loads(self, tree, data, path):
        out = []
        for m in self.pattern.finditer(data):
            raw = m.group("path").decode()
            if raw.startswith("."):
                ld = self._find(raw, [path.parent], "rel")
            else:
                ld = self._find(raw, [self.root, self.root / "node_modules", self.root / "lib", self.root / "src"])
            ld = ld or Load((0, 0), (0, 0), raw, None)
            ld.stmt = (kit.line_start(data, m.start()), kit.line_end(data, m.end() - 1))
            ld.lit = (m.start("path"), m.end("path"))
            if m.group("items") is not None:
                ld.items = (m.start("items"), m.end("items"))
            out.append(ld)
        return out

    def render(self, ld, target, from_path):
        rel = super().render(ld, target, from_path)
        return rel if ld.style != "rel" or rel.startswith(".") else "./" + rel

    def unlist(self, line, s, e):
        return re.sub(rb"\{\s*$", b"", line[:s]) + re.sub(rb"^\s*\}\s*from\s*", b"", line[e:])

    def default_line(self, from_path, target, binding):
        rel = kit.relative_path(from_path.parent, target)
        return f'import "{rel if rel.startswith(".") else "./" + rel}";'

    def decls(self, tree, data, path):
        out = []
        for n in tree.root_node.named_children:
            if n.type in self._TYPES:
                nm = n.child_by_field_name("name") or next((c for c in n.named_children if c.type == "identifier"), None)
                if nm is not None:
                    s, e = kit.span(n, data)
                    out.append(Decl(kit.text(nm, data), s, e))
        return out

    def new_file(self, target, loads, block, source, source_data):
        head = [m.group().decode().strip() for m in
                re.finditer(rb"(?m)^[ \t]*(?://[ \t]*SPDX-License-Identifier:[^\n]*|pragma[ \t][^\n]*;)", source_data)]
        parts = ["\n".join(head), "\n".join(loads), block.strip("\n")]
        return "\n\n".join(x for x in parts if x) + "\n"


class Gradle(Profile):
    name = "gradle"
    suffixes = (".gradle",)
    family = suffixes
    self_loads = False
    pattern = re.compile(rb"""(?m)^[ \t]*apply[ \t(]+from[ \t]*[:=][ \t]*(?:file\(|rootProject\.file\()?[ \t]*(?P<q>['"])(?P<path>[^'"\n]+)(?P=q)""")

    def resolve(self, raw, path):
        rel = raw.replace("\\", "/")
        return self._find(rel, [path.parent], "rel") or self._find(rel, [self.root]) or Load((0, 0), (0, 0), raw, None)

    def default_line(self, from_path, target, binding):
        return f"apply from: '{kit.relative_path(from_path.parent, target)}'"

    def decls(self, tree, data, path):
        out = []
        for n in tree.root_node.named_children:
            t = kit.text(n, data)
            m = re.match(r"(?:def|void|[\w<>\[\]]+)\s+(\w+)\s*\(", t) or re.match(r"(?:task\s+|ext\.)(\w+)", t) or \
                re.match(r"tasks\.register\(\s*['\"](\w+)", t)
            if m:
                s, e = kit.span(n, data)
                # Each applied script has its own methods: only tasks and ``ext`` reach other scripts.
                out.append(Decl(m.group(1), s, e, "def", not t.lstrip().startswith(("def ", "void "))))
        return out


LANGS: dict[str, Profile] = {}
for _p in (C(), Ruby(), Shell(), PowerShell(), Julia(), Verilog(), Lua(), Zig(), R(), SQL(), DM(), Lisp(), Robot(),
           Cobol(), Ejs(), Terraform(), Dart(), Solidity(), Gradle()):
    for _s in _p.suffixes:
        LANGS[_s] = _p


def lang_of(path: Path) -> Profile:
    p = LANGS.get(path.suffix.lower())
    if p is None:
        raise Unsupported(f"No path-loaded language for {path.suffix} files.")
    return p


class _State:
    """Parsed files of one plan, read through the draft."""

    def __init__(self, prof: Profile, draft: kit.Draft):
        self.prof = prof
        self.draft = draft
        self._cache: dict = {}
        prof.exists = draft.exists

    def data(self, f: Path) -> bytes:
        return self.draft.get(f) if self.draft.exists(f) else b""

    def tree(self, f: Path):
        data = self.data(f)
        key = ("t", f, data)
        if key not in self._cache:
            self._cache[key] = kit.parse(f, data)
        return self._cache[key]

    def loads(self, f: Path) -> list[Load]:
        data = self.data(f)
        key = ("l", f, data)
        if key not in self._cache:
            self._cache[key] = self.prof.loads(self.tree(f), data, f) if data else []
        return self._cache[key]

    def closure(self, f: Path) -> set[Path]:
        seen, stack = set(), [f]
        while stack:
            for ld in self.loads(stack.pop()):
                t = ld.target
                if t is not None and t not in seen and self.draft.exists(t):
                    seen.add(t)
                    stack.append(t)
        return seen

    def uses(self, f: Path, name: str, skip=()) -> bool:
        data = self.data(f)
        if self.prof.mask is None and self.prof.key(name) == name and name.encode() not in data:
            return False
        return any(not any(s <= a < e for s, e in skip) for a, _ in self.prof.refs(self.tree(f), data, {name}))

    def add_load(self, f: Path, target: Path, like: Load | None = None, after: Load | None = None, binding=None,
                 names=None) -> None:
        """Add a load of ``target`` to ``f`` unless it has one."""
        if f == target or any(ld.target == target for ld in self.loads(f)):
            return
        data = self.data(f)
        lds = self.loads(f)
        like = like or next((ld for ld in lds if ld.target is not None and ld.binding is None), None)
        line = self.prof.load_line(data, f, target, like, binding, names)
        at = after.stmt[1] if after is not None else self.prof.load_point(data, lds)
        self.draft.put(f, self.prof.put_loads(data, at, [line]))

    def drop_item(self, f: Path, ld: Load, name: str) -> None:
        """Remove ``name`` from the name list of ``ld``; remove the load when the list is then empty."""
        data = self.data(f)
        s, e = ld.items
        kept = [x for x in data[s:e].decode().split(",") if x.strip() and
                self.prof.key(x.split()[0]) != self.prof.key(name)]
        if not kept:
            self.drop_load(f, ld)
        else:
            self.draft.put(f, data[:s] + ", ".join(x.strip() for x in kept).encode() + data[e:])

    def drop_load(self, f: Path, ld: Load) -> None:
        data = self.data(f)
        self.draft.put(f, kit.tidy(data[: ld.stmt[0]] + data[ld.stmt[1]:]))


_LOADISH = re.compile(rb"@import|\brequire|#[ \t]*(?:include|import)|\bsource\b|^[ \t]*\.[ \t]|\binclude\(|`include|Import-Module|"
                      rb"^[ \t]*(?:import|export|part|Resource|apply)\b|\bCOPY\b|\((?:cl:)?load\b|\\ir?\b")


def _put_loads(data: bytes, at: int, lines: list[str]) -> bytes:
    """Insert load lines at ``at`` and keep them in the group of loads around them."""
    prev = data[kit.line_start(data, max(at - 1, 0)):at] if at > 0 else b""
    nxt = data[at:kit.line_end(data, at)] if at < len(data) else b""
    before = bool(prev.strip()) and not _LOADISH.search(prev)
    after = bool(nxt.strip()) and not _LOADISH.search(nxt) and not nxt.lstrip().startswith((b"//", b";"))
    if at > 0 and not data[:at].endswith(b"\n"):
        data = data[:at] + b"\n" + data[at:]
        at += 1
    return kit.insert_lines(data, at, lines, blank_before=before, blank_after=after)


def _bind_name(prof, target: Path, taken: set[str]) -> str:
    """A binding name for ``target``: its stem, or its folder and stem when the stem is reserved."""
    word = target.stem if target.stem != "init" else target.parent.name
    if word in prof.reserved or word in taken:
        word = f"{target.parent.name}_{word}" if target.parent.name else word + "_mod"
    return _unique(word, taken | set(prof.reserved))


def _unique(word: str, taken: set[str]) -> str:
    word = re.sub(r"\W", "_", word) or "m"
    out, i = word, 2
    while out in taken:
        out, i = f"{word}{i}", i + 1
    return out


def _build_files(root: Path) -> list[Path]:
    from graphify.moves import _SKIP_DIRS

    out = []
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in _SKIP_DIRS and not x.startswith(".")]
        out += [Path(d) / n for n in names if n in BUILD_FILES or n.endswith(BUILD_SUFFIXES)]
    return out


def _build_paths(draft: kit.Draft, root: Path, old: Path, new: Path, keep_old: bool, notes: list, before: bool = False) -> bool:
    """Rewrite or extend the path of ``old`` in build files. ``before``: list the new file before ``old``."""
    done = False
    for b in _build_files(root):
        data = draft.get(b)
        text = data.decode("utf-8", "replace")
        if b.suffix == ".f" and any(line.strip() and not re.match(r"\s*(?://|#|[-+]|[\w./${}()-]+\s*$)", line)
                                    for line in text.splitlines()):
            continue
        o = kit.relative_path(b.parent, old)
        n = kit.relative_path(b.parent, new)
        if b.suffix == ".asd" and old.suffix in Lisp.suffixes:
            o, n = o[: -len(old.suffix)], n[: -len(new.suffix)]
            comp = re.compile(rf'\(:file\s+"{re.escape(o)}"[^)]*\)')
            if not comp.search(text):
                continue
            if keep_old:
                m = comp.search(text)
                pad = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
                sep = "\n" + pad if not pad.strip() else " "
                add = f'(:file "{n}")'
                new_text = text[: m.start()] + (add + sep + m.group() if before else m.group() + sep + add) + text[m.end():]
            else:
                new_text = comp.sub(lambda m: m.group().replace(f'"{o}"', f'"{n}"'), text)
            draft.put(b, new_text.encode())
            notes.append(f"Updated {kit.rel(root, b)}.")
            done = True
            continue
        pat = re.compile(rf"(?<![\w./\\-]){re.escape(o)}(?![\w./\\-])")
        if not pat.search(text):
            continue
        if keep_old:
            lines = text.split("\n")
            for i, line in enumerate(lines):
                m = pat.search(line)
                if m is None:
                    continue
                if line.strip().strip("\"',") == o:
                    lines.insert(i + 1, line.replace(o, n))
                else:
                    lines[i] = line[: m.end()] + " " + n + line[m.end():]
                break
            new_text = "\n".join(lines)
        else:
            new_text = pat.sub(n, text)
        if new_text != text:
            draft.put(b, new_text.encode())
            notes.append(f"Updated {kit.rel(root, b)}.")
            done = True
    return done


def _files(prof, root, graph, *extra):
    files = set(project_files(root, graph, prof.family))
    files |= {p for p in extra if p.is_file()}
    return sorted(files)


def plan_move(graph: dict, node: dict, source: Path, to: str, root: Path) -> MovePlan:
    prof = lang_of(source)
    name = prof.node_name(node)
    key = prof.key(name)
    target = resolve_path(root, to)
    if target.suffix.lower() not in prof.family:
        raise Unsupported(f"Move {prof.name} code to a {'/'.join(prof.family)} file, not {to}.")
    if target == source:
        raise Unsupported(f"`{name}` is already in {kit.rel(root, source)}.")
    why = prof.check_move(source, target, name)
    if why:
        raise Unsupported(why)
    if prof.scope == "dir" and target.parent != source.parent:
        raise Unsupported(f"The files of a {prof.name} folder share their definitions. Move `{name}` to a file in "
                          f"{kit.rel(root, source.parent) or '.'}, or move the whole folder.")
    files = _files(prof, root, graph, source, target)
    prof.setup(root, files)
    prof.pending = (target,)
    draft = kit.Draft()
    st = _State(prof, draft)
    decls = [d for d in prof.decls(st.tree(source), st.data(source), source) if prof.key(d.name) == key]
    if not decls:
        raise Unsupported(f"`{name}` is not a top-level definition of {kit.rel(root, source)}.")
    name = decls[0].name
    if draft.exists(target) and any(prof.key(d.name) == key for d in prof.decls(st.tree(target), st.data(target), target)):
        return MovePlan(name, str(source), str(target), {}, (f"{kit.rel(root, target)} already declares `{name}`.",))
    jobs = [(source, target, decls)]
    notes: list[str] = []
    conflicts: list[str] = []
    if isinstance(prof, C) and source.suffix in _C_SOURCES:
        header = prof.pair(source)
        hdecls = [d for d in prof.decls(st.tree(header), st.data(header), header) if d.name == name] if header else []
        if hdecls:
            th = prof.pair(target) if draft.exists(target) else None
            if th is None or th == header:
                offset = os.path.relpath(target.parent, source.parent)
                th = Path(os.path.normpath(header.parent / offset / (target.stem + header.suffix)))
            jobs.append((header, th, hdecls))
    vis_from, vis_to = jobs[-1][0], jobs[-1][1]
    before = {f: (st.closure(f) if prof.self_loads else set()) for f in files}
    loaders = [f for f in files if any(ld.target == vis_from for ld in st.loads(f))]
    moved: dict[Path, tuple] = {}
    for frm, to_path, ds in jobs:
        _move_one(prof, st, root, name, frm, to_path, ds, jobs, conflicts, notes, moved)
    if conflicts:
        return MovePlan(name, str(source), str(target), {}, tuple(dict.fromkeys(conflicts)), tuple(dict.fromkeys(notes)))
    if prof.binding:
        _rebind(prof, st, files, name, source, target)
    elif prof.scope == "dir":
        pass
    elif prof.self_loads:
        for f in sorted(set(files) | {source}, key=lambda f: (f not in (source, vis_from), len(before.get(f, ())), f)):
            if f in (vis_to, target) or not draft.exists(f):
                continue
            if vis_from in before.get(f, set()) or f == vis_from or f == source:
                via = next((ld for ld in st.loads(f) if ld.target == vis_from), None)
                listed = via is not None and via.items is not None
                if listed and not any(prof.key(x.split()[0]) == key for x in
                                      st.data(f)[via.items[0]:via.items[1]].decode().split(",") if x.strip()):
                    continue
                if st.uses(f, name) and vis_to not in st.closure(f):
                    st.add_load(f, vis_to, like=via, after=via, names=[name] if listed else None)
                if listed:
                    st.drop_item(f, next(ld for ld in st.loads(f) if ld.target == vis_from), name)
        if isinstance(prof, C) and vis_to != target and target.suffix in _C_SOURCES:
            st.add_load(target, vis_to)
    elif not prof.listed or vis_to.suffix in (".svh", ".vh"):
        for f in loaders:
            if vis_to not in st.closure(f) and f != vis_to:
                st.add_load(f, vis_to, after=next((ld for ld in st.loads(f) if ld.target == vis_from), None))
        if not loaders and st.uses(source, name) and vis_to not in st.closure(source):
            st.add_load(source, vis_to)
        if not loaders and not any(any(ld.target == vis_to for ld in st.loads(f)) for f in files):
            notes.append(f"Nothing in the project loads {kit.rel(root, vis_from)}. Load {kit.rel(root, vis_to)} where you load it.")
    if prof.ordered:
        late = _runs_before(prof, st, files, name, jobs)
        if late:
            return MovePlan(name, str(source), str(target), {}, (late,), tuple(dict.fromkeys(notes)))
    if not prof.cycles:
        for frm, to_path, _ in jobs:
            if draft.exists(frm) and frm in st.closure(to_path) and to_path in st.closure(frm):
                return MovePlan(name, str(source), str(target), {}, (
                    f"The move makes {kit.rel(root, frm)} and {kit.rel(root, to_path)} load each other, and {prof.name} cannot "
                    f"load that cycle. Move the code that `{name}` and {frm.name} share as well.",), tuple(dict.fromkeys(notes)))
    for frm, to_path, _ in jobs:
        if not _keeps(prof, st, frm):
            for f in files:
                for ld in st.loads(f):
                    if ld.target == frm:
                        if not prof.binding and to_path not in st.closure(f) and f != to_path:
                            st.add_load(f, to_path, like=ld, after=ld)
                        st.drop_load(f, ld)
                        break
            draft.remove(frm)
            notes.append(f"Deleted {kit.rel(root, frm)}: no code stays in it.")
            _build_paths(draft, root, frm, to_path, False, notes)
        elif not Path(to_path).is_file() and not (isinstance(prof, C) and to_path.suffix in _C_HEADERS):
            if not _build_paths(draft, root, frm, to_path, True, notes, prof.listed and st.uses(frm, name)) and prof.listed \
                    and not prof.ordered:
                notes.append(f"Add {kit.rel(root, to_path)} to the file list of your build.")
    return MovePlan(name, str(source), str(target), draft.final(), (), tuple(dict.fromkeys(notes)))


def _runs_before(prof, st, files, name, jobs) -> str | None:
    """A conflict when a file that runs before the new file uses the moved definition."""
    to_path = jobs[0][1]
    if any(ld.target == to_path for f in files for ld in st.loads(f)):
        return None
    peers = sorted(f for f in set(files) | {jobs[0][0]} if f.parent == to_path.parent and f != to_path and st.draft.exists(f))
    for f in peers:
        if f.name < to_path.name and st.uses(f, name):
            return (f"{f.name} runs before {to_path.name} and uses `{name}`. Move `{name}` to a file that runs no "
                    f"later than {f.name}.")
    data = st.data(to_path)
    tree = st.tree(to_path)
    used = prof.used(tree, data) - {prof.key(d.name) for d in prof.decls(tree, data, to_path)}
    for f in peers:
        if f.name > to_path.name:
            late = sorted(d.name for d in prof.decls(st.tree(f), st.data(f), f) if prof.key(d.name) in used)
            if late:
                return (f"`{name}` uses {', '.join(f'`{x}`' for x in late)} from {f.name}, which runs after "
                        f"{to_path.name}. Move `{name}` to a file that runs after {f.name}.")
    return None


def _keeps(prof, st, f) -> bool:
    data = st.data(f)
    if prof.decls(st.tree(f), data, f):
        return True
    rest = bytearray(data)
    for ld in st.loads(f):
        rest[ld.stmt[0]:ld.stmt[1]] = b" " * (ld.stmt[1] - ld.stmt[0])
    text = bytes(rest)
    text = re.sub(rb"(?m)^[ \t]*(?://|#|--|%).*$|/\*.*?\*/", b"", text, flags=re.S)
    if isinstance(prof, C):
        text = re.sub(rb"(?m)^[ \t]*#[ \t]*(?:pragma[ \t]+once|ifndef|define|endif|include|import)\b[^\n]*", b"", text)
    if isinstance(prof, Lua):
        text = re.sub(rb"(?m)^[ \t]*(?:local[ \t]+\w+[ \t]*=[ \t]*\{[ \t]*\}|return[ \t]+\w+)[ \t]*;?[ \t]*$", b"", text)
    return bool(prof.boiler(text).strip())


def _move_one(prof, st, root, name, frm, to_path, ds, jobs, conflicts, notes, moved):
    draft = st.draft
    data = st.data(frm)
    ranges = sorted((d.start, d.end) for d in ds)
    block = "\n\n".join(kit.piece(data, s, e) for s, e in ranges)
    rest = kit.cut(data, ranges)
    if prof.inline and prof.inline_from(frm) and to_path not in st.closure(frm):
        s0, e0 = ranges[0]
        indent = re.match(rb"[ \t]*", data[s0:]).group()
        like = next((ld for ld in st.loads(frm) if ld.target is not None), None)
        line = indent + prof.load_line(data, frm, to_path, like).strip().encode() + b"\n"
        moved_ranges = [(s + len(line) - (e0 - s0), e + len(line) - (e0 - s0)) for s, e in ranges[1:]]
        rest = kit.cut(data[:s0] + line + data[e0:], moved_ranges)
    rest = prof.tidy_rest(rest, frm)
    if rest.strip():
        rest = rest.rstrip() + b"\n"
    rest_tree = kit.parse(frm, rest)
    rest_decls = prof.decls(rest_tree, rest, frm)
    remaining = {prof.key(d.name): d for d in rest_decls}
    block_tree = kit.parse(frm, block.encode())
    used = prof.used(block_tree, block.encode()) - {prof.key(name)}
    rest_uses = any(True for _ in prof.refs(rest_tree, rest, {name}))
    private = [d for d in ds if not d.public]
    if private and prof.binding and rest_uses:
        if isinstance(prof, Zig):
            block = prof.make_public(block, private[0])
            notes.append(f"Made `{name}` pub: {frm.name} still uses it.")
        else:
            conflicts.append(f"`{name}` is local to {frm.name}, and {frm.name} still uses it.")
    if private and isinstance(prof, C) and (rest_uses or any(d.kind == "proto" for d in ds)):
        conflicts.append(f"`{name}` is static in {frm.name}, and {frm.name} still uses it.")
    elif private and not prof.binding and rest_uses:
        conflicts.append(f"`{name}` is private to {frm.name}, and {frm.name} still uses it.")
    load_lines: list[str] = []
    target_exists = draft.exists(to_path)
    tdata = st.data(to_path)
    tloads = st.loads(to_path) if target_exists else []
    have = {ld.target for ld in tloads}
    lds = st.loads(frm)
    if isinstance(prof, C):
        th = jobs[-1][1] if len(jobs) > 1 and to_path.suffix in _C_SOURCES else None
        if th is not None and th not in have:
            like = next((ld for ld in lds if ld.target == jobs[-1][0]), None)
            load_lines.append(prof.load_line(data, to_path, th, like))
            have.add(th)
    if prof.self_loads and not prof.binding:
        needs_self = sorted(n for n in used if n in remaining)
        for n in needs_self:
            if not remaining[n].public:
                word = "static" if isinstance(prof, C) else "private"
                conflicts.append(f"`{name}` uses `{remaining[n].name}`, which is {word} in {frm.name}.")
        if needs_self:
            via = frm
            if isinstance(prof, C) and frm.suffix in _C_SOURCES:
                h = prof.pair(frm)
                hnames = {d.name for d in prof.decls(st.tree(h), st.data(h), h)} if h else set()
                if h is None or not set(needs_self) <= hnames:
                    missing = sorted(set(needs_self) - hnames)
                    conflicts.append(f"`{name}` uses {', '.join(f'`{m}`' for m in missing)} from {frm.name}, "
                                     f"and no header declares {'it' if len(missing) == 1 else 'them'}.")
                via = h
            if via is not None and via not in have and via != to_path:
                load_lines.append(prof.load_line(data, to_path, via, next((ld for ld in lds if ld.target == via), None)))
                have.add(via)
        for ld in lds:
            if ld.target is None:
                continue
            if ld.target in have or ld.target == to_path:
                continue
            defined = set()
            for f in {ld.target} | st.closure(ld.target):
                defined |= {prof.key(d.name) for d in prof.decls(st.tree(f), st.data(f), f)}
            if used & defined:
                listed = None
                if ld.items is not None:
                    listed = [x.strip() for x in data[ld.items[0]:ld.items[1]].decode().split(",")
                              if x.strip() and prof.key(x.split()[-1]) in used]
                load_lines.append(prof.load_line(data, to_path, ld.target, ld, None, listed))
                have.add(ld.target)
        if isinstance(prof, C):
            sysl = [s for s in prof.system_loads(data) if s.encode() not in tdata]
            load_lines = sysl + load_lines
        elif isinstance(prof, Ruby):
            load_lines = [data[ld.stmt[0]:ld.stmt[1]].decode().strip() for ld in lds
                          if ld.target is None and data[ld.stmt[0]:ld.stmt[1]].strip() not in tdata] + load_lines
    if prof.binding:
        publish: set[str] = set()
        block, load_lines = _bind_block(prof, st, frm, to_path, name, ds, block, remaining, lds, tdata, tloads, conflicts, publish)
        for d in sorted((remaining[n] for n in publish), key=lambda d: -d.start):
            text = rest[d.start:d.end].decode()
            rest = rest[: d.start] + prof.make_public(text, d).encode() + rest[d.end:]
            notes.append(f"Made `{d.name}` pub in {frm.name}: the moved code uses it.")
    scope = ds[0].scope
    at = None
    if isinstance(prof, C) and scope:
        block, at = prof.wrap(block, scope, tdata)
    if target_exists:
        if at is None:
            at = prof.insert_point(tdata, to_path)
        new = prof.place(tdata, block, to_path, at)
        if load_lines:
            nl = prof.loads(kit.parse(to_path, new), new, to_path)
            new = prof.put_loads(new, prof.load_point(new, nl), list(dict.fromkeys(load_lines)))
        draft.put(to_path, new)
    else:
        draft.put(to_path, prof.new_file(to_path, list(dict.fromkeys(load_lines)), block, frm, data).encode())
    if prof.binding and rest_uses:
        rest = _rebind_rest(prof, frm, rest, to_path, name, notes)
    draft.put(frm, rest)
    moved[frm] = (to_path, block)


def _binding_for(prof, st, f, target, taken) -> tuple[str, bool]:
    for ld in st.loads(f):
        if ld.target == target and ld.binding:
            return ld.binding, False
    return _bind_name(prof, target, taken), True


def _bind_block(prof, st, frm, to_path, name, ds, block, remaining, lds, tdata, tloads, conflicts, publish):
    """Rewrite references of the moved block for its new file. Return the block and the loads it needs."""
    data = block.encode()
    tree = kit.parse(frm, data)
    edits = []
    load_lines = []
    taken = kit.words(kit.parse(to_path, tdata).root_node, tdata) | kit.words(tree.root_node, data) if tdata else \
        kit.words(tree.root_node, data)
    src_tab = ds[0].table
    own_binding = next((ld.binding for ld in tloads if ld.target == frm and ld.binding), None)
    need_self = False
    self_name = own_binding or _bind_name(prof, frm, taken)
    if isinstance(prof, Lua):
        tt = prof.table(kit.parse(to_path, tdata), tdata) if tdata else src_tab or "M"
        tt = tt or src_tab or "M"
        for s, e, member in prof.member_refs(tree, data, src_tab) if src_tab else ():
            if member == name:
                edits.append((s, e, tt))
            elif member in remaining:
                if not remaining[member].public:
                    conflicts.append(f"`{name}` uses `{member}`, which is local to {frm.name}.")
                edits.append((s, e, self_name))
                need_self = True
        local_used = {n for n in kit.words(tree.root_node, data) if n in remaining and not remaining[n].table}
        for n in sorted(local_used):
            if not remaining[n].public:
                conflicts.append(f"`{name}` uses `{n}`, which is local to {frm.name}.")
    else:
        for s, e, word in prof.bare_refs(tree, data, set(remaining)):
            edits.append((s, s, self_name + "."))
            need_self = True
            if not remaining[word].public:
                publish.add(word)
    if edits:
        data = replace_ranges(data, edits)
    block = data.decode()
    if need_self and own_binding is None:
        load_lines.append(prof.default_line(to_path, frm, self_name))
    used = kit.words(kit.parse(frm, data).root_node, data)
    for ld in lds:
        if ld.binding and ld.binding in used and ld.binding != self_name:
            if ld.target == to_path:
                block = re.sub(rf"\b{re.escape(ld.binding)}\.", "", block)
                continue
            if ld.target is None:
                line = st.data(frm)[ld.stmt[0]:ld.stmt[1]].decode().strip()
                if line.encode() not in tdata:
                    load_lines.append(line)
                continue
            if not any(t.target == ld.target for t in tloads):
                load_lines.append(prof.load_line(st.data(frm), to_path, ld.target, ld))
    if isinstance(prof, Zig) and re.search(r"\bstd\.", block) and b"@import(\"std\")" not in tdata:
        if not any("@import(\"std\")" in line for line in load_lines):
            load_lines.insert(0, 'const std = @import("std");')
    return block, load_lines


def _rebind_rest(prof, frm, rest, to_path, name, notes):
    """Reach the moved definition from the code that stays in its old file."""
    tree = kit.parse(frm, rest)
    lds = prof.loads(tree, rest, frm)
    taken = kit.words(tree.root_node, rest)
    binding = next((ld.binding for ld in lds if ld.target == to_path and ld.binding), None)
    fresh = binding is None
    binding = binding or _bind_name(prof, to_path, taken)
    edits = []
    if isinstance(prof, Lua):
        tab = prof.table(tree, rest)
        for s, e, member in prof.member_refs(tree, rest, tab) if tab else ():
            if member == name:
                edits.append((s, e, binding))
    else:
        for s, e, _ in prof.bare_refs(tree, rest, {name}):
            edits.append((s, s, binding + "."))
    if not edits:
        return rest
    rest = replace_ranges(rest, edits)
    if fresh:
        lds = prof.loads(kit.parse(frm, rest), rest, frm)
        line = prof.load_line(rest, frm, to_path, next((ld for ld in lds if ld.binding and ld.target), None), binding)
        rest = _put_loads(rest, prof.load_point(rest, lds), [line])
    return rest


def _rebind(prof, st, files, name, source, target):
    """Point member reads ``a.name`` of other files at the new file."""
    for f in files:
        if f == source or not st.draft.exists(f):
            continue
        for ld in st.loads(f):
            if ld.target != source or not ld.binding:
                continue
            data = st.data(f)
            tree = st.tree(f)
            refs = [(s, e) for s, e, m in prof.member_refs(tree, data, ld.binding) if m == name]
            if not refs:
                continue
            if f == target:
                own = prof.table(tree, data) if isinstance(prof, Lua) else ""
                edits = [(s, e + 1, own + "." if own else "") for s, e in refs]
                st.draft.put(f, replace_ranges(data, edits))
            else:
                b, fresh = _binding_for(prof, st, f, target, kit.words(tree.root_node, data))
                st.draft.put(f, replace_ranges(data, [(s, e, b) for s, e in refs]))
                if fresh:
                    st.add_load(f, target, like=ld, after=next((x for x in st.loads(f) if x.target == source), None), binding=b)
            lds = st.loads(f)
            old = next((x for x in lds if x.target == source and x.binding == ld.binding), None)
            if old is not None:
                data = st.data(f)
                tree = st.tree(f)
                left = [n for n in kit.tokens(tree.root_node, data, {ld.binding}) if not (old.stmt[0] <= n.start_byte < old.stmt[1])]
                if not left:
                    st.drop_load(f, old)
            break


def plan_move_file(graph: dict, old: Path, new: str, root: Path, tests: bool = True) -> MovePlan:
    prof = lang_of(old)
    target = resolve_path(root, new)
    if target.suffix.lower() not in prof.family:
        raise Unsupported(f"Keep a {prof.name} suffix: {new}.")
    if target.exists():
        return MovePlan(kit.rel(root, old), str(old), str(target), {}, (f"{kit.rel(root, target)} already exists.",))
    files = _files(prof, root, graph, old)
    prof.setup(root, files)
    pairs = [(old, target)]
    renames, notes = [], []
    if isinstance(prof, C):
        p = prof.pair(old)
        if p is not None and p.parent == old.parent and p.stem == old.stem:
            np = target.with_suffix(p.suffix)
            if not np.exists():
                pairs.append((p, np))
                renames.append((kit.rel(root, p), kit.rel(root, np)))
    if tests:
        for t, n, *_ in kit.test_pairs(root, old, target):
            if not n.exists() and t.suffix.lower() in prof.family:
                pairs.append((t, n))
                renames.append((kit.rel(root, t), kit.rel(root, n)))
    moving = dict(pairs)
    draft = kit.Draft()
    st = _State(prof, draft)
    for f in sorted(set(files) | set(moving)):
        data = st.data(f)
        if not data:
            continue
        at = moving.get(f, f)
        edits = []
        for ld in st.loads(f):
            if ld.target is None or (f not in moving and ld.target not in moving):
                continue
            if ld.style == "dotted" and ld.target not in moving:
                continue
            raw = prof.render(ld, moving.get(ld.target, ld.target), at)
            if raw != ld.raw.replace("\\", "/") and raw != ld.raw:
                edits.append((ld.lit[0], ld.lit[1], raw))
                if isinstance(prof, Lua) and ld.target == old:
                    renames.append((ld.raw, raw))
        if edits:
            draft.put(f, replace_ranges(data, edits))
    prof.file_plan(st, old, target, files, notes)
    for o, n in pairs:
        data = draft.get(o)
        draft.remove(o)
        draft.put(n, data)
        _build_paths(draft, root, o, n, False, notes)
    return MovePlan(kit.rel(root, old), str(old), str(target), draft.final(), (), tuple(dict.fromkeys(notes)), (),
                    tuple(dict.fromkeys(renames)))
