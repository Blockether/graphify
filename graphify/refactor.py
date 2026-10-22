"""Exact references and renames of one graph node, in every tree-sitter language.

A site is an identifier leaf that spells the node's name, outside strings and
comments. The graph anchors the sites: the definition line and the line of each
incoming edge (calls, references, imports, inherits...). A site off those lines
counts only in a linked file where no other node of the same name could own it.
Clojure vars use the exact resolver of ``graphify.clojure_refactor``.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# File suffix -> (grammar module, language function).
GRAMMARS: dict[str, tuple[str, str]] = {
    **dict.fromkeys((".py", ".pyi"), ("tree_sitter_python", "language")),
    **dict.fromkeys((".js", ".jsx", ".mjs", ".cjs"), ("tree_sitter_javascript", "language")),
    **dict.fromkeys((".ts", ".mts", ".cts"), ("tree_sitter_typescript", "language_typescript")),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".go": ("tree_sitter_go", "language"),
    ".rs": ("tree_sitter_rust", "language"),
    ".java": ("tree_sitter_java", "language"),
    **dict.fromkeys((".groovy", ".gradle"), ("tree_sitter_groovy", "language")),
    **dict.fromkeys((".c", ".h"), ("tree_sitter_c", "language")),
    **dict.fromkeys((".cpp", ".cc", ".cxx", ".hpp", ".hh", ".cu", ".cuh"), ("tree_sitter_cpp", "language")),
    **dict.fromkeys((".rb", ".rake"), ("tree_sitter_ruby", "language")),
    ".cs": ("tree_sitter_c_sharp", "language"),
    **dict.fromkeys((".kt", ".kts"), ("tree_sitter_kotlin", "language")),
    ".scala": ("tree_sitter_scala", "language"),
    ".php": ("tree_sitter_php", "language_php"),
    ".swift": ("tree_sitter_swift", "language"),
    **dict.fromkeys((".lua", ".luau"), ("tree_sitter_lua", "language")),
    ".zig": ("tree_sitter_zig", "language"),
    **dict.fromkeys((".ps1", ".psm1"), ("tree_sitter_powershell", "language")),
    **dict.fromkeys((".ex", ".exs"), ("tree_sitter_elixir", "language")),
    **dict.fromkeys((".m", ".mm"), ("tree_sitter_objc", "language")),
    ".jl": ("tree_sitter_julia", "language"),
    **dict.fromkeys((".f90", ".f95", ".f03", ".f08", ".f"), ("tree_sitter_fortran", "language")),
    **dict.fromkeys((".v", ".sv", ".svh", ".vh"), ("tree_sitter_verilog", "language")),
    **dict.fromkeys((".sh", ".bash"), ("tree_sitter_bash", "language")),
}
CLOJURE_SUFFIXES = (".clj", ".cljs", ".cljc", ".bb", ".edn")

# Edges that do not name their target at their line.
_NOT_SITES = frozenset({"contains", "method", "rationale_for", "case_of", "member_of", "defines"})
_TEXT = re.compile(r"string|comment|heredoc|char_literal|regex")
_CODE = re.compile(r"interpolation|substitution|template_argument")
_IMPORT = re.compile(r"import|use_declaration|use_list|(?<![a-z])require(?![a-z])|(?<![a-z])include(?![a-z])|export_clause|export_specifier")
_GLOB = re.compile(rb"::\s*\*|\bimport\s+\*|\bimport\s+(?:static\s+)?[\w.]+\.\*|\bexport\s+\*|\*\s+as\b|\brequire\s*\(")
_MEMBER = re.compile(r"attribute|member|field_expression|selector|navigation|scoped|qualified|property|dot")
_KEYED = re.compile(r"keyword_argument|named_argument|^pair$|field_initializer|keyed_element|value_argument_label|argument_label|labeled_argument|named_expression_argument|initializer_pair")
_VALID = re.compile(r"^[A-Za-z_$][\w$]*[?!]?$")


@dataclass(frozen=True)
class Occurrence:
    """One site that names the node: its definition, a use, an import or a member access."""

    file: str
    line: int
    col: int
    start: int
    end: int
    role: str
    text: str
    qualified: bool


@dataclass(frozen=True)
class Edit:
    """One replacement of the name range ``start:end`` in ``file``."""

    file: str
    line: int
    col: int
    start: int
    end: int
    old: str
    new: str


@dataclass(frozen=True)
class Mention:
    """Text that spells the name but is not a site of the node: strings, comments, other symbols."""

    file: str
    line: int
    text: str


@dataclass(frozen=True)
class RenamePlan:
    """The edits of one rename, and why it is not safe when it is not."""

    node: str
    old: str
    new: str
    edits: tuple[Edit, ...]
    conflicts: tuple[str, ...]
    mentions: tuple[Mention, ...]
    digests: dict = field(default_factory=dict)

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(sorted({e.file for e in self.edits}))


class StaleFile(RuntimeError):
    """A planned file changed on disk before the rename was applied."""


class Unsupported(ValueError):
    """The node has no name sites: a file, a concept, or a language without a grammar."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_graph(root) -> dict:
    """The ``graphify-out/graph.json`` of ``root``."""
    return json.loads((Path(root) / "graphify-out" / "graph.json").read_text(encoding="utf-8"))


def symbol_name(label: str) -> str:
    """The identifier of a node label: ``.save()`` -> ``save``, ``Foo.bar()`` -> ``bar``."""
    name = label.strip()
    if name.endswith("()"):
        name = name[:-2]
    return re.split(r"[.:/#]", name)[-1]


def _line(location) -> int:
    m = re.search(r"\d+", str(location or ""))
    return int(m.group()) if m else 0


_LANGUAGES: dict[tuple[str, str], object] = {}


def _parser(suffix: str):
    spec = GRAMMARS.get(suffix.lower())
    if spec is None:
        return None
    from tree_sitter import Language, Parser

    if spec not in _LANGUAGES:
        module = importlib.import_module(spec[0])
        _LANGUAGES[spec] = Language(getattr(module, spec[1])())
    return Parser(_LANGUAGES[spec])


_FUNC = re.compile(r"function|method|lambda|closure|constructor|subroutine|func_literal")
_TYPE = re.compile(r"type_annotation|type_query|type_arguments|generic_type|^type$")
_CLASS = re.compile(r"class|struct|impl_item|interface|trait|object_declaration|protocol|enum|record|extension")
_SELF = frozenset({"self", "this", "cls", "super", "Self", "@"})
# Grammars where a method body can call another method without a receiver.
_IMPLICIT_THIS = frozenset({
    "tree_sitter_java", "tree_sitter_groovy", "tree_sitter_c_sharp", "tree_sitter_cpp", "tree_sitter_kotlin",
    "tree_sitter_scala", "tree_sitter_swift", "tree_sitter_ruby", "tree_sitter_objc",
})
# Grammars that import a module by a path string, so no identifier names a module.
_PATH_IMPORTS = frozenset({
    "tree_sitter_javascript", "tree_sitter_typescript", "tree_sitter_c", "tree_sitter_cpp", "tree_sitter_ruby",
    "tree_sitter_php", "tree_sitter_lua", "tree_sitter_bash", "tree_sitter_powershell",
})
# Grammars where a bare name from another file needs an import that names it.
_NAMED_IMPORTS = frozenset({"tree_sitter_python", "tree_sitter_javascript", "tree_sitter_typescript", "tree_sitter_rust"})
_LOAD = re.compile(rb"^(await\s+)?(import|require)\s*\(")
# Method names that standard libraries also define: a call edge alone does not prove the receiver type.
_COMMON = frozenset("""
push pop len get set insert remove append extend clear contains iter map filter next parse write read close
open send drop clone new from into join split keys values items update add delete has size length find sort
reverse start stop run call apply emit on then format flush lock wait poll resolve reject is_empty to_string
toString equals hashCode compareTo path name id value kind key data
""".split())
# Wrapper types that pass method calls to their inner type.
_WRAPPERS = frozenset(b"Arc Rc Box Weak Pin Cow Ref RefMut RefCell Cell Mutex RwLock MutexGuard Option Some Self".split())
# Declarations of a field or property: their name is a member, not a reference.
_FIELD = re.compile(r"^(property_signature|public_field_definition|field_declaration|field_definition|property_declaration)$")
_IMPORTS = frozenset({"imports", "imports_from", "re_exports"})
# External bases without methods to override.
_EMPTY_BASES = frozenset({"ABC", "object", "Object", "Protocol", "Generic", "NamedTuple", "TypedDict", "Enum", "IntEnum", "StrEnum"})


def _receiver(text: str) -> str:
    """The last name of a receiver expression: ``self._client`` -> ``_client``, ``Foo(x)`` -> ``Foo``."""
    names = re.findall(r"[A-Za-z_$@][\w$]*", re.sub(r"\([^()]*\)|\[[^\[\]]*\]", "", text))
    return names[-1] if names else ""


def _in_all(node) -> bool:
    """A string of a Python ``__all__`` list: it names a symbol."""
    while node is not None and node.type not in ("assignment", "augmented_assignment", "expression_statement", "module"):
        node = node.parent
    if node is None or node.type not in ("assignment", "augmented_assignment"):
        return False
    left = node.child_by_field_name("left")
    return left is not None and left.text == b"__all__"


def _role(leaf) -> tuple[str, str | None] | None:
    """``(role, receiver)`` of a code leaf, or None inside a string or comment."""
    role, receiver = "use", None
    parent = leaf.parent
    if parent is not None and _KEYED.search(parent.type):
        key = parent.child_by_field_name("name") or parent.child_by_field_name("key") or parent.child_by_field_name("label")
        if key is None and parent.named_child_count > 1:
            key = parent.named_children[0]
        if key is not None and key.id == leaf.id:
            return None  # a parameter name, a field or a map key: not a reference
    if parent is not None and _FIELD.match(parent.type) and parent.child_by_field_name("name") == leaf:
        return "member", None
    if parent is not None and parent.type == "token_tree":
        dot = leaf.prev_sibling
        if dot is not None and dot.type in (".", "::") and dot.prev_sibling is not None:
            role, receiver = "member", dot.prev_sibling.text.decode("utf-8", "replace")  # f.path(..) in a macro
    elif parent is not None and _MEMBER.search(parent.type) and parent.named_child_count > 1:
        first = parent.named_children[0]
        if first.id != leaf.id:
            role = "member"
            receiver = _receiver(first.text.decode("utf-8", "replace"))
    elif parent is not None and leaf in (parent.child_by_field_name("name"), parent.child_by_field_name("method")):
        obj = parent.child_by_field_name("object") or parent.child_by_field_name("receiver")
        if obj is not None and obj.id != leaf.id:
            role, receiver = "member", _receiver(obj.text.decode("utf-8", "replace"))  # Java g.greet(), Ruby g.greet
    if _module_path(leaf):
        return None  # import a.b.name names a module, not a symbol
    if leaf.type == "shorthand_property_identifier_pattern" and _loader(leaf) is not None:
        return "import", None  # const { a } = require("./m")
    node = parent
    while node is not None:
        kind = node.type
        if _CODE.search(kind):
            break
        if _TEXT.search(kind):
            return ("export", None) if _in_all(node) else None
        if _IMPORT.search(kind) and role in ("use", "member"):
            role, receiver = "import", None  # use a::b::name
        node = node.parent
    return role, receiver


def _module_path(leaf) -> bool:
    """The leaf is a part of a Python module path: ``import a.b`` or ``from a.b import``."""
    node = leaf.parent
    if node is None or node.type != "dotted_name":
        return False
    while node.parent is not None and node.parent.type in ("dotted_name", "relative_import"):
        node = node.parent
    parent = node.parent
    if parent is not None and parent.type == "aliased_import" and parent.child_by_field_name("name") == node:
        parent = parent.parent
        return parent is not None and parent.type == "import_statement"
    if parent is None:
        return False
    return parent.type == "import_statement" or (parent.type == "import_from_statement" and parent.child_by_field_name("module_name") == node)

def _leaves(tree, needle: bytes):
    """Named leaves whose text is ``needle``."""
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.child_count == 0:
            if node.is_named and node.text == needle:
                yield node
            continue
        if needle in node.text:
            stack.extend(reversed(node.children))


def _ancestor(node, pattern, skip=None):
    node = node.parent
    while node is not None:
        if (skip is None or node.id != skip.id) and pattern.search(node.type) and not (
            pattern is _FUNC and _CLASS.search(node.type)
        ):
            return node
        node = node.parent
    return None


def _declaration(leaf):
    """The definition node that the name leaf names."""
    node = leaf.parent
    for _ in range(3):
        if node is None:
            return None
        if _FUNC.search(node.type) or _CLASS.search(node.type) or "definition" in node.type or "declaration" in node.type:
            return node
        node = node.parent
    return None


def _params(func) -> list:
    field = func.child_by_field_name("parameters")
    out = [field] if field is not None else []
    return out + [c for c in func.children if "parameter" in c.type and (field is None or c.id != field.id)]


def _binding(leaf, params) -> bool:
    """The leaf names a parameter, not a type or a default value."""
    child, node = leaf, leaf.parent
    while node is not None and child.id != params.id:
        if _TYPE.search(node.type):
            return False
        for name in ("type", "value", "right", "default", "default_value", "return_type"):
            if node.child_by_field_name(name) == child:
                return False
        child, node = node, node.parent
    return True


def _shadowed(leaf, needle: bytes, owner_decl) -> bool:
    """A parameter of an enclosing function, other than the owner, binds the name."""
    node = leaf.parent
    while node is not None:
        if owner_decl is not None and node.id == owner_decl.id:
            return False
        if _FUNC.search(node.type) and not _CLASS.search(node.type):
            for params in _params(node):
                if params.start_byte <= leaf.start_byte < params.end_byte:
                    return _binding(leaf, params)  # the leaf declares a parameter
                if any(_binding(p, params) for p in _leaves_in(params, needle)):
                    return True
        node = node.parent
    return False


def _leaves_in(node, needle: bytes):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.child_count == 0:
            if n.is_named and n.text == needle:
                yield n
        elif needle in n.text:
            stack.extend(n.children)


@dataclass
class _Site:
    occ: Occurrence
    receiver: str | None
    leaf: object


@dataclass
class _Scan:
    """The parsed name sites of one file."""

    data: bytes
    tree: object
    sites: list[_Site]
    imports: str | None = None
    nodes: list | None = None
    file: str = ""
    fields: dict | None = None


@dataclass
class _Owner:
    """A node that can own sites of the name: the target or a rival."""

    node: dict
    method: bool
    module: bool
    linked: set[str]  # files with an edge to the node, and its own file
    anchors: dict[str, set[int]]
    file: str = ""
    modules: set[str] = field(default_factory=set)  # its file and the files that re-export it
    class_name: str = ""
    family: set[str] = field(default_factory=set)  # labels of the class and its subclasses
    decl: object = None
    scope: tuple[int, int] | None = None  # the enclosing function of a nested definition
    klass: tuple[int, int] | None = None  # the enclosing class of a method
    stars: set[str] = field(default_factory=set)  # files that re-export its module with a glob
    unsure: list = field(default_factory=list)  # linked calls of a common method on receivers of unknown type


def _code_nodes(graph: dict) -> list[dict]:
    return [n for n in graph.get("nodes", ()) if n.get("file_type") == "code" and n.get("source_file")]


def _links(graph: dict):
    return graph.get("links", graph.get("edges", ()))


def _is_method(node: dict) -> bool:
    return str(node.get("label", "")).startswith(".")


def _resolve(root: Path, file: str) -> Path:
    path = Path(file)
    return path if path.is_absolute() else root / path


def _is_module(node: dict) -> bool:
    return Path(str(node.get("source_file") or "")).name == node.get("label")


def _families(graph: dict, owners: dict) -> None:
    """Give each method owner the labels of its class and every subclass in the graph."""
    links = list(_links(graph))
    byid = {n.get("id"): n for n in graph.get("nodes", ())}
    children: dict[str, set[str]] = {}
    for l in links:
        if l.get("relation") in ("inherits", "implements", "extends"):
            children.setdefault(l["target"], set()).add(l["source"])
    for l in links:
        owner = owners.get(l.get("target"))
        if owner is None or not owner.method or l.get("relation") != "method":
            continue
        seen, stack = {l["source"]}, [l["source"]]
        while stack:
            for child in children.get(stack.pop(), ()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        owner.family = {str(byid.get(c, {}).get("label", "")) for c in seen} - {""}


def _owners(graph: dict, node: dict, name: str) -> list[_Owner]:
    """The target first, then every other code node or module with the same name."""
    def same(n: dict) -> bool:
        if _is_module(n):
            return Path(n["source_file"]).stem == name
        return symbol_name(n.get("label", "")) == name

    nodes = [node] + [n for n in _code_nodes(graph) if n["id"] != node["id"] and same(n)]
    owners = {}
    for n in nodes:
        line = _line(n.get("source_location"))
        owners[n["id"]] = _Owner(n, _is_method(n), _is_module(n), {n["source_file"]}, {n["source_file"]: {line}}, n["source_file"])
    _families(graph, owners)
    modules = {n["id"]: n["source_file"] for n in _code_nodes(graph) if _is_module(n)}
    importers: dict[str, set[str]] = {}
    for link in _links(graph):
        source_file = link.get("source_file")
        if not source_file or link.get("relation") in _NOT_SITES:
            continue
        owner = owners.get(link.get("target"))
        if owner:
            owner.linked.add(source_file)
            owner.anchors.setdefault(source_file, set()).add(_line(link.get("source_location")))
        if link.get("relation") in _IMPORTS and link.get("target") in modules:
            importers.setdefault(modules[link["target"]], set()).add(source_file)
    for owner in owners.values():
        if not owner.module:
            owner.linked |= importers.get(owner.file, set())  # a file that imports the module can name the symbol
    return list(owners.values())


def _place(owner: _Owner, scan: _Scan, needle: bytes) -> None:
    """Find the definition leaf of an owner and its enclosing scopes."""
    line = _line(owner.node.get("source_location"))
    for site in scan.sites:
        if site.occ.line == line and site.occ.role != "member":
            decl = _declaration(site.leaf)
            if decl is None:
                continue
            owner.decl = decl
            func = _ancestor(decl, _FUNC, decl)
            if func is not None and not owner.method:
                owner.scope = (func.start_byte, func.end_byte)
            klass = _ancestor(decl, _CLASS)
            if klass is not None:
                owner.klass = (klass.start_byte, klass.end_byte)
                name = klass.child_by_field_name("name") or klass.child_by_field_name("type")  # impl<T> Foo<T>
                owner.class_name = (_type_name(name.text) or "") if name is not None else ""
            return


def _scan(root: Path, graph: dict, node: dict, name: str) -> tuple[dict[str, _Scan], list[_Owner]]:
    owners = _owners(graph, node, name)
    needle = name.encode("utf-8")
    family = GRAMMARS.get(Path(node["source_file"]).suffix.lower(), (None,))[0]
    files = sorted({n["source_file"] for n in _code_nodes(graph)} | set(owners[0].linked))
    scans: dict[str, _Scan] = {}
    for file in files:
        if GRAMMARS.get(Path(file).suffix.lower(), (None,))[0] != family:
            continue
        path = _resolve(root, file)
        if not path.is_file():
            continue
        data = path.read_bytes()
        if needle not in data:
            if file in owners[0].linked and _GLOB.search(data):
                glob = _Scan(data, _parser(path.suffix).parse(data), [], file=file)
                if _star(glob, owners[0]):
                    owners[0].stars.add(file)  # pub use m::*
            continue
        tree = _parser(path.suffix).parse(data)
        sites = []
        for leaf in _leaves(tree, needle):
            found = _role(leaf)
            if found is None:
                continue
            role, receiver = found
            line, col = leaf.start_point[0] + 1, leaf.start_point[1] + 1
            occ = Occurrence(file, line, col, leaf.start_byte, leaf.end_byte, role, name, receiver is not None)
            sites.append(_Site(occ, receiver, leaf))
        scans[file] = _Scan(data, tree, sites, file=file)
    for owner in owners:
        if owner.file in scans:
            _place(owner, scans[owner.file], needle)
    return scans, owners


def _imports(scan: _Scan) -> list:
    """The import statements of a file, cached on the scan."""
    if scan.nodes is not None:
        return scan.nodes
    out, stack = [], [scan.tree.root_node]
    while stack:
        n = stack.pop()
        if _IMPORT.search(n.type):
            out.append(n)
        elif n.child_count:
            stack.extend(n.children)
    scan.nodes = out
    return out


def _names_module(text: str, def_file: str) -> bool:
    """The import text names the module of ``def_file``: its stem, or its folder for an index file or a Go package."""
    path = Path(def_file)
    stem = path.stem
    if stem in ("__init__", "index", "mod", "lib", "main") or path.suffix == ".go":
        stem = path.parent.name
    return bool(stem) and re.search(r"(?<![\w$])" + re.escape(stem) + r"(?![\w$])", text) is not None


def _aliases(scan: _Scan, def_file: str) -> set[str]:
    """Names that stand for the module of ``def_file`` in this file, empty when no import names it."""
    out: set[str] = set()
    for n in _imports(scan):
        text = _path_text(n, scan)
        if _names_module(text, def_file):
            out.update(re.findall(r"[A-Za-z_$][\w$]*", text))
    if out:
        out |= {Path(def_file).stem, Path(def_file).parent.name}
    return out - {"import", "from", "as", "require", "use", "const", "let", "var", "export", "crate", "super"}


def _imported_names(scan: _Scan) -> str:
    """The text of every import statement of a file, cached on the scan."""
    if scan.imports is None:
        scan.imports = " ".join(_path_text(n, scan) for n in _imports(scan))
    return scan.imports


def _path_text(node, scan: _Scan) -> str:
    """Import text with Rust ``self::`` and ``super::`` replaced by the module names they stand for."""
    text = node.text.decode("utf-8", "replace")
    if not scan.file.endswith(".rs") or not re.search(r"\b(super|self)::", text):
        return text
    chain = list(Path(scan.file).with_suffix("").parts)
    if chain and chain[-1] in ("mod", "lib", "main"):
        chain.pop()
    inline, n = [], node.parent
    while n is not None:
        if n.type == "mod_item" and n.child_by_field_name("name") is not None:
            inline.append(n.child_by_field_name("name").text.decode("utf-8", "replace"))
        n = n.parent
    chain += reversed(inline)

    def up(m):
        k = m.group(0).count("super")
        return (chain[-1 - k] if len(chain) > k else "crate") + "::"

    text = re.sub(r"\bself::", (chain[-1] if chain else "crate") + "::", text)
    return re.sub(r"\b(?:super::)+", up, text)


def _loader(leaf):
    """The declarator of a destructured ``require("./m")`` or ``await import("./m")``."""
    node = leaf.parent
    while node is not None and node.type in ("object_pattern", "pair_pattern"):
        node = node.parent
    if node is None or node.type != "variable_declarator":
        return None
    value = node.child_by_field_name("value")
    return node if value is not None and _LOAD.search(value.text) else None


def _import_text(leaf, scan: _Scan) -> str:
    """The whole import statement around a leaf, with its module path."""
    node, found = leaf.parent, None
    while node is not None:
        if _IMPORT.search(node.type):
            found = node
        node = node.parent
    if found is not None and found.parent is not None and found.parent.type == "export_statement":
        found = found.parent  # export { a } from "./m"
    if found is None:
        found = _loader(leaf)
    return _path_text(found, scan) if found is not None else ""


def _receiver_fits(receiver: str | None, owner: _Owner, exact: bool = False) -> bool:
    """``client`` or ``gateway_client`` names an instance of ``GatewayClient`` or a subclass."""
    if not receiver:
        return False
    r = receiver.lstrip("_@$").replace("_", "").lower()
    for name in owner.family | {owner.class_name}:
        c = name.lower()
        if c and len(r) >= 3 and (c == r or (not exact and (c.endswith(r) or c.startswith(r)))):
            return True
    return False


def _named(text: str, owner: _Owner) -> bool:
    return any(_names_module(text, m) for m in owner.modules or {owner.file})


def _visible(owner: _Owner, aliases) -> set[str]:
    out: set[str] = set()
    for m in owner.modules or {owner.file}:
        out |= aliases(m)
    return out


def _can_own(owner: _Owner, file: str, site: _Site, scan: _Scan, needle: bytes, aliases) -> bool:
    occ = site.occ
    if owner.module:
        if GRAMMARS.get(Path(file).suffix.lower(), (None,))[0] in _PATH_IMPORTS:
            return False
        return occ.role not in ("member", "export") and file in owner.linked and occ.line in owner.anchors.get(file, ())
    if occ.role == "export":
        return file == owner.file or file in owner.modules
    if owner.decl is not None and owner.file == file and owner.decl.start_byte <= occ.start < owner.decl.end_byte and occ.line == _line(owner.node.get("source_location")):
        return True  # the definition itself
    if owner.scope is not None and (file != owner.file or not owner.scope[0] <= occ.start < owner.scope[1]):
        return False
    inside = owner.klass is not None and file == owner.file and owner.klass[0] <= occ.start < owner.klass[1]
    if owner.method:
        if occ.role == "member":
            if inside and site.receiver in _SELF:
                return True
            kind = _receiver_type(site, scan)
            if kind == "Self":
                kind = owner.class_name if inside else None
            if kind == "?" and needle.decode() in _COMMON:
                return False  # bound to a call result: most likely a library type
            if kind is not None and kind != "?":
                return kind in owner.family | {owner.class_name}  # a declared receiver type decides
            if _anchored(owner, file, site) and (needle.decode() not in _COMMON or _receiver_fits(site.receiver, owner)):
                return True  # an edge alone cannot tell a common method from a library one
            if needle.decode() not in _COMMON and _caller_anchored(owner, file, site):
                return True  # the graph keeps one edge for each caller, so more calls there share it
            # Without an edge, a receiver named after the class, in a file that sees it.
            if not _receiver_fits(site.receiver, owner):
                return False
            if _receiver_fits(site.receiver, owner, exact=True) or file in owner.linked or _visible(owner, aliases):
                return True
            texts = _imported_names(scan)
            return any(re.search(r"(?<![\w$])" + re.escape(c) + r"(?![\w$])", texts) for c in owner.family | {owner.class_name} if c)
        family = GRAMMARS.get(Path(file).suffix.lower(), (None,))[0]
        return inside and family in _IMPLICIT_THIS
    if file not in owner.linked and not _visible(owner, aliases):
        return False
    if file != owner.file:
        if occ.role == "import":
            return _named(_import_text(site.leaf, scan), owner)
        texts = [_import_text(s.leaf, scan) for s in scan.sites if s.occ.role == "import"]
        if texts and not any(_named(text, owner) for text in texts):
            return False  # an import binds the name to another module
        if not texts and occ.role != "member" and _family(file) in _NAMED_IMPORTS and not _star(scan, owner):
            return False  # a bare name from another file needs an import of it
    if occ.role == "member":
        if site.receiver in ("super", "self") and file.endswith(".rs"):
            return _named(_path_text(site.leaf.parent, scan), owner)  # super::name(..)
        return site.receiver in _visible(owner, aliases)
    return not _shadowed(site.leaf, needle, owner.decl)


def _type_name(text: bytes) -> str | None:
    """The first class name of a type, through wrappers: ``Arc<Mutex<Registry>>`` -> ``Registry``."""
    for name in re.findall(rb"[A-Z][\w$]*", text):
        if name not in _WRAPPERS:
            return name.decode("utf-8", "replace")
    return None


def _fields(scan: _Scan) -> dict[bytes, bytes]:
    """Field and property declarations of a file: name -> type text."""
    if scan.fields is None:
        scan.fields, stack = {}, [scan.tree.root_node]
        while stack:
            n = stack.pop()
            if _FIELD.match(n.type):
                name, kind = n.child_by_field_name("name"), n.child_by_field_name("type")
                if name is not None and kind is not None:
                    scan.fields.setdefault(name.text, kind.text)
            elif n.child_count:
                stack.extend(n.children)
    return scan.fields


def _receiver_type(site: _Site, scan: _Scan) -> str | None:
    """The declared class of a receiver: ``f: Fixture``, ``f = Fixture::new()``, ``Fixture f`` or a field.

    Look in the enclosing functions from the innermost out, then in field declarations.
    ``"?"`` means a declaration from a call of unknown type.
    """
    name = site.receiver
    if not name or name in _SELF:
        return None
    r = re.escape(name).encode()
    typed = rb"(?<![\w$.])" + r + rb"\s*:\s*([&*]?\s*(?:mut\s+|const\s+)?[A-Z][\w$<>:, &]*)"
    built = rb"(?<![\w$.])(?:let\s+(?:mut\s+)?|const\s+|var\s+)?" + r + rb"\s*:?=\s*(?:new\s+|&\s*(?:mut\s+)?)?([\w$]+(?:\s*(?:\.|::)\s*[\w$]+)*)\s*(::|\(|\{|<|\.)?"
    decl = rb"(?<![\w$.])([A-Z][\w$]*)(?:<[^<>;]*>)?\s*[*&]?\s+" + r + rb"\s*[=;,)]"
    node = site.leaf.parent
    while node is not None:
        if _FUNC.search(node.type) and not _CLASS.search(node.type):
            text = node.text
            m = re.search(typed, text)
            if m:
                return _type_name(m.group(1))
            m = re.search(built, text)
            if m and (m.group(2) or re.search(rb"\.|::", m.group(1))):
                kinds = [s for s in re.split(rb"\s*(?:\.|::)\s*", m.group(1)) if s[:1].isupper()]
                if scan.file.endswith(".go"):
                    kinds = [k[3:] for k in kinds[-1:] if re.match(rb"New[A-Z]", k)]  # Go: pkg.NewThing() makes a Thing, pkg.Make() is unknown
                if kinds:
                    return kinds[-1].decode("utf-8", "replace")  # Fixture::new(), vis.Setting(..)
                return "?"  # let tmp = tempfile::tempdir()
            m = re.search(decl, text)
            if m:
                return m.group(1).decode("utf-8", "replace")
        node = node.parent
    field_type = _fields(scan).get(name.encode())
    return _type_name(field_type) if field_type else None


def _anchored(owner: _Owner, file: str, site: _Site) -> bool:
    """An edge of the owner starts on the site line, or earlier in the same call chain."""
    lines = owner.anchors.get(file, ())
    if site.occ.line in lines:
        return True
    if site.occ.role != "member" or not lines:
        return False
    node = site.leaf.parent
    while node is not None and node.parent is not None and not re.search(r"statement|declaration|block|body|argument|lambda|closure", node.parent.type):
        node = node.parent
    first = node.start_point[0] + 1 if node is not None else site.occ.line
    return any(first <= line < site.occ.line for line in lines) and site.occ.line - first <= 10


def _caller_anchored(owner: _Owner, file: str, site: _Site) -> bool:
    """The outermost function around the site has an edge to the owner."""
    lines = owner.anchors.get(file, ())
    node, outer = site.leaf.parent, None
    while node is not None:
        if _FUNC.search(node.type) and not _CLASS.search(node.type):
            outer = node
        node = node.parent
    return outer is not None and any(outer.start_point[0] + 1 <= line <= outer.end_point[0] + 1 for line in lines)


def _family(file: str) -> str | None:
    return GRAMMARS.get(Path(file).suffix.lower(), (None,))[0]


def _star(scan: _Scan, owner: _Owner) -> bool:
    """The file imports every name of the owner's module: ``import *`` or a CommonJS ``require``."""
    for n in _imports(scan):
        text = _path_text(n, scan)
        if "*" in text and _named(text, owner):
            return True
    calls = re.findall(rb"require\(\s*['\"]([^'\"]+)", scan.data)
    return any(_named(c.decode("utf-8", "replace"), owner) for c in calls)


def _prefer(able: list[_Owner], file: str, site: _Site, scan: _Scan, aliases) -> list[_Owner]:
    """Break a tie between owners: definition, anchor, then the imported module."""
    occ = site.occ
    definition = [o for o in able if o.file == file and occ.line == _line(o.node.get("source_location"))]
    if definition:
        return definition
    local = [o for o in able if o.file == file and not o.method and not o.module]
    if occ.role == "use" and len(local) == 1:
        return local  # a bare name sees its own file first
    if occ.role == "member":
        fits = [o for o in able if o.method and _receiver_fits(site.receiver, o)]
        if len(fits) == 1:
            return fits
    anchored = [o for o in able if _anchored(o, file, site)]
    if len(anchored) == 1:
        return anchored
    if occ.role == "import":
        text = _import_text(site.leaf, scan)
        named = [o for o in able if not o.module and _named(text, o)]
        if len(named) == 1:
            return named
    imported = [o for o in able if not o.module and _visible(o, aliases)]
    if len(imported) == 1:
        return imported
    return able


def _owned(scans: dict[str, _Scan], owners: list[_Owner], name: str) -> tuple[list[Occurrence], list[Mention]]:
    """Split the scanned sites into sites of the target and other mentions.

    A file with an owned import of the name re-exports it, so imports of that file
    count too. Repeat until no new re-exporting file appears.
    """
    target = owners[0]
    target.modules = {target.file} | target.stars
    for _ in range(6):
        own, other = _split(scans, owners, name)
        facades = {o.file for o in own if o.role == "import"} - target.modules
        if not facades:
            break
        target.modules |= facades
    return own, other


def _split(scans: dict[str, _Scan], owners: list[_Owner], name: str) -> tuple[list[Occurrence], list[Mention]]:
    target = owners[0]
    needle = name.encode("utf-8")
    own, other = [], []
    target.unsure = []
    common = target.method and name in _COMMON
    for file, scan in scans.items():
        cache: dict[str, set[str]] = {}

        def aliases(def_file, scan=scan, cache=cache):
            if def_file not in cache:
                cache[def_file] = _aliases(scan, def_file)
            return cache[def_file]

        for site in scan.sites:
            occ = site.occ
            able = [o for o in owners if _can_own(o, file, site, scan, needle, aliases)]
            if target in able and len(able) > 1:
                able = _prefer(able, file, site, scan, aliases)
            if able == [target]:
                role = "definition" if file == target.file and occ.line == _line(target.node.get("source_location")) else occ.role
                own.append(Occurrence(occ.file, occ.line, occ.col, occ.start, occ.end, role, occ.text, occ.qualified))
            else:
                why = "another symbol" if able else "unconfirmed"
                other.append(Mention(file, occ.line, f"{name} ({why})"))
                if common and not able and occ.role == "member" and _anchored(target, file, site) and _receiver_type(site, scan) is None:
                    target.unsure.append(other[-1])
    own.sort(key=lambda o: (o.file, o.start))
    return own, other


def _text_mentions(file: str, data: bytes, name: str, covered: set[int]) -> list[Mention]:
    out = []
    for m in re.finditer(rb"(?<![\w$])" + re.escape(name.encode()) + rb"(?![\w$])", data):
        if m.start() in covered:
            continue
        line = data.count(b"\n", 0, m.start()) + 1
        text = data[data.rfind(b"\n", 0, m.start()) + 1 : data.find(b"\n", m.end()) if b"\n" in data[m.end():] else len(data)]
        out.append(Mention(file, line, text.decode("utf-8", "replace").strip()[:120]))
    return out


def _node(graph: dict, node_id: str) -> dict:
    for n in graph.get("nodes", ()):
        if n.get("id") == node_id:
            return n
    raise KeyError(f"No node {node_id!r} in the graph.")


def _clojure(node: dict) -> tuple[str, str] | None:
    meta = node.get("metadata") or {}
    if meta.get("language") == "clojure" and meta.get("kind") == "var" and meta.get("module") and meta.get("name"):
        return str(meta["module"]), str(meta["name"])
    return None


def _clojure_files(root: Path, graph: dict) -> list[Path]:
    files = {str(n.get("source_file") or "") for n in graph.get("nodes", ())}
    return sorted(_resolve(root, f) for f in files if f.lower().endswith(CLOJURE_SUFFIXES))


def _target(graph: dict, node_id: str) -> tuple[dict, str]:
    node = _node(graph, node_id)
    if _clojure(node):
        return node, _clojure(node)[1]
    file = str(node.get("source_file") or "")
    name = symbol_name(str(node.get("label", "")))
    if node.get("file_type") != "code" or not file or Path(file).name == node.get("label"):
        raise Unsupported(f"{node.get('label', node_id)} is not a code symbol. Name a function, method, class or var.")
    if Path(file).suffix.lower() not in GRAMMARS:
        raise Unsupported(f"No tree-sitter grammar for {Path(file).suffix} files.")
    if not _VALID.match(name):
        raise Unsupported(f"{node.get('label')} has no identifier name.")
    return node, name


def references(graph: dict, node_id: str, root) -> list[Occurrence]:
    """Every site of the node in the project ``root``, sorted by file and offset."""
    root = Path(root)
    node, name = _target(graph, node_id)
    var = _clojure(node)
    if var:
        from graphify import clojure_refactor

        return clojure_refactor.references(*var, _clojure_files(root, graph))
    scans, owners = _scan(root, graph, node, name)
    return _owned(scans, owners, name)[0]


def _double_conflicts(scans: dict[str, _Scan], target: _Owner, owned: set) -> list[str]:
    """Method definitions of the name in a class named like the target's class: test doubles."""
    if not target.method:
        return []
    names = {n.lower() for n in target.family | {target.class_name} if n}
    out = []
    for file, scan in scans.items():
        for site in scan.sites:
            if (file, site.occ.line) in owned:
                continue
            decl = _declaration(site.leaf)
            if decl is None or not _FUNC.search(decl.type):
                continue
            name = decl.child_by_field_name("name")
            if name is None or name.id != site.leaf.id:
                continue
            klass = _ancestor(decl, _CLASS)
            label = klass.child_by_field_name("name") if klass is not None else None
            if label is not None and label.text.decode("utf-8", "replace").lower() in names:
                out.append(f"`{label.text.decode()}` at {file}:{site.occ.line} also defines `{site.occ.text}`; a test double or duck type needs the same rename.")
    return out


def _override_conflicts(graph: dict, node: dict, name: str) -> list[str]:
    """A method that can override, or be overridden, needs the whole hierarchy renamed."""
    if not _is_method(node):
        return []
    links = list(_links(graph))
    byid = {n.get("id"): n for n in graph.get("nodes", ())}
    owner = next((l["source"] for l in links if l.get("relation") == "method" and l.get("target") == node["id"]), None)
    if owner is None:
        return []
    parents: dict[str, set[str]] = {}
    for l in links:
        if l.get("relation") in ("inherits", "implements", "extends"):
            parents.setdefault(l["source"], set()).add(l["target"])
    children: dict[str, set[str]] = {}
    for child, ps in parents.items():
        for p in ps:
            children.setdefault(p, set()).add(child)
    methods: dict[str, set[str]] = {}
    for l in links:
        if l.get("relation") == "method" and symbol_name(byid.get(l.get("target"), {}).get("label", "")) == name:
            methods.setdefault(l["source"], set()).add(l["target"])
    out = []
    family_labels = {str(byid.get(owner, {}).get("label", ""))}
    for cls, found in methods.items():
        if cls != owner and str(byid.get(cls, {}).get("label", "")) in family_labels:
            where = next(iter(found))
            spot = f"{byid.get(where, {}).get('source_file', '')}:{_line(byid.get(where, {}).get('source_location'))}"
            out.append(f"Another `{byid[cls]['label']}` at {spot} also defines `{name}`; a test double or duck type needs the same rename.")
    seen, stack = {owner}, [(owner, "up"), (owner, "down")]
    while stack:
        cls, way = stack.pop()
        for nxt in (parents if way == "up" else children).get(cls, ()):
            if nxt in seen:
                continue
            seen.add(nxt)
            label = byid.get(nxt, {}).get("label", nxt)
            if way == "up" and label in _EMPTY_BASES:
                continue
            if way == "up" and not byid.get(nxt, {}).get("source_file"):
                out.append(f"`{byid.get(owner, {}).get('label', owner)}` extends `{label}` from outside the project; `{name}` can override its method.")
                continue
            if methods.get(nxt):
                out.append(f"`{label}` also defines `{name}`; an override needs the same rename.")
            stack.append((nxt, way))
    return out


def _implements(target: _Owner) -> list[str]:
    """A method of a trait impl, or one marked as an override, belongs to a contract outside its class."""
    decl = target.decl
    if not target.method or decl is None:
        return []
    impl = _ancestor(decl, re.compile(r"^impl_item$"))
    trait = impl.child_by_field_name("trait") if impl is not None else None
    if trait is not None:
        return [f"`{target.node.get('label')}` implements trait `{trait.text.decode('utf-8', 'replace')}`; rename the trait method and every impl together."]
    name = decl.child_by_field_name("name")
    head = decl.text[: (name.start_byte if name is not None else decl.start_byte) - decl.start_byte]
    if re.search(rb"(?<![\w@])override\b|@Override", head):
        return [f"`{target.node.get('label')}` overrides a base method; rename the base method and every override together."]
    return []


def plan_rename(graph: dict, node_id: str, new: str, root) -> RenamePlan:
    """Plan the rename of the node to ``new`` in the project ``root``, without writes.

    A conflict makes the plan unsafe: an invalid name, no sites, or a file that
    already uses the new name, where the renamed symbol would collide.
    """
    root = Path(root)
    node, old = _target(graph, node_id)
    var = _clojure(node)
    if var:
        from graphify import clojure_refactor

        return clojure_refactor.plan_rename(*var, new, _clojure_files(root, graph))
    conflicts: list[str] = []
    if new == old or not _VALID.match(new or ""):
        conflicts.append(f"`{new}` is not a valid new name for `{old}`.")
    scans, owners = _scan(root, graph, node, old)
    own, others = _owned(scans, owners, old)
    edits = [Edit(o.file, o.line, o.col, o.start, o.end, old, new) for o in own]
    if not edits:
        conflicts.append(f"No sites of `{old}` found.")
    conflicts.extend(_override_conflicts(graph, node, old))
    conflicts.extend(_double_conflicts(scans, owners[0], {(o.file, o.line) for o in own}))
    conflicts.extend(_implements(owners[0]))
    if owners[0].unsure:
        spots = ", ".join(f"{m.file}:{m.line}" for m in owners[0].unsure[:5])
        conflicts.append(f"`{old}` is also a common library method; {len(owners[0].unsure)} linked calls on receivers of unknown type stay unconfirmed ({spots}). Check them and edit by hand.")
    needle = new.encode("utf-8")
    for file in sorted({e.file for e in edits}):
        if needle and any(True for _ in _leaves(scans[file].tree, needle)):
            conflicts.append(f"{file} already uses `{new}`.")
    mentions = list(others)
    for file, scan in sorted(scans.items()):
        covered = {s.occ.start for s in scan.sites}
        mentions.extend(_text_mentions(file, scan.data, old, covered))
    digests = {file: digest(scans[file].data) for file in {e.file for e in edits}}
    absolute = tuple(Edit(str(_resolve(root, e.file)), e.line, e.col, e.start, e.end, e.old, e.new) for e in edits)
    digests = {str(_resolve(root, f)): d for f, d in digests.items()}
    return RenamePlan(node["id"], old, new, absolute, tuple(conflicts), tuple(sorted(mentions, key=lambda m: (m.file, m.line))), digests)


def rewrite(plan: RenamePlan) -> dict[str, tuple[bytes, bytes]]:
    """The old and new bytes of each planned file, without writes.

    Raises ``StaleFile`` when a file changed after the plan.
    """
    by_file: dict[str, list[Edit]] = {}
    for e in plan.edits:
        by_file.setdefault(e.file, []).append(e)
    out: dict[str, tuple[bytes, bytes]] = {}
    for path in sorted(by_file):
        data = Path(path).read_bytes()
        if digest(data) != plan.digests.get(path):
            raise StaleFile(f"{path} changed after the rename plan. Plan the rename again.")
        new = data
        for e in sorted(by_file[path], key=lambda e: e.start, reverse=True):
            new = new[: e.start] + e.new.encode("utf-8") + new[e.end :]
        out[path] = (data, new)
    return out


def apply_rename(plan: RenamePlan) -> list[str]:
    """Write the edits of a conflict-free plan. Refuse a file that changed since the plan."""
    if plan.conflicts:
        raise ValueError("The rename plan has conflicts: " + " ".join(plan.conflicts))
    contents = rewrite(plan)
    for path, (_, new) in contents.items():
        Path(path).write_bytes(new)
    return sorted(contents)
