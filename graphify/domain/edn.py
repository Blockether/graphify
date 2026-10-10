"""Read Clojure and EDN source into plain, position-aware form values.

The reader sits on tree-sitter-clojure. It keeps the data that domain plugins
need (keywords, symbols, collections and literals, each with its line) and
drops reader noise such as comments, discard forms and metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

LIST = "list"
VECTOR = "vector"
MAP = "map"
SET = "set"
KEYWORD = "kw"
SYMBOL = "sym"
STRING = "str"
NUMBER = "num"
BOOL = "bool"
NIL = "nil"
CHAR = "char"
REGEX = "regex"
QUOTE = "quote"
DEREF = "deref"
TAGGED = "tagged"
FN = "fn"
OTHER = "other"

_COLLECTIONS = {LIST, VECTOR, MAP, SET, FN, QUOTE, DEREF, TAGGED}


@dataclass(eq=False)
class Form:
    """One read form.

    ``value`` holds the scalar value: the qualified name of a keyword or a
    symbol (``"ns/name"`` or ``"name"``), the decoded string, the number text,
    or ``True``/``False``. ``items`` holds the children of a collection; a
    map stores its keys and values alternately.
    """

    kind: str
    line: int
    value: object = None
    items: list["Form"] = field(default_factory=list)
    text: str = ""

    # -- scalar helpers ---------------------------------------------------
    @property
    def name(self) -> str:
        """Return the unqualified name of a keyword or a symbol."""
        v = str(self.value or "")
        return v.split("/", 1)[1] if "/" in v and v != "/" else v

    @property
    def namespace(self) -> str | None:
        v = str(self.value or "")
        return v.split("/", 1)[0] if "/" in v and v != "/" else None

    def is_kw(self, name: str | None = None) -> bool:
        return self.kind == KEYWORD and (name is None or self.value == name)

    def is_sym(self, name: str | None = None) -> bool:
        return self.kind == SYMBOL and (name is None or self.value == name)

    # -- collection helpers ----------------------------------------------
    @property
    def head(self) -> "Form | None":
        return self.items[0] if self.kind in (LIST, FN) and self.items else None

    @property
    def args(self) -> list["Form"]:
        return self.items[1:] if self.kind == LIST else list(self.items)

    def pairs(self) -> list[tuple["Form", "Form"]]:
        """Return the key and value pairs of a map."""
        it = self.items if self.kind == MAP else []
        return [(it[i], it[i + 1]) for i in range(0, len(it) - 1, 2)]

    def get(self, key: str) -> "Form | None":
        """Return the value of the keyword ``key`` in a map, or None."""
        for k, v in self.pairs():
            if k.kind == KEYWORD and k.value == key:
                return v
        return None

    def walk(self) -> Iterator["Form"]:
        """Yield this form and every nested form, depth first."""
        yield self
        for child in self.items:
            yield from child.walk()

    def __repr__(self) -> str:  # compact, for test failures
        return f"Form({self.kind}, {self.text[:40]!r}, L{self.line})"


def to_edn(form: Form | None) -> str:
    """Render a form back to compact EDN text."""
    if form is None:
        return "nil"
    k = form.kind
    if k == KEYWORD:
        return ":" + str(form.value)
    if k == SYMBOL:
        return str(form.value)
    if k == STRING:
        return '"' + str(form.value).replace("\\", "\\\\").replace('"', '\\"') + '"'
    if k in (NUMBER, CHAR, REGEX, OTHER):
        return form.text
    if k == BOOL:
        return "true" if form.value else "false"
    if k == NIL:
        return "nil"
    inner = " ".join(to_edn(i) for i in form.items)
    if k == LIST:
        return f"({inner})"
    if k == VECTOR:
        return f"[{inner}]"
    if k == MAP:
        return "{" + inner + "}"
    if k == SET:
        return "#{" + inner + "}"
    if k == FN:
        return f"#({inner})"
    if k == QUOTE:
        return "'" + inner
    if k == DEREF:
        return "@" + inner
    if k == TAGGED:
        return f"#{form.value} {inner}"
    return form.text


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(_ESCAPES.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


class Reader:
    """Convert tree-sitter-clojure nodes to :class:`Form` values.

    ``current_ns`` and ``aliases`` resolve auto-resolved keywords (``::x`` and
    ``::alias/x``) to their full namespace.
    """

    def __init__(self, source: bytes, current_ns: str | None = None, aliases: dict[str, str] | None = None):
        self.source = source
        self.current_ns = current_ns
        self.aliases = aliases or {}

    def text(self, node) -> str:
        return self.source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    @staticmethod
    def values(node) -> list:
        return [c for i, c in enumerate(node.children) if node.field_name_for_child(i) == "value"]

    def read(self, node) -> Form | None:
        t = node.type
        line = node.start_point[0] + 1
        if t in ("comment", "dis_expr"):
            return None
        if t == "kwd_lit":
            ns_node = node.child_by_field_name("namespace")
            name_node = node.child_by_field_name("name")
            name = self.text(name_node) if name_node is not None else self.text(node).lstrip(":")
            ns = self.text(ns_node) if ns_node is not None else None
            if self.text(node).startswith("::"):
                ns = self.aliases.get(ns, ns) if ns else self.current_ns
            return Form(KEYWORD, line, f"{ns}/{name}" if ns else name, text=self.text(node))
        if t == "sym_lit":
            ns_node = node.child_by_field_name("namespace")
            name_node = node.child_by_field_name("name")
            name = self.text(name_node) if name_node is not None else self.text(node)
            ns = self.text(ns_node) if ns_node is not None else None
            return Form(SYMBOL, line, f"{ns}/{name}" if ns else name, text=self.text(node))
        if t == "str_lit":
            raw = self.text(node)
            return Form(STRING, line, _unescape(raw[1:-1]), text=raw)
        if t == "num_lit":
            return Form(NUMBER, line, self.text(node), text=self.text(node))
        if t == "bool_lit":
            return Form(BOOL, line, self.text(node) == "true", text=self.text(node))
        if t == "nil_lit":
            return Form(NIL, line, None, text="nil")
        if t == "char_lit":
            return Form(CHAR, line, self.text(node), text=self.text(node))
        if t == "regex_lit":
            raw = self.text(node)
            return Form(REGEX, line, raw[2:-1], text=raw)
        kinds = {"list_lit": LIST, "vec_lit": VECTOR, "map_lit": MAP, "set_lit": SET, "anon_fn_lit": FN}
        if t in kinds:
            return Form(kinds[t], line, items=self._children(node), text=self.text(node))
        if t == "ns_map_lit":
            prefix = node.child_by_field_name("prefix")
            ns = self.text(prefix).lstrip("#:") if prefix is not None else ""
            items = self._children(node)
            for i in range(0, len(items), 2):
                k = items[i]
                if k.kind == KEYWORD and "/" not in str(k.value) and ns:
                    k.value = f"{ns}/{k.value}"
            return Form(MAP, line, items=items, text=self.text(node))
        if t in ("quoting_lit", "syn_quoting_lit"):
            return Form(QUOTE, line, items=self._children(node), text=self.text(node))
        if t == "var_quoting_lit":
            kids = self._children(node)
            return kids[0] if kids else None
        if t == "derefing_lit":
            return Form(DEREF, line, items=self._children(node), text=self.text(node))
        if t in ("unquoting_lit", "unquote_splicing_lit", "meta_lit"):
            kids = self._children(node)
            return kids[0] if kids else None
        if t == "read_cond_lit":
            vals = [v for v in (self.read(c) for c in self.values(node)) if v is not None]
            branches = {str(vals[i].value): vals[i + 1] for i in range(0, len(vals) - 1, 2) if vals[i].kind == KEYWORD}
            return branches.get("clj") or branches.get("default") or (vals[1] if len(vals) > 1 else None)
        if t == "tagged_or_ctor_lit":
            tag = node.child_by_field_name("tag")
            value = node.child_by_field_name("value")
            inner = self.read(value) if value is not None else None
            return Form(TAGGED, line, self.text(tag) if tag is not None else "", items=[inner] if inner else [], text=self.text(node))
        return Form(OTHER, line, self.text(node), text=self.text(node))

    def _children(self, node) -> list[Form]:
        out = []
        for child in self.values(node):
            f = self.read(child)
            if f is not None:
                out.append(f)
        return out


def parse_tree(source: bytes):
    """Parse ``source`` with tree-sitter-clojure and return its root node."""
    import tree_sitter_clojure as tsclj
    from tree_sitter import Language, Parser

    return Parser(Language(tsclj.language())).parse(source).root_node


def top_level_nodes(root) -> list:
    return [c for c in root.children if c.is_named and c.type not in ("comment", "dis_expr")]


def read_string(text: str, current_ns: str | None = None) -> list[Form]:
    """Read every top-level form of ``text``."""
    source = text.encode("utf-8")
    reader = Reader(source, current_ns)
    return [f for f in (reader.read(n) for n in top_level_nodes(parse_tree(source))) if f is not None]
