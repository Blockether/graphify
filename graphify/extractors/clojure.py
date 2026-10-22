"""Clojure extractor for .clj/.cljs/.cljc/.bb/.edn, backed by the Blockether
tree-sitter-clojure grammar (https://github.com/Blockether/tree-sitter-clojure).

Clojure resolves every symbol through its namespace, so this extractor records
the namespace map of each file (``ns`` aliases, ``:refer`` lists, ``:use``) and
resolves references exactly:

* names defined in the same file become ``calls`` / ``references`` edges here;
* qualified (``alias/name``, ``some.ns/name``) and referred names are emitted in
  the result's ``clojure`` block and bound across files by
  ``graphify.extract._resolve_clojure_references`` once the corpus is known.

Lexical bindings (fn params, ``let``/``loop``/``for``/``doseq`` bindings,
destructuring) shadow vars, so a parameter named like a function never yields a
false call edge.
"""
from __future__ import annotations

from pathlib import Path

from graphify.extractors.base import _file_stem, _make_id, _read_source_bytes

# Forms that bind names in a leading binding vector: [name init name init ...]
_LET_FORMS = frozenset({
    "let", "let*", "loop", "loop*", "when-let", "if-let", "when-some", "if-some",
    "when-first", "with-open", "with-local-vars",
    "for", "doseq", "dotimes", "with-out-str", "plet", "go-loop",
})
# Forms whose binding vector names EXISTING vars: [var value var value ...]
_REDEF_FORMS = frozenset({"binding", "with-redefs"})
# Forms that take a quoted symbol naming a var: (requiring-resolve 'my.ns/f)
_RESOLVE_FORMS = frozenset({"requiring-resolve", "resolve", "ns-resolve", "find-var"})
_FN_FORMS = frozenset({"fn", "fn*", "bound-fn"})
# Map destructuring keys whose value is a vector of names to bind. The `!` forms
# are the Clojure 1.13 required-key directives.
_MAP_KEY_DIRECTIVES = frozenset({"keys", "syms", "strs", "keys!", "syms!", "strs!"})
# Map destructuring keys whose value is itself a binding form (Clojure 1.13 adds
# `:select`, `:defaults`, `:all`, `:excess` and `:missing`).
_MAP_SYM_DIRECTIVES = frozenset({"as", "select", "defaults", "all", "excess", "missing"})

_FN_DEFINERS = frozenset({"defn", "defn-", "definline"})
_MACRO_DEFINERS = frozenset({"defmacro"})
_DATA_DEFINERS = frozenset({"def", "defonce"})
_TYPE_DEFINERS = frozenset({"defrecord", "deftype", "definterface"})
_TEST_DEFINERS = frozenset({
    "deftest", "deftest-", "defspec", "defdescribe", "defexpect",
})
# `def`-prefixed symbols that are not definitions.
_NOT_DEFINERS = frozenset({
    "default", "defaults", "default-value", "defer", "deferred", "deflate",
    "deref", "deref-future", "defined?",
})
# Spec registrations: (s/def ::name spec), (s/fdef fname ...)
_SPEC_DEFINERS = frozenset({"def", "fdef"})

_CJ_CHAR_MAP = {
    "=": "_eq", "<": "_lt", ">": "_gt", "?": "_p", "!": "_bang",
    "+": "_plus", "*": "_star", "/": "_slash", "%": "_pct", "&": "_amp",
    "'": "_q", ".": "_dot",
}

CLOJURE_SUFFIXES = frozenset({".clj", ".cljs", ".cljc", ".bb", ".edn"})


def _cj_id(*parts: str) -> str:
    return _make_id(*(''.join(_CJ_CHAR_MAP.get(c, c) for c in p) for p in parts))


def _ns_stub_id(ns: str) -> str:
    """Id of the sourceless stub for a required namespace (or a Java class)."""
    return _make_id("clojure_ns", ns.replace(".", "_dot_"))


def _lib_stub_id(lib: str) -> str:
    """Id of the sourceless stub for a deps.edn / project.clj library."""
    return _make_id("clojure_lib", lib.replace(".", "_dot_").replace("/", "_slash_"))


def extract_clojure(path: Path, positions: bool = False) -> dict:
    """Extract namespaces, vars, macros, protocols, records, multimethods,
    specs, tests, imports and calls from a Clojure source or EDN file.

    With ``positions``, the ``clojure`` block also lists every resolved symbol
    occurrence with its byte range, for exact references and renames."""
    try:
        import tree_sitter_clojure as tsclj
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree-sitter-clojure not installed"}

    try:
        language = Language(tsclj.language())
    except Exception as e:  # grammar present but unloadable (ABI mismatch)
        return {"nodes": [], "edges": [], "error": f"tree-sitter-clojure failed to load: {e}"}

    try:
        source = _read_source_bytes(path)
        root = Parser(language).parse(source).root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _make_id(_file_stem(path))
    str_path = str(path)
    suffix = path.suffix.lower()
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()

    # Namespace state
    current_ns: str | None = None
    ns_nid: str | None = None
    aliases: dict[str, str] = {}        # alias -> namespace
    refers: dict[str, str] = {}         # referred name -> namespace
    refer_all: list[str] = []           # namespaces with :refer :all / :use
    imports: dict[str, str] = {}        # simple class name -> fully qualified
    defs: dict[str, str] = {}           # var name -> node id (this file)
    bodies: list[tuple[str, list, frozenset]] = []  # (owner nid, forms, shadowed names)
    refs: list[dict] = []               # cross-file references for the corpus pass
    import_stubs: dict[str, str] = {}   # stub id -> namespace
    refer_names: dict[str, str] = {}    # :rename local name -> original var name
    occurrences: list[dict] = []        # positions mode: resolved symbol sites
    unresolved: set[str] = set()        # positions mode: unqualified names bound nowhere
    record_only = False                 # positions mode: inside a (comment ...) form

    def _text(node) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _line(node) -> int:
        return node.start_point[0] + 1

    def add_node(nid: str, label: str, line: int, file_type: str = "code") -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": file_type,
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    def add_edge(src: str, tgt: str, relation: str, line: int) -> None:
        key = (src, tgt, relation)
        if src == tgt or key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    def add_stub(nid: str, label: str) -> str:
        # Every stub is an import anchor (a namespace, Java class or library).
        # `type: module` makes the colliding-id pass merge the copies that
        # several files emit into one shared node (#1327), so the resolver can
        # point `imports` at the real namespace node.
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": "code",
                "type": "module",
                "source_file": "",
                "source_location": "",
                "origin_file": str_path,
            })
        return nid

    file_nid = _cj_id(stem)
    add_node(file_nid, path.name, 1)

    # ── tree helpers ────────────────────────────────────────────────────────
    def _values(node) -> list:
        """Form children (the grammar's `value` field), skipping comments,
        `#_` discards and delimiters."""
        out = []
        for i, child in enumerate(node.children):
            if node.field_name_for_child(i) == "value":
                out.append(child)
        return out

    def _root_forms(node) -> list:
        """Top-level forms of the `source` node, which has no `value` field."""
        return [c for c in node.children
                if c.is_named and c.type not in ("comment", "dis_expr")]

    def _sym(node) -> tuple[str | None, str] | None:
        """(namespace, name) of a sym_lit, ignoring its metadata."""
        if node is None or node.type != "sym_lit":
            return None
        ns_node = node.child_by_field_name("namespace")
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        return (_text(ns_node) if ns_node is not None else None, _text(name_node))

    def _sym_text(node) -> str | None:
        s = _sym(node)
        if s is None:
            return None
        return f"{s[0]}/{s[1]}" if s[0] else s[1]

    def _kwd_name(node) -> str | None:
        if node is None or node.type != "kwd_lit":
            return None
        name_node = node.child_by_field_name("name")
        return _text(name_node) if name_node is not None else None

    def _str_content(node) -> str:
        content = node.child_by_field_name("content")
        return _text(content) if content is not None else ""

    def _head(node) -> tuple[str | None, str] | None:
        vals = _values(node)
        return _sym(vals[0]) if vals else None

    def _owner() -> str:
        return ns_nid or file_nid

    def _binding_names(node, out: set[str]) -> None:
        """Every symbol bound by a destructuring form (vector, map or symbol)."""
        if node is None:
            return
        if node.type == "sym_lit":
            s = _sym(node)
            if s and not s[0] and s[1] != "&":
                out.add(s[1])
            return
        if node.type == "vec_lit":
            for child in _values(node):
                _binding_names(child, out)
            return
        if node.type == "map_lit":
            _map_binding_names(node, out)
            return
        if node.type in ("meta_lit", "old_meta_lit"):
            return

    def _map_binding_names(node, out: set[str]) -> None:
        """Symbols bound by a map destructuring form, including the Clojure 1.13
        directives (`:keys!`, `:syms!`, `:strs!`, `:select`, `:defaults`, ...)."""
        vals = _values(node)
        for i in range(0, len(vals) - 1, 2):
            key, val = vals[i], vals[i + 1]
            if key.type == "kwd_lit":
                kname = _kwd_name(key) or ""
                if kname in _MAP_KEY_DIRECTIVES:
                    for item in _values(val) if val.type == "vec_lit" else []:
                        if item.type == "sym_lit":
                            s = _sym(item)
                            if s:
                                out.add(s[1])
                        elif item.type == "kwd_lit":
                            n = _kwd_name(item)
                            if n:
                                out.add(n)
                        elif item.type == "str_lit":
                            out.add(_str_content(item))
                    continue
                if kname == "or":
                    continue
                if kname in _MAP_SYM_DIRECTIVES:
                    _binding_names(val, out)
                    continue
            _binding_names(key, out)

    def _walk_defaults(node, owner: str, shadow: frozenset) -> None:
        """Walk the `:or` default expressions of a destructuring form."""
        if node is None:
            return
        if node.type == "vec_lit":
            for child in _values(node):
                _walk_defaults(child, owner, shadow)
            return
        if node.type != "map_lit":
            return
        vals = _values(node)
        for i in range(0, len(vals) - 1, 2):
            key, val = vals[i], vals[i + 1]
            if key.type != "kwd_lit":
                _walk_defaults(key, owner, shadow)  # nested destructuring: {{:keys [a]} :b}
            elif _kwd_name(key) == "or" and val.type == "map_lit":
                for default in _values(val)[1::2]:
                    _walk_refs(default, owner, shadow)
    def _walk_binding_inits(vec, owner: str, shadow: frozenset) -> frozenset:
        """Walk the init expressions of a binding vector in order and return the
        scope after it. Each init sees the names bound before it."""
        vals = _values(vec)
        for i in range(0, len(vals) - 1, 2):
            target, init = vals[i], vals[i + 1]
            if target.type == "kwd_lit":  # for/doseq modifiers (:let/:when/:while)
                if _kwd_name(target) == "let" and init.type == "vec_lit":
                    shadow = _walk_binding_inits(init, owner, shadow)
                else:
                    _walk_refs(init, owner, shadow)
                continue
            _walk_refs(init, owner, shadow)
            bound: set[str] = set()
            _binding_names(target, bound)
            shadow = shadow | frozenset(bound)
            _walk_defaults(target, owner, shadow)
        return shadow

    # ── reference resolution ────────────────────────────────────────────────
    def _where(sym, shadow: frozenset) -> tuple[bool, list, str] | None:
        """Resolve a symbol to (defined in this file, candidate namespaces, var name)."""
        ns_part, name = sym
        if name.startswith(".") or name == "new" or name.isdigit():
            return None  # 1.12 method values (String/.length, Integer/new) and array classes
        if ns_part:
            target_ns = aliases.get(ns_part, ns_part)
            if current_ns and target_ns == current_ns and name in defs:
                return True, [current_ns], name
            if "." not in target_ns and target_ns not in aliases.values():
                return None  # Java static (Math/PI) or an unknown alias
            return False, [target_ns], name
        if name in shadow:
            return None
        if name in defs:
            return True, [current_ns], name
        if name in refers:
            return False, [refers[name]], refer_names.get(name, name)
        if refer_all:
            return False, list(refer_all), name
        unresolved.add(name)
        return None

    def _occur(node, nss: list, name: str, role: str, local: bool = False,
               shadow: frozenset = frozenset()) -> None:
        """Record the name range of a resolved symbol in positions mode."""
        if not positions or node is None or node.type != "sym_lit":
            return
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        occurrences.append({
            "ns": list(nss), "name": name, "text": _text(name_node),
            "start": name_node.start_byte, "end": name_node.end_byte,
            "line": name_node.start_point[0] + 1, "col": name_node.start_point[1] + 1,
            "role": "comment" if record_only else role,
            "qualified": node.child_by_field_name("namespace") is not None,
            "local": local, "shadow": sorted(shadow),
        })

    def _occur_sym(node, sym, role: str, name: str | None = None) -> None:
        """Record a site that names a var outside a body walk (defmethod, impls)."""
        found = _where(sym, frozenset()) if positions and sym else None
        if found:
            _occur(node, found[1], name or found[2], role, local=found[0])

    def _resolve_ref(owner: str, sym, relation: str, line: int, shadow: frozenset,
                     node=None) -> None:
        found = _where(sym, shadow)
        if found is None:
            return
        local, nss, name = found
        _occur(node, nss, name, "use", local=local, shadow=shadow)
        if record_only:
            return
        if local:
            add_edge(owner, defs[name], relation, line)
            return
        refs.append({"caller": owner, "ns": nss, "name": name,
                     "relation": relation, "line": line})

    def _walk_refs(node, owner: str, shadow: frozenset) -> None:
        t = node.type
        if t in ("quoting_lit", "comment", "dis_expr", "str_lit", "regex_lit",
                 "num_lit", "kwd_lit", "char_lit", "nil_lit", "bool_lit"):
            return
        if t in ("meta_lit", "old_meta_lit"):
            return
        if t == "sym_lit":
            s = _sym(node)
            if s:
                _resolve_ref(owner, s, "references", _line(node), shadow, node)
            return
        if t == "list_lit":
            vals = _values(node)
            if not vals:
                return
            head = _sym(vals[0])
            if head and not head[0]:
                hname = head[1]
                if hname == "comment" and hname not in defs:
                    _walk_comment(vals[1:], owner, shadow)
                    return
                if hname in ("quote",):
                    return
                if hname in _FN_FORMS and hname not in shadow:
                    _walk_fn(vals[1:], owner, shadow)
                    return
                if hname == "letfn" and hname not in shadow and len(vals) > 1:
                    bound = set()
                    for fdef in _values(vals[1]) if vals[1].type == "vec_lit" else []:
                        fv = _values(fdef) if fdef.type == "list_lit" else []
                        if fv and fv[0].type == "sym_lit":
                            bound.add(_sym(fv[0])[1])
                    inner = shadow | frozenset(bound)
                    for fdef in _values(vals[1]) if vals[1].type == "vec_lit" else []:
                        if fdef.type == "list_lit":
                            _walk_fn(_values(fdef)[1:], owner, inner)
                    for v in vals[2:]:
                        _walk_refs(v, owner, inner)
                    return
                if hname in _LET_FORMS and hname not in shadow and len(vals) > 1 \
                        and vals[1].type == "vec_lit":
                    inner = _walk_binding_inits(vals[1], owner, shadow)
                    for v in vals[2:]:
                        _walk_refs(v, owner, inner)
                    return
                if hname in _REDEF_FORMS and hname not in shadow and len(vals) > 1 \
                        and vals[1].type == "vec_lit":
                    for k, item in enumerate(_values(vals[1])):
                        target = _sym(item) if k % 2 == 0 and item.type == "sym_lit" else None
                        if target:
                            _resolve_ref(owner, target, "references", _line(item), shadow, item)
                        else:
                            _walk_refs(item, owner, shadow)
                    for v in vals[2:]:
                        _walk_refs(v, owner, shadow)
                    return
                if hname in _RESOLVE_FORMS and hname not in shadow:
                    for v in vals[1:]:
                        quoted = _values(v) if v.type == "quoting_lit" else []
                        named = _sym(quoted[0]) if quoted and quoted[0].type == "sym_lit" else None
                        if named:
                            # A quoted symbol ignores locals, so no shadowing applies.
                            _resolve_ref(owner, named, "references", _line(quoted[0]), frozenset(),
                                         quoted[0])
            if head:
                _resolve_ref(owner, head, "calls", _line(vals[0]), shadow, vals[0])
            elif vals[0].type == "var_quoting_lit":
                # (#'my.ns/f x) calls the var directly.
                for v in _values(vals[0]):
                    called = _sym(v) if v.type == "sym_lit" else None
                    if called:
                        _resolve_ref(owner, called, "calls", _line(v), shadow, v)
            else:
                # A computed head such as ((comp f g) x) still references code.
                _walk_refs(vals[0], owner, shadow)
            for v in vals[1:]:
                _walk_refs(v, owner, shadow)
            return
        if t == "tagged_or_ctor_lit":
            tag = _sym(node.child_by_field_name("tag"))
            if tag and not tag[0] and "." in tag[1]:
                # Record constructor literal: #my.ns.Rec{...}
                rec_ns, _, rec = tag[1].rpartition(".")
                if current_ns and rec_ns == current_ns and rec in defs:
                    add_edge(owner, defs[rec], "references", _line(node))
                else:
                    refs.append({"caller": owner, "ns": [rec_ns], "name": rec,
                                 "relation": "references", "line": _line(node)})
            for v in _values(node):
                _walk_refs(v, owner, shadow)
            return
        if t == "var_quoting_lit":
            for v in _values(node):
                if v.type == "sym_lit":
                    s = _sym(v)
                    if s:
                        _resolve_ref(owner, s, "references", _line(v), shadow, v)
            return
        for child in node.children:
            _walk_refs(child, owner, shadow)

    def _walk_comment(forms: list, owner: str, shadow: frozenset) -> None:
        """Record sites inside a rich (comment ...) form, without graph edges."""
        nonlocal record_only
        if not positions or record_only:
            return
        record_only = True
        try:
            for f in forms:
                _walk_refs(f, owner, shadow)
        finally:
            record_only = False

    def _walk_fn(rest: list, owner: str, shadow: frozenset) -> None:
        """Walk `[name?] [params] body...` or `[name?] ([params] body...)+`."""
        if rest and rest[0].type == "sym_lit":
            shadow = shadow | {_sym(rest[0])[1]}
            rest = rest[1:]
        if rest and rest[0].type == "vec_lit":
            _walk_arity(rest, owner, shadow)
            return
        for arity in rest:
            if arity.type == "list_lit":
                _walk_arity(_values(arity), owner, shadow)
            else:
                _walk_refs(arity, owner, shadow)

    def _walk_arity(vals: list, owner: str, shadow: frozenset) -> None:
        if not vals:
            return
        bound: set[str] = set()
        params = vals[0] if vals[0].type == "vec_lit" else None
        if params is not None:
            _binding_names(params, bound)
            vals = vals[1:]
        inner = shadow | frozenset(bound)
        _walk_defaults(params, owner, inner)
        for v in vals:
            _walk_refs(v, owner, inner)

    # ── namespace forms ─────────────────────────────────────────────────────
    def _flatten_conditionals(items: list) -> list:
        """Inline reader conditionals (#? / #?@) so every platform branch counts."""
        out = []
        for item in items:
            if item.type in ("read_cond_lit", "splicing_read_cond_lit"):
                branch = _values(item)
                for i in range(1, len(branch), 2):
                    form = branch[i]
                    if item.type == "splicing_read_cond_lit" and form.type == "vec_lit":
                        out.extend(_flatten_conditionals(_values(form)))
                    else:
                        out.extend(_flatten_conditionals([form]))
            elif item.type == "quoting_lit":
                out.extend(_flatten_conditionals(_values(item)))
            else:
                out.append(item)
        return out

    def _add_import(ns: str, line: int, label: str | None = None) -> None:
        stub = add_stub(_ns_stub_id(ns), label or ns)
        import_stubs[stub] = ns
        add_edge(file_nid, stub, "imports", line)

    def _libspec(spec, prefix: str | None, use: bool, line: int) -> None:
        if spec.type == "sym_lit":
            lib = _sym_text(spec)
            if lib:
                full = f"{prefix}.{lib}" if prefix else lib
                _add_import(full, line)
                if use:
                    refer_all.append(full)
            return
        if spec.type == "str_lit":  # ClojureScript npm module: "react"
            _add_import(_str_content(spec), line)
            return
        if spec.type == "kwd_lit":
            return
        if spec.type == "list_lit":  # prefix list: (clojure [set :as s] string)
            vals = _flatten_conditionals(_values(spec))
            if vals and vals[0].type == "sym_lit":
                pre = _sym_text(vals[0])
                full_pre = f"{prefix}.{pre}" if prefix else pre
                for sub in vals[1:]:
                    _libspec(sub, full_pre, use, line)
            return
        if spec.type != "vec_lit":
            return
        vals = _flatten_conditionals(_values(spec))
        if not vals:
            return
        first = vals[0]
        if first.type == "str_lit":
            lib = _str_content(first)
        elif first.type == "sym_lit":
            lib = _sym_text(first)
            if lib and prefix:
                lib = f"{prefix}.{lib}"
        else:
            return
        if not lib:
            return
        opts = vals[1:]
        # A vector of plain symbols after a prefix symbol is a prefix list too.
        if opts and all(o.type in ("sym_lit", "vec_lit") for o in opts) and first.type == "sym_lit":
            for sub in opts:
                _libspec(sub, lib, use, line)
            return
        _add_import(lib, line)
        if use and not any(_kwd_name(o) == "only" for o in opts):
            refer_all.append(lib)
        i = 0
        while i < len(opts):
            key = _kwd_name(opts[i])
            val = opts[i + 1] if i + 1 < len(opts) else None
            i += 2
            if key in ("as", "as-alias") and val is not None and val.type == "sym_lit":
                aliases[_sym(val)[1]] = lib
            elif key in ("refer", "only", "refer-macros") and val is not None:
                if val.type == "kwd_lit" and _kwd_name(val) == "all":
                    refer_all.append(lib)
                elif val.type == "vec_lit":
                    for r in _values(val):
                        s = _sym(r)
                        if s:
                            refers[s[1]] = lib
                            _occur(r, [lib], s[1], "refer")
            elif key == "rename" and val is not None and val.type == "map_lit":
                pairs = _values(val)
                for j in range(0, len(pairs) - 1, 2):
                    a, b = _sym(pairs[j]), _sym(pairs[j + 1])
                    if a and b:
                        refers[b[1]] = lib
                        refer_names[b[1]] = a[1]
                        _occur(pairs[j], [lib], a[1], "refer")
            elif key == "default" and val is not None and val.type == "sym_lit":
                aliases[_sym(val)[1]] = lib
            elif opts[i - 2].type != "kwd_lit":
                i -= 1  # unknown positional value; resync on the next keyword

    def _java_import(spec, line: int) -> None:
        if spec.type == "sym_lit":
            fq = _sym_text(spec)
            if fq:
                imports[fq.rsplit(".", 1)[-1]] = fq
                _add_import(fq, line)
            return
        if spec.type in ("list_lit", "vec_lit"):
            vals = _values(spec)
            if vals and vals[0].type == "sym_lit":
                pkg = _sym_text(vals[0])
                for cls in vals[1:]:
                    c = _sym_text(cls)
                    if c:
                        imports[c] = f"{pkg}.{c}"
                        _add_import(f"{pkg}.{c}", line)

    def _ns_clause(clause, line: int) -> None:
        vals = _flatten_conditionals(_values(clause))
        if not vals:
            return
        kind = _kwd_name(vals[0]) or (_sym(vals[0]) or (None, ""))[1]
        if kind in ("require", "require-macros", "use", "use-macros"):
            use = kind.startswith("use")
            for spec in vals[1:]:
                _libspec(spec, None, use, line)
        elif kind == "import":
            for spec in vals[1:]:
                _java_import(spec, line)

    def _docstring(node, owner: str) -> None:
        text = _str_content(node).strip()
        if text:
            doc_nid = _cj_id(owner, "rationale")
            add_node(doc_nid, text[:120], _line(node), file_type="rationale")
            add_edge(doc_nid, owner, "rationale_for", _line(node))

    def _handle_ns(node, vals: list) -> None:
        nonlocal current_ns, ns_nid
        if len(vals) < 2:
            return
        name = _sym_text(vals[1])
        if not name:
            return
        current_ns = name
        ns_nid = _cj_id(stem, name)
        add_node(ns_nid, name, _line(node))
        add_edge(file_nid, ns_nid, "contains", _line(node))
        for clause in vals[2:]:
            if clause.type == "str_lit":
                _docstring(clause, ns_nid)
            elif clause.type in ("list_lit", "vec_lit", "read_cond_lit", "splicing_read_cond_lit"):
                for c in _flatten_conditionals([clause]):
                    if c.type in ("list_lit", "vec_lit"):
                        _ns_clause(c, _line(c))

    # ── definitions ─────────────────────────────────────────────────────────
    def _contain(nid: str, line: int, relation: str = "contains") -> None:
        add_edge(_owner(), nid, relation, line)

    def _register(name: str, nid: str, label: str, line: int, name_node=None) -> None:
        add_node(nid, label, line)
        defs.setdefault(name, nid)
        _occur(name_node, [current_ns], name, "definition", local=True)
        _contain(nid, line)

    def _after_name(vals: list, idx: int) -> tuple[list, object | None]:
        """Skip an optional docstring and attr-map after the name at vals[idx]."""
        rest = vals[idx + 1:]
        doc = None
        if len(rest) > 1 and rest[0].type == "str_lit":
            doc, rest = rest[0], rest[1:]
        if len(rest) > 1 and rest[0].type == "map_lit":
            rest = rest[1:]
        return rest, doc

    def _handle_fn_def(node, vals: list, kind: str) -> None:
        name_sym = _sym(vals[1]) if len(vals) > 1 else None
        if not name_sym:
            return
        name = name_sym[1]
        nid = _cj_id(stem, name)
        if kind in _MACRO_DEFINERS:
            label = f"{name} (macro)"
        else:
            label = f"{name}()"
        _register(name, nid, label, _line(node), vals[1])
        rest, doc = _after_name(vals, 1)
        if doc is not None:
            _docstring(doc, nid)
        bodies.append((nid, ["fn", rest], frozenset()))

    def _handle_data_def(node, vals: list) -> None:
        name_sym = _sym(vals[1]) if len(vals) > 1 else None
        if not name_sym:
            return
        name = name_sym[1]
        nid = _cj_id(stem, name)
        _register(name, nid, name, _line(node), vals[1])
        rest = vals[2:]
        if len(rest) == 2 and rest[0].type == "str_lit":
            _docstring(rest[0], nid)
            rest = rest[1:]
        bodies.append((nid, rest, frozenset()))

    def _handle_defmulti(node, vals: list) -> None:
        name_sym = _sym(vals[1]) if len(vals) > 1 else None
        if not name_sym:
            return
        name = name_sym[1]
        nid = _cj_id(stem, name)
        _register(name, nid, f"{name} (multimethod)", _line(node), vals[1])
        rest, doc = _after_name(vals, 1)
        if doc is not None:
            _docstring(doc, nid)
        bodies.append((nid, rest, frozenset()))

    def _handle_defmethod(node, vals: list) -> None:
        multi = _sym(vals[1]) if len(vals) > 1 else None
        if not multi or len(vals) < 3:
            return
        dispatch = _text(vals[2])
        name = multi[1]
        nid = _cj_id(stem, name, "method", dispatch)
        add_node(nid, f"{name} {dispatch}"[:80], _line(node))
        _contain(nid, _line(node))
        _pending_impl.append((nid, multi, _line(node)))
        _occur_sym(vals[1], multi, "implementation")
        bodies.append((nid, ["fn", vals[3:]], frozenset()))

    def _handle_defprotocol(node, vals: list) -> None:
        name_sym = _sym(vals[1]) if len(vals) > 1 else None
        if not name_sym:
            return
        name = name_sym[1]
        nid = _cj_id(stem, name)
        _register(name, nid, name, _line(node), vals[1])
        rest = vals[2:]
        if rest and rest[0].type == "str_lit":
            _docstring(rest[0], nid)
        for sig in rest:
            if sig.type != "list_lit":
                continue
            sv = _values(sig)
            msym = _sym(sv[0]) if sv else None
            if not msym:
                continue
            mnid = _cj_id(stem, name, msym[1])
            add_node(mnid, f".{msym[1]}()", _line(sig))
            defs.setdefault(msym[1], mnid)
            _occur(sv[0], [current_ns], msym[1], "definition", local=True)
            add_edge(nid, mnid, "method", _line(sig))
            for d in sv[1:]:
                if d.type == "str_lit":
                    _docstring(d, mnid)

    def _impl_methods(type_nid: str, type_name: str, items: list, shadow: frozenset) -> None:
        """Protocol/interface sections: `Proto (m [this] ...) Other (n [x] ...)`."""
        proto = None
        for item in items:
            if item.type == "sym_lit":
                proto = _sym(item)
                _pending_impl.append((type_nid, proto, _line(item)))
            elif item.type == "list_lit":
                iv = _values(item)
                msym = _sym(iv[0]) if iv else None
                if not msym:
                    continue
                mnid = _cj_id(stem, type_name, msym[1])
                add_node(mnid, f".{msym[1]}()", _line(item))
                add_edge(type_nid, mnid, "method", _line(item))
                if proto is not None:
                    pns = proto[0]
                    if pns is None and proto[1] not in defs:
                        pns = refers.get(proto[1])
                    _pending_impl.append((mnid, (pns, msym[1]), _line(item)))
                    _occur_sym(iv[0], proto, "implementation", msym[1])
                bodies.append((mnid, ["fn", iv[1:]], shadow))

    def _handle_type_def(node, vals: list, kind: str) -> None:
        name_sym = _sym(vals[1]) if len(vals) > 1 else None
        if not name_sym:
            return
        name = name_sym[1]
        nid = _cj_id(stem, name)
        _register(name, nid, name, _line(node), vals[1])
        fields: set[str] = set()
        rest = vals[2:]
        if rest and rest[0].type == "vec_lit":
            _binding_names(rest[0], fields)
            rest = rest[1:]
        if kind == "defrecord":
            defs.setdefault(f"->{name}", nid)
            defs.setdefault(f"map->{name}", nid)
        elif kind == "deftype":
            defs.setdefault(f"->{name}", nid)
        _impl_methods(nid, name, rest, frozenset(fields))

    def _handle_extend(node, vals: list, kind: str) -> None:
        """(extend-protocol P T1 (m ..) T2 ..) and (extend-type T P1 (m ..) P2 ..)."""
        if len(vals) < 2:
            return
        if kind == "extend-type":
            tsym = _sym(vals[1])
            if not tsym:
                return
            tnid = defs.get(tsym[1]) if not tsym[0] else None
            owner = tnid or _owner()
            _impl_methods(owner, tsym[1], vals[2:], frozenset())
            return
        proto = _sym(vals[1])
        if not proto:
            return
        current_type = None
        for item in vals[2:]:
            if item.type in ("sym_lit", "nil_lit"):
                current_type = _text(item)
                local = defs.get(current_type)
                if local:
                    _pending_impl.append((local, proto, _line(item)))
            elif item.type == "list_lit" and current_type is not None:
                iv = _values(item)
                msym = _sym(iv[0]) if iv else None
                if not msym:
                    continue
                parent = defs.get(current_type) or _owner()
                mnid = _cj_id(stem, current_type, msym[1])
                add_node(mnid, f".{msym[1]}()", _line(item))
                add_edge(parent, mnid, "method", _line(item))
                _pending_impl.append((mnid, (proto[0], msym[1]), _line(item)))
                _occur_sym(iv[0], proto, "implementation", msym[1])
                bodies.append((mnid, ["fn", iv[1:]], frozenset()))

    def _handle_spec(node, vals: list, kind: str) -> None:
        if len(vals) < 2:
            return
        target = vals[1]
        if target.type == "kwd_lit":
            label = _text(target)
        elif target.type == "sym_lit":
            label = _sym_text(target) or ""
        else:
            return
        if not label:
            return
        nid = _cj_id(stem, "spec", label)
        add_node(nid, f"{label} (spec)", _line(node))
        _contain(nid, _line(node))
        if kind == "fdef" and target.type == "sym_lit":
            _pending_impl.append((nid, _sym(target), _line(node)))
        bodies.append((nid, vals[2:], frozenset()))

    def _handle_custom_def(node, vals: list, kind: str) -> None:
        name_sym = _sym(vals[1]) if len(vals) > 1 else None
        if not name_sym:
            return
        name = name_sym[1]
        nid = _cj_id(stem, name)
        rest, doc = _after_name(vals, 1)
        if kind in _TEST_DEFINERS:
            label = f"{name} (test)"
        elif rest and rest[0].type == "vec_lit" or kind.endswith("fn"):
            label = f"{name}()"
        else:
            label = name
        _register(name, nid, label, _line(node), vals[1])
        if doc is not None:
            _docstring(doc, nid)
        if rest and rest[0].type == "vec_lit":
            bodies.append((nid, ["fn", rest], frozenset()))
        else:
            bodies.append((nid, rest, frozenset()))

    def _handle_defproject(node, vals: list) -> None:
        """Leiningen project.clj: dependencies become `imports` of library stubs."""
        name = _sym_text(vals[1]) if len(vals) > 1 else None
        if not name:
            return
        nid = _cj_id(stem, name)
        add_node(nid, name, _line(node))
        add_edge(file_nid, nid, "contains", _line(node))
        opts = vals[3:]
        for i in range(0, len(opts) - 1, 2):
            if _kwd_name(opts[i]) in ("dependencies", "plugins", "managed-dependencies"):
                _deps_vector(opts[i + 1])
            elif _kwd_name(opts[i]) == "profiles":
                _scan_edn_deps(opts[i + 1])

    def _deps_vector(vec) -> None:
        if vec.type != "vec_lit":
            return
        for dep in _values(vec):
            dv = _values(dep) if dep.type == "vec_lit" else []
            lib = _sym_text(dv[0]) if dv else None
            if lib:
                stub = add_stub(_lib_stub_id(lib), lib)
                add_edge(file_nid, stub, "imports", _line(dep))

    def _scan_edn_deps(node) -> None:
        """deps.edn/bb.edn: keys of every :deps / :extra-deps / :replace-deps map."""
        if node.type == "map_lit":
            vals = _values(node)
            for i in range(0, len(vals) - 1, 2):
                key = _kwd_name(vals[i])
                val = vals[i + 1]
                if key in ("deps", "extra-deps", "replace-deps", "override-deps", "default-deps") \
                        and val.type == "map_lit":
                    dvals = _values(val)
                    for j in range(0, len(dvals), 2):
                        lib = _sym_text(dvals[j])
                        if lib:
                            stub = add_stub(_lib_stub_id(lib), lib)
                            add_edge(file_nid, stub, "imports", _line(dvals[j]))
                elif key in ("dependencies", "plugins"):
                    _deps_vector(val)
                else:
                    _scan_edn_deps(val)
        elif node.type == "vec_lit":
            for v in _values(node):
                _scan_edn_deps(v)

    _pending_impl: list = []

    def _process(form) -> None:
        """Dispatch one top-level (or wrapper-nested) form."""
        nonlocal current_ns, ns_nid
        if form.type in ("read_cond_lit", "splicing_read_cond_lit"):
            for f in _flatten_conditionals([form]):
                _process(f)
            return
        if form.type != "list_lit":
            if form.type not in ("comment", "dis_expr"):
                _toplevel.append(form)
            return
        vals = _values(form)
        head = _sym(vals[0]) if vals else None
        if not head:
            _toplevel.append(form)
            return
        hns, h = head
        if hns:
            if h in _SPEC_DEFINERS and (aliases.get(hns, hns) in ("clojure.spec.alpha", "cljs.spec.alpha", "clojure.spec", "s", "spec")):
                _handle_spec(form, vals, h)
                return
            if h.startswith("def") and h not in _NOT_DEFINERS:
                _handle_custom_def(form, vals, h)
                return
            _toplevel.append(form)
            return
        if h == "ns":
            _handle_ns(form, vals)
        elif h == "in-ns":
            if len(vals) > 1 and vals[1].type == "quoting_lit":
                inner = _values(vals[1])
                name = _sym_text(inner[0]) if inner else None
                if name:
                    current_ns = name
                    ns_nid = _cj_id(stem, name)
                    add_node(ns_nid, name, _line(form))
                    add_edge(file_nid, ns_nid, "contains", _line(form))
        elif h in ("require", "use"):
            for spec in _flatten_conditionals(vals[1:]):
                _libspec(spec, None, h == "use", _line(form))
        elif h == "import":
            for spec in _flatten_conditionals(vals[1:]):
                _java_import(spec, _line(form))
        elif h == "comment":
            _comments.append(vals[1:])
        elif h in _FN_DEFINERS or h in _MACRO_DEFINERS:
            _handle_fn_def(form, vals, h)
        elif h in _DATA_DEFINERS:
            _handle_data_def(form, vals)
        elif h == "defmulti":
            _handle_defmulti(form, vals)
        elif h == "defmethod":
            _handle_defmethod(form, vals)
        elif h == "defprotocol":
            _handle_defprotocol(form, vals)
        elif h in _TYPE_DEFINERS:
            _handle_type_def(form, vals, h)
        elif h in ("extend-protocol", "extend-type"):
            _handle_extend(form, vals, h)
        elif h == "defproject":
            _handle_defproject(form, vals)
        elif h == "declare":
            for v in vals[1:]:
                s = _sym(v)
                if s and not s[0]:
                    _occur(v, [current_ns], s[1], "declaration", local=True)
        elif h.startswith("def") and h not in _NOT_DEFINERS:
            _handle_custom_def(form, vals, h)
        elif any(v.type in ("list_lit", "read_cond_lit") and _contains_def(v) for v in vals[1:]):
            # Wrapper macro around definitions: (do ...), (when-not ...), (compile-if ...)
            _toplevel.append(vals[0])
            for v in vals[1:]:
                _process(v)
        else:
            _toplevel.append(form)

    def _contains_def(node) -> bool:
        if node.type == "read_cond_lit":
            return any(_contains_def(v) for v in _values(node))
        if node.type != "list_lit":
            return False
        vals = _values(node)
        head = _sym(vals[0]) if vals else None
        if head and not head[0] and head[1].startswith("def") and head[1] not in _NOT_DEFINERS:
            return True
        return any(_contains_def(v) for v in vals[1:])

    _toplevel: list = []
    _comments: list = []

    if suffix == ".edn":
        for form in _root_forms(root):
            _scan_edn_deps(form)
    else:
        for form in _root_forms(root):
            _process(form)

        # Bodies are walked after every definition is known, so a call to a
        # function defined later in the file (after a `declare`) still binds.
        for owner, forms, shadow in bodies:
            if forms and forms[0] == "fn":
                _walk_fn(forms[1], owner, shadow)
            else:
                for f in forms:
                    _walk_refs(f, owner, shadow)
        for form in _toplevel:
            _walk_refs(form, _owner(), frozenset())
        for forms in _comments:
            _walk_comment(forms, _owner(), frozenset())

        # Implementations: defmethod -> defmulti, record -> protocol, impl -> protocol method.
        for impl_nid, target, line in _pending_impl:
            if target is None:
                continue
            tns, tname = target
            if not tns and tname in defs:
                add_edge(impl_nid, defs[tname], "implements", line)
            elif tns:
                target_ns = aliases.get(tns, tns)
                if current_ns and target_ns == current_ns and tname in defs:
                    add_edge(impl_nid, defs[tname], "implements", line)
                else:
                    refs.append({"caller": impl_nid, "ns": [target_ns], "name": tname,
                                 "relation": "implements", "line": line})
            elif tname in refers:
                refs.append({"caller": impl_nid, "ns": [refers[tname]], "name": tname,
                             "relation": "implements", "line": line})
            elif refer_all:
                refs.append({"caller": impl_nid, "ns": list(refer_all), "name": tname,
                             "relation": "implements", "line": line})

    if current_ns:
        def_names: dict[str, str] = {}
        for name, nid in defs.items():
            def_names.setdefault(nid, name)  # a record's own name, not its ->Rec factory
        for n in nodes:
            if n["id"] == ns_nid:
                n["metadata"] = {"language": "clojure", "kind": "namespace", "module": current_ns}
            elif n["id"] in def_names and n.get("source_file"):
                n["metadata"] = {"language": "clojure", "kind": "var", "module": current_ns,
                                 "name": def_names[n["id"]]}
    clean_edges = [e for e in edges if e["source"] in seen_ids and e["target"] in seen_ids]
    result: dict = {"nodes": nodes, "edges": clean_edges}
    result["clojure"] = {
        "ns": current_ns,
        "ns_nid": ns_nid,
        "file_nid": file_nid,
        "defs": sorted([name, nid] for name, nid in defs.items()),
        "refs": refs,
        "import_stubs": sorted([nid, ns] for nid, ns in import_stubs.items()),
    }
    if positions:
        primary: dict[str, str] = {}
        for name, nid in defs.items():
            primary.setdefault(nid, name)
        result["clojure"]["occurrences"] = occurrences
        result["clojure"]["unresolved"] = sorted(unresolved)
        result["clojure"]["vars"] = {name: primary[nid] for name, nid in defs.items()}
    return result


def remap_clojure_ids(per_file: list, mapping: dict) -> None:
    """Rewrite the node ids in each result's ``clojure`` block through an id remap.

    ``extract()`` makes node ids relative to the scan root after extraction. The
    resolver reads ids from this block, so it must see the same final ids.
    """
    if not mapping:
        return
    for result in per_file:
        info = result.get("clojure") if isinstance(result, dict) else None
        if not info:
            continue
        for key in ("ns_nid", "file_nid"):
            if info.get(key) in mapping:
                info[key] = mapping[info[key]]
        info["defs"] = [[name, mapping.get(nid, nid)] for name, nid in info.get("defs", [])]
        info["import_stubs"] = [[mapping.get(nid, nid), ns]
                                for nid, ns in info.get("import_stubs", [])]
        for ref in info.get("refs", []):
            if ref.get("caller") in mapping:
                ref["caller"] = mapping[ref["caller"]]

def resolve_clojure_references(
    per_file: list[dict],
    paths: list[Path],
    all_nodes: list[dict],
    all_edges: list[dict],
    context_nodes: list[dict] | None = None,
) -> None:
    """Bind the cross-file references that ``extract_clojure`` recorded.

    Namespaces are global names in Clojure, so a qualified or referred var
    resolves exactly: ``(str/join ...)`` with ``[clojure.string :as str]`` is
    ``clojure.string/join`` wherever that namespace is defined in the corpus.
    A namespace defined by several files (``foo.clj`` + ``foo.cljs``) prefers
    the caller's own suffix, then ``.cljc``. ``imports`` edges to required
    namespaces are repointed from their sourceless stub onto the real ``ns``
    node; stubs that end up unreferenced are dropped. References to namespaces
    outside the corpus (libraries, ``clojure.core``) produce no edge.
    ``context_nodes`` are the unchanged files' nodes of an incremental rebuild.
    Their ``metadata`` (language ``clojure``, ``module``, ``name``) makes their
    namespaces resolvable without a new parse.
    Must run after id disambiguation, so node ids are final.
    """
    node_by_id = {n.get("id"): n for n in all_nodes}
    fresh_files = {n.get("source_file") for n in all_nodes if n.get("source_file")}
    ctx_infos: dict[tuple[str, str], dict] = {}
    for n in context_nodes or []:
        meta = n.get("metadata")
        sf = n.get("source_file")
        if not isinstance(meta, dict) or meta.get("language") != "clojure" \
                or not meta.get("module") or not sf or sf in fresh_files:
            continue
        info = ctx_infos.setdefault((meta["module"], sf), {
            "ns": meta["module"], "ns_nid": None, "defs": [], "refs": [], "import_stubs": [],
        })
        if meta.get("kind") == "namespace":
            info["ns_nid"] = n.get("id")
        elif meta.get("name"):
            info["defs"].append([meta["name"], n.get("id")])
        node_by_id.setdefault(n.get("id"), n)
    by_ns: dict[str, list[tuple[str, dict]]] = {}
    infos: list[tuple[str, dict]] = []
    for result, path in zip(per_file, paths):
        info = result.get("clojure") if isinstance(result, dict) else None
        if not info:
            continue
        sfx = Path(str(path)).suffix.lower()
        infos.append((sfx, info))
        if info.get("ns"):
            by_ns.setdefault(info["ns"], []).append((sfx, info))
    for (ns, sf), info in ctx_infos.items():
        by_ns.setdefault(ns, []).append((Path(sf).suffix.lower(), info))
    if not infos:
        return

    def _candidates(ns: str, sfx: str) -> list[dict]:
        cands = by_ns.get(ns, [])
        return [i for _, i in sorted(cands, key=lambda c: (c[0] != sfx, c[0] != ".cljc"))]

    def _lookup(ns: str, name: str, sfx: str) -> str | None:
        for info in _candidates(ns, sfx):
            for def_name, nid in info.get("defs", []):
                if def_name == name and nid in node_by_id:
                    return nid
        return None

    existing = {(e.get("source"), e.get("target"), e.get("relation")) for e in all_edges}
    for sfx, info in infos:
        for ref in info.get("refs", []):
            caller = ref.get("caller")
            caller_node = node_by_id.get(caller)
            if caller_node is None:
                continue
            target = None
            for ns in ref.get("ns", []):
                target = _lookup(ns, ref.get("name", ""), sfx)
                if target:
                    break
            relation = ref.get("relation", "calls")
            if not target or target == caller or (caller, target, relation) in existing:
                continue
            existing.add((caller, target, relation))
            all_edges.append({
                "source": caller,
                "target": target,
                "relation": relation,
                "confidence": "EXTRACTED",
                "source_file": caller_node.get("source_file", ""),
                "source_location": f"L{ref.get('line', 1)}",
                "weight": 1.0,
            })

    # Repoint `imports` stubs of in-corpus namespaces onto their ns node.
    remap: dict[str, str] = {}
    for sfx, info in infos:
        for stub_id, ns in info.get("import_stubs", []):
            if stub_id in remap:
                continue
            for cand in _candidates(ns, sfx):
                ns_nid = cand.get("ns_nid")
                if ns_nid in node_by_id:
                    remap[stub_id] = ns_nid
                    break
    if not remap:
        return
    seen_pairs: set[tuple[str, str, str]] = set()
    kept: list[dict] = []
    for e in all_edges:
        if e.get("target") in remap and e.get("relation") == "imports":
            e["target"] = remap[e["target"]]
        key = (e.get("source"), e.get("target"), e.get("relation"))
        if e.get("relation") == "imports":
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
        kept.append(e)
    all_edges[:] = kept
    referenced = {e.get("source") for e in all_edges} | {e.get("target") for e in all_edges}
    all_nodes[:] = [
        n for n in all_nodes
        if not (n.get("id") in remap and not n.get("source_file") and n.get("id") not in referenced)
    ]
