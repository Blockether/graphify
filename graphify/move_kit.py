"""Shared helpers for the move adapters of languages without a dedicated module.

They parse with the tree-sitter grammars of ``graphify.refactor``, list code tokens,
cut a definition with the comments and attributes above it, and insert text at
line boundaries. ``named_moves`` and ``path_moves`` build on them.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from graphify.refactor import GRAMMARS, _parser

IDENT_TYPES = frozenset({
    "identifier", "type_identifier", "simple_identifier", "name", "constant", "package_identifier",
    "field_identifier", "namespace_identifier", "word", "command_name", "simple_name",
})
_SKIP_WORDS = ("comment", "string", "heredoc", "char_literal", "character_literal", "regex", "raw_string")
WORD = re.compile(rb"[A-Za-z0-9_$]")


# File suffix -> grammar for languages that only moves parse: (module or "pack", language).
# "pack" loads the language from ``tree_sitter_language_pack``.
MOVE_GRAMMARS: dict[str, tuple[str, str]] = {
    ".metal": ("tree_sitter_cpp", "language"),
    ".ets": ("tree_sitter_typescript", "language_typescript"),
    ".ml": ("tree_sitter_ocaml", "language_ocaml"),
    ".mli": ("tree_sitter_ocaml", "language_ocaml_interface"),
    **dict.fromkeys((".pas", ".pp", ".dpr", ".dpk", ".lpr", ".inc"), ("tree_sitter_pascal", "language")),
    **dict.fromkeys((".lisp", ".lsp", ".cl", ".asd"), ("tree_sitter_commonlisp", "language")),
    ".sol": ("tree_sitter_solidity", "language"),
    ".sql": ("tree_sitter_sql", "language"),
    **dict.fromkeys((".tf", ".tfvars", ".hcl"), ("tree_sitter_hcl", "language")),
    **dict.fromkeys((".dm", ".dme"), ("tree_sitter_dm", "language")),
    ".vb": ("tree_sitter_vb_dotnet", "language"),
    **dict.fromkeys((".erl", ".hrl", ".escript"), ("pack", "erlang")),
    ".r": ("pack", "r"),
    ".dart": ("pack", "dart"),
    ".vue": ("pack", "vue"),
    ".svelte": ("pack", "svelte"),
    ".astro": ("pack", "astro"),
    **dict.fromkeys((".cls", ".trigger"), ("pack", "apex")),
}
# Languages without a grammar: suffix -> the comment and string pattern of a text tree.
TEXT_SYNTAX: dict[str, bytes] = {
    **dict.fromkeys((".cbl", ".cob", ".cobol", ".cpy"),
                    rb"(?m)^.{6}[*/][^\n]*|\*>[^\n]*|\"[^\"\n]*\"|'[^'\n]*'"),
    **dict.fromkeys((".robot", ".resource"), rb"(?m)(?:^|(?<=  ))#[^\n]*"),
    **dict.fromkeys((".razor", ".cshtml"), rb"(?s)@\*.*?\*@|<!--.*?-->|//[^\n]*|/\*.*?\*/|\"[^\"\n]*\""),
    ".ejs": rb"(?s)<%#.*?%>|<!--.*?-->|//[^\n]*",
}
_MOVE_LANGUAGES: dict = {}


def _move_parser(suffix: str):
    spec = MOVE_GRAMMARS.get(suffix)
    if spec is None:
        return None
    from tree_sitter import Language, Parser

    if spec not in _MOVE_LANGUAGES:
        if spec[0] == "pack":
            from tree_sitter_language_pack import get_language

            _MOVE_LANGUAGES[spec] = get_language(spec[1])
        else:
            import importlib

            _MOVE_LANGUAGES[spec] = Language(getattr(importlib.import_module(spec[0]), spec[1])())
    return Parser(_MOVE_LANGUAGES[spec])


class TextNode:
    """A node of a text tree: the same fields as a tree-sitter node, enough for this module."""

    is_named = True

    def __init__(self, type_: str, start: int, end: int, parent=None):
        self.type, self.start_byte, self.end_byte, self.parent = type_, start, end, parent
        self.children: list = []

    @property
    def child_count(self):
        return len(self.children)

    @property
    def named_children(self):
        return self.children

    def _sibling(self, step):
        if self.parent is None:
            return None
        sibs = self.parent.children
        i = sibs.index(self) + step
        return sibs[i] if 0 <= i < len(sibs) else None

    @property
    def prev_sibling(self):
        return self._sibling(-1)

    @property
    def next_sibling(self):
        return self._sibling(1)

    def child_by_field_name(self, name):
        return None

    def descendant_for_byte_range(self, a, b):
        for c in self.children:
            if c.start_byte <= a and b <= c.end_byte and (a < c.end_byte or c.start_byte == c.end_byte):
                return c
        return self


class TextTree:
    """Words, comments and strings of a file in a language without a grammar."""

    def __init__(self, data: bytes, skip: bytes):
        self.root_node = root = TextNode("source_file", 0, len(data))
        spans = [(m.start(), m.end(), "comment" if not m.group().startswith((b'"', b"'")) else "string")
                 for m in re.finditer(skip, data) if m.end() > m.start()]
        i = 0
        for s, e, kind in spans + [(len(data), len(data), None)]:
            for w in re.finditer(rb"[A-Za-z_$][\w$-]*[\w$]|[A-Za-z_$]", data[i:s]):
                root.children.append(TextNode("identifier", i + w.start(), i + w.end(), root))
            if kind:
                root.children.append(TextNode(kind, s, e, root))
            i = max(i, e)


def parse(path: Path, data: bytes):
    suffix = Path(path).suffix.lower()
    if suffix in TEXT_SYNTAX:
        return TextTree(data, TEXT_SYNTAX[suffix])
    parser = _parser(suffix) or _move_parser(suffix)
    if parser is None:
        raise ValueError(f"No grammar for {suffix} files.")
    return parser.parse(data)


def grammar(path: Path) -> str | None:
    suffix = Path(path).suffix.lower()
    return GRAMMARS.get(suffix, MOVE_GRAMMARS.get(suffix, (suffix if suffix in TEXT_SYNTAX else None,)))[0]


def text(node, data: bytes) -> str:
    return data[node.start_byte:node.end_byte].decode("utf-8", "replace")


def skipped(node) -> bool:
    return any(word in node.type for word in _SKIP_WORDS)


def walk(node, skip=skipped):
    """Every node under ``node`` in document order, without the subtrees that ``skip`` accepts."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n is not node and skip(n):
            continue
        yield n
        stack.extend(reversed(n.children))


def tokens(node, data: bytes, names, types=IDENT_TYPES):
    """Leaf identifier nodes whose text is one of ``names``, outside comments and strings."""
    names = set(names)
    for n in walk(node):
        if n.child_count == 0 and n.type in types and text(n, data) in names:
            yield n


def words(node, data: bytes) -> set[str]:
    """The identifier words of a subtree, outside comments and strings."""
    out = set()
    for n in walk(node):
        if n.child_count == 0 and n.is_named and not skipped(n):
            out.update(w.decode() for w in re.findall(rb"[A-Za-z_$][A-Za-z0-9_$]*", data[n.start_byte:n.end_byte]))
    return out


def is_word(data: bytes, i: int) -> bool:
    return 0 <= i < len(data) and bool(WORD.match(data[i:i + 1]))


def line_start(data: bytes, i: int) -> int:
    return data.rfind(b"\n", 0, i) + 1


def line_end(data: bytes, i: int) -> int:
    j = data.find(b"\n", i)
    return len(data) if j < 0 else j + 1


def leading(node, data: bytes, extra=lambda n: False) -> int:
    """The start of ``node`` with the comments and attributes right above it, at a line start."""
    start = node.start_byte
    prev = node.prev_sibling
    while prev is not None and ("comment" in prev.type or extra(prev)):
        gap = data[prev.end_byte:start]
        if gap.count(b"\n") > 1 or gap.strip():
            break
        start = prev.start_byte
        prev = prev.prev_sibling
    ls = line_start(data, start)
    return ls if not data[ls:start].strip() else start


def span(node, data: bytes, extra=lambda n: False) -> tuple[int, int]:
    """Whole lines of a definition, its comments and attributes, and one blank line after it."""
    start = leading(node, data, extra)
    end = line_end(data, node.end_byte - 1) if node.end_byte else node.end_byte
    if data[node.end_byte:end].strip() and data[node.end_byte:end].strip() not in (b";", b","):
        end = node.end_byte
    return start, end


def cut(data: bytes, ranges) -> bytes:
    """Remove the ranges, then collapse the blank lines that the cut leaves."""
    out = data
    for start, end in sorted(ranges, reverse=True):
        out = out[:start] + out[end:]
    out = re.sub(rb"\n[ \t]*\n([ \t]*\n)+", b"\n\n", out)
    return out.lstrip(b"\n") if not data.startswith(b"\n") else out


def tidy(data: bytes) -> bytes:
    """Collapse runs of blank lines to one blank line."""
    return re.sub(rb"\n[ \t]*\n([ \t]*\n)+", b"\n\n", data)


def piece(data: bytes, start: int, end: int) -> str:
    return data[start:end].decode("utf-8", "replace").strip("\n")


def indent_of(data: bytes, i: int) -> str:
    ls = line_start(data, i)
    m = re.match(rb"[ \t]*", data[ls:])
    return m.group().decode()


def reindent(block: str, old: str, new: str) -> str:
    if old == new:
        return block
    out = []
    for line in block.split("\n"):
        if line.startswith(old):
            line = new + line[len(old):]
        elif line.strip():
            line = new + line.lstrip()
        out.append(line)
    return "\n".join(out)


def insert_lines(data: bytes, at: int, lines: list[str], blank_before: bool = False, blank_after: bool = False) -> bytes:
    """Insert whole lines at the line boundary ``at``."""
    if not lines:
        return data
    body = "\n".join(lines) + "\n"
    if blank_before and at > 0 and not data[:at].endswith(b"\n\n"):
        body = "\n" + body
    if blank_after and not data[at:].startswith(b"\n"):
        body += "\n"
    return data[:at] + body.encode() + data[at:]


def append_block(data: bytes, block: str, at: int | None = None) -> bytes:
    """Put ``block`` at ``at`` (default: the end) with one blank line around it."""
    if at is None:
        at = len(data.rstrip(b"\n \t"))
        tail = b"\n"
        head = data[:at]
        return head + (b"\n\n" if head.strip() else b"") + block.strip("\n").encode() + tail
    before = data[:at].rstrip(b"\n")
    after = data[at:]
    sep_after = b"\n\n" if after.strip() and not after.lstrip(b" \t").startswith((b"}", b"end")) else b"\n"
    return before + b"\n\n" + block.strip("\n").encode() + sep_after + after.lstrip(b"\n")


def rel(root: Path, path) -> str:
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path)


def relative_path(from_dir: Path, to: Path) -> str:
    return Path(os.path.relpath(to, from_dir)).as_posix()


_TEST_DIRS = (("src/main/", "src/test/"), ("lib/", "test/"), ("lib/", "spec/"), ("src/", "test/"), ("src/", "tests/"),
              ("Sources/", "Tests/"), ("app/", "test/"), ("app/", "spec/"), ("", "test/"), ("", "tests/"), ("", "spec/"))
_TEST_NAMES = ("{stem}Test{suf}", "{stem}Tests{suf}", "{stem}_test{suf}", "{stem}_spec{suf}", "test_{stem}{suf}",
               "{stem}.test{suf}", "{stem}Spec{suf}")


def test_pairs(root: Path, old: Path, new: Path) -> list[tuple[Path, Path, str, str]]:
    """Test files of ``old`` and their places for ``new``: ``(test, new_test, old_stem, new_stem)``.

    A test file mirrors its source folder under a test root, such as
    ``src/main/java/a/Util.java`` and ``src/test/java/a/UtilTest.java``.
    """
    ro, rn = rel(root, old), rel(root, new)
    out = []
    for src, tst in _TEST_DIRS:
        if not (ro.startswith(src) and rn.startswith(src)):
            continue
        for pattern in _TEST_NAMES:
            stem_o, stem_n = Path(ro).stem, Path(rn).stem
            for suf in {old.suffix, ".exs"} if old.suffix == ".ex" else {old.suffix}:
                to = Path(ro[len(src):]).parent / pattern.format(stem=stem_o, suf=suf)
                tn = Path(rn[len(src):]).parent / pattern.format(stem=stem_n, suf=suf)
                t_old, t_new = root / tst / to, root / tst / tn
                if t_old.is_file() and t_old != old and t_old.resolve() not in {o for o, *_ in out}:
                    out.append((t_old.resolve(), t_new, stem_o, stem_n))
        if out:
            break
    return out


class Draft:
    """Old and new bytes of each file of a plan. Each file is read once."""

    def __init__(self):
        self.old: dict[str, bytes | None] = {}
        self.new: dict[str, bytes] = {}
        self.deleted: set[str] = set()

    def _load(self, path: Path) -> str:
        key = str(Path(path).resolve())
        if key not in self.old:
            self.old[key] = Path(key).read_bytes() if Path(key).is_file() else None
        return key

    def get(self, path: Path) -> bytes:
        key = self._load(path)
        return self.new.get(key, self.old[key] or b"")

    def exists(self, path: Path) -> bool:
        key = self._load(path)
        return key not in self.deleted and (key in self.new or self.old[key] is not None)

    def put(self, path: Path, data: bytes) -> None:
        key = self._load(path)
        self.new[key] = data
        self.deleted.discard(key)

    def remove(self, path: Path) -> None:
        key = self._load(path)
        self.new.pop(key, None)
        self.deleted.add(key)

    def final(self) -> dict:
        out = {}
        for key, old in self.old.items():
            if key in self.deleted:
                if old is not None:
                    out[key] = (old, None)
            elif key in self.new and self.new[key] != old:
                out[key] = (old, self.new[key])
        return out
