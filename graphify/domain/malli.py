"""Convert Malli schema forms to JSON Schema (draft 2020-12).

The converter works on :class:`graphify.domain.edn.Form` values, so it reads
schemas straight from source code. A ``resolve`` callback turns a symbol into
the form that its ``def`` holds; the converter puts each named schema in
``$defs`` once and refers to it with ``$ref``.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from graphify.domain import edn
from graphify.domain.edn import Form

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

# resolve(symbol_form, context) -> (qualified_name, value_form, value_context) or None
Resolver = Callable[[Form, Any], "tuple[str, Form, Any] | None"]

_SIMPLE: dict[str, dict] = {
    "string": {"type": "string"},
    "int": {"type": "integer"},
    "integer": {"type": "integer"},
    "long": {"type": "integer"},
    "double": {"type": "number"},
    "float": {"type": "number"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "uuid": {"type": "string", "format": "uuid"},
    "keyword": {"type": "string", "x-malli": "keyword"},
    "qualified-keyword": {"type": "string", "x-malli": "qualified-keyword"},
    "symbol": {"type": "string", "x-malli": "symbol"},
    "qualified-symbol": {"type": "string", "x-malli": "qualified-symbol"},
    "nil": {"type": "null"},
    "any": {},
    "some": {"not": {"type": "null"}},
    "inst": {"type": "string", "format": "date-time"},
    "pos-int": {"type": "integer", "minimum": 1},
    "nat-int": {"type": "integer", "minimum": 0},
    "neg-int": {"type": "integer", "maximum": -1},
    "pos": {"type": "number", "exclusiveMinimum": 0},
    "neg": {"type": "number", "exclusiveMaximum": 0},
    "map": {"type": "object"},
    "vector": {"type": "array"},
    "sequential": {"type": "array"},
    "set": {"type": "array", "uniqueItems": True},
    "seqable": {"type": "array"},
    "decimal": {"type": "number"},
    "ratio": {"type": "number"},
    "local-date": {"type": "string", "format": "date"},
    "local-date-time": {"type": "string", "format": "date-time"},
    "offset-date-time": {"type": "string", "format": "date-time"},
    "instant": {"type": "string", "format": "date-time"},
}

_PREDICATES = {
    "string?": "string", "int?": "int", "integer?": "int", "pos-int?": "pos-int", "nat-int?": "nat-int",
    "neg-int?": "neg-int", "number?": "number", "double?": "double", "float?": "double", "boolean?": "boolean",
    "keyword?": "keyword", "qualified-keyword?": "qualified-keyword", "simple-keyword?": "keyword",
    "symbol?": "symbol", "uuid?": "uuid", "inst?": "inst", "map?": "map", "vector?": "vector",
    "sequential?": "sequential", "set?": "set", "nil?": "nil", "any?": "any", "some?": "some",
    "pos?": "pos", "neg?": "neg", "decimal?": "decimal", "ratio?": "ratio", "seqable?": "seqable",
    "coll?": "sequential", "true?": "boolean", "false?": "boolean", "zero?": "int",
}

_SCHEMA_CONSTRUCTORS = {"schema", "-schema", "form", "deref", "into-schema"}


def edn_value(form: Form | None) -> Any:
    """Convert a literal form to a JSON value. Keywords become their names."""
    if form is None:
        return None
    k = form.kind
    if k in (edn.KEYWORD, edn.SYMBOL):
        return str(form.value)
    if k in (edn.STRING, edn.BOOL):
        return form.value
    if k == edn.NIL:
        return None
    if k == edn.NUMBER:
        text = str(form.value).rstrip("MN")
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return text
    if k in (edn.VECTOR, edn.LIST, edn.SET):
        return [edn_value(i) for i in form.items]
    if k == edn.MAP:
        return {str(edn_value(a)): edn_value(b) for a, b in form.pairs()}
    if k == edn.REGEX:
        return form.value
    return form.text


def merge_objects(*schemas: dict) -> dict:
    """Merge object schemas: properties and required keys add up.

    A schema that is not a plain object goes into ``allOf`` instead.
    """
    props: dict[str, Any] = {}
    required: list[str] = []
    rest: list[dict] = []
    extra: dict[str, Any] = {}
    for s in schemas:
        if not s:
            continue
        if s.get("type") == "object" and set(s) <= {"type", "properties", "required", "additionalProperties", "description", "title"}:
            props.update(s.get("properties", {}))
            for r in s.get("required", []):
                if r not in required:
                    required.append(r)
            for k in ("additionalProperties", "description", "title"):
                if k in s:
                    extra[k] = s[k]
        else:
            rest.append(s)
    merged: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        merged["required"] = required
    merged.update(extra)
    if rest:
        return {"allOf": [merged, *rest]}
    return merged


class MalliConverter:
    """Convert Malli forms to JSON Schema and collect named ``$defs``."""

    def __init__(self, resolve: Resolver | None = None, registry: dict[str, tuple[Form, Any]] | None = None):
        self.resolve = resolve
        self.registry = dict(registry or {})
        self.defs: dict[str, dict] = {}
        self.warnings: list[str] = []
        self._active: set[str] = set()

    # -- public ------------------------------------------------------------
    def document(self, form: Form | None, context: Any = None, title: str | None = None) -> dict:
        """Return a full JSON Schema document for ``form``, with ``$defs``."""
        self.defs = {}
        body = self.convert(form, context)
        return self.wrap(body, title)

    def wrap(self, body: dict, title: str | None = None) -> dict:
        doc: dict[str, Any] = {"$schema": JSON_SCHEMA_DIALECT}
        if title:
            doc["title"] = title
        doc.update(body)
        used = self._used_defs(body)
        if used:
            doc["$defs"] = {k: self.defs[k] for k in sorted(used)}
        return doc

    def convert(self, form: Form | None, context: Any = None) -> dict:
        if form is None:
            return {}
        try:
            return self._convert(form, context)
        except RecursionError:  # pragma: no cover - deep self-reference
            self.warnings.append(f"schema too deep: {form.text[:60]}")
            return {}

    # -- internals -----------------------------------------------------------
    def _used_defs(self, body: Any) -> set[str]:
        found: set[str] = set()
        stack = [body]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                ref = cur.get("$ref")
                if isinstance(ref, str) and ref.startswith("#/$defs/"):
                    name = ref[len("#/$defs/"):]
                    if name not in found and name in self.defs:
                        found.add(name)
                        stack.append(self.defs[name])
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
        return found

    @staticmethod
    def _def_name(qualified: str) -> str:
        return qualified.replace("/", ".")

    def _named(self, name: str, form: Form, context: Any) -> dict:
        key = self._def_name(name)
        if key not in self.defs and key not in self._active:
            self._active.add(key)
            self.defs[key] = {}
            try:
                self.defs[key] = self._convert(form, context)
            finally:
                self._active.discard(key)
        return {"$ref": f"#/$defs/{key}"}

    def _symbol(self, form: Form, context: Any) -> dict:
        name = str(form.value)
        bare = name.rsplit("/", 1)[-1]
        if bare in _PREDICATES:
            return copy.deepcopy(_SIMPLE[_PREDICATES[bare]])
        resolved = self.resolve(form, context) if self.resolve else None
        if resolved is None:
            self.warnings.append(f"unresolved schema symbol `{name}` (line {form.line})")
            return {"$comment": f"unresolved Malli schema `{name}`"}
        qualified, value, value_context = resolved
        return self._named(qualified, value, value_context)

    def _keyword(self, form: Form, context: Any, props: dict | None = None) -> dict:
        name = str(form.value)
        if name in _SIMPLE:
            return self._with_props(copy.deepcopy(_SIMPLE[name]), name, props)
        if name in self.registry:
            value, value_context = self.registry[name]
            return self._named(name, value, value_context)
        self.warnings.append(f"unknown Malli type `:{name}` (line {form.line})")
        return {"$comment": f"unknown Malli type :{name}"}

    @staticmethod
    def _with_props(schema: dict, type_name: str, props: dict | None) -> dict:
        if not props:
            return schema
        t = schema.get("type")
        mn, mx = props.get("min"), props.get("max")
        if t == "string":
            if mn is not None:
                schema["minLength"] = mn
            if mx is not None:
                schema["maxLength"] = mx
        elif t in ("integer", "number"):
            if mn is not None:
                schema["minimum"] = mn
            if mx is not None:
                schema["maximum"] = mx
        elif t == "array":
            if mn is not None:
                schema["minItems"] = mn
            if mx is not None:
                schema["maxItems"] = mx
        elif t == "object":
            if mn is not None:
                schema["minProperties"] = mn
            if mx is not None:
                schema["maxProperties"] = mx
        for key in ("title", "description"):
            if isinstance(props.get(key), str):
                schema[key] = props[key]
        if "default" in props:
            schema["default"] = props["default"]
        for k, v in props.items():
            if k.startswith("json-schema/"):
                schema[k.split("/", 1)[1]] = v
        return schema

    def _props(self, items: list[Form]) -> tuple[dict, dict, list[Form]]:
        """Split ``[props? children...]``; return (json props, raw props form map, children)."""
        if items and items[0].kind == edn.MAP:
            raw = items[0]
            return edn_value(raw) or {}, {str(k.value): v for k, v in raw.pairs() if k.kind == edn.KEYWORD}, items[1:]
        return {}, {}, items

    def _convert(self, form: Form, context: Any) -> dict:
        k = form.kind
        if k == edn.KEYWORD:
            return self._keyword(form, context)
        if k == edn.SYMBOL:
            return self._symbol(form, context)
        if k == edn.VECTOR:
            return self._vector(form, context)
        if k == edn.LIST:
            return self._call(form, context)
        if k == edn.QUOTE and form.items:
            return self._convert(form.items[0], context)
        if k == edn.REGEX:
            return {"type": "string", "pattern": form.value}
        if k in (edn.STRING, edn.NUMBER, edn.BOOL, edn.NIL):
            return {"const": edn_value(form)}
        if k == edn.MAP:
            # Malli's map syntax: {:type :map :children [...]}
            t = form.get("type")
            if t is not None and t.kind == edn.KEYWORD:
                return self._keyword(t, context)
        self.warnings.append(f"unsupported schema form `{form.text[:60]}` (line {form.line})")
        return {"$comment": f"unsupported Malli form {form.text[:80]}"}

    def _call(self, form: Form, context: Any) -> dict:
        head = form.head
        name = str(head.value).rsplit("/", 1)[-1] if head is not None and head.kind == edn.SYMBOL else ""
        args = form.args
        if name in _SCHEMA_CONSTRUCTORS and args:
            return self._convert(args[0], context)
        if name in ("merge", "union") and args:
            return merge_objects(*(self._inline_object(a, context) for a in args))
        if name in ("optional-keys", "required-keys", "closed-schema", "open-schema", "update-properties") and args:
            return self._convert(args[0], context)
        if name in ("select-keys",) and len(args) >= 2:
            base = self._inline_object(args[0], context)
            keep = [str(edn_value(x)) for x in args[1].items] if args[1].kind == edn.VECTOR else []
            if base.get("type") == "object":
                base = dict(base)
                base["properties"] = {p: v for p, v in base.get("properties", {}).items() if p in keep}
                base["required"] = [r for r in base.get("required", []) if r in keep]
            return base
        if name in ("assoc",) and len(args) >= 3:
            base = self._inline_object(args[0], context)
            extra = {"type": "object", "properties": {str(edn_value(args[1])): self._convert(args[2], context)},
                     "required": [str(edn_value(args[1]))]}
            return merge_objects(base, extra)
        if name in ("dissoc",) and len(args) >= 2:
            base = dict(self._inline_object(args[0], context))
            drop = {str(edn_value(a)) for a in args[1:]}
            base["properties"] = {p: v for p, v in base.get("properties", {}).items() if p not in drop}
            base["required"] = [r for r in base.get("required", []) if r not in drop]
            return base
        self.warnings.append(f"unsupported schema call `{form.text[:60]}` (line {form.line})")
        return {"$comment": f"computed schema {form.text[:80]}"}

    def _inline_object(self, form: Form, context: Any) -> dict:
        """Convert ``form`` and inline a ``$ref`` so that its properties can merge."""
        s = self._convert(form, context)
        ref = s.get("$ref", "") if isinstance(s, dict) else ""
        if ref.startswith("#/$defs/") and len(s) == 1:
            return copy.deepcopy(self.defs.get(ref[len("#/$defs/"):], s))
        return s

    def _entry_key(self, key: Form) -> str:
        return str(key.value) if key.kind in (edn.KEYWORD, edn.SYMBOL, edn.STRING) else key.text

    def _map(self, items: list[Form], props: dict, context: Any) -> dict:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for entry in items:
            if entry.kind == edn.KEYWORD:  # [:map ::registry-key]
                key = self._entry_key(entry)
                properties[key] = self._keyword(entry, context)
                required.append(key)
                continue
            if entry.kind != edn.VECTOR or not entry.items:
                continue
            key = self._entry_key(entry.items[0])
            rest = entry.items[1:]
            entry_props: dict = {}
            if rest and rest[0].kind == edn.MAP:
                entry_props = edn_value(rest[0]) or {}
                rest = rest[1:]
            schema = self._convert(rest[0], context) if rest else (
                self._keyword(entry.items[0], context) if entry.items[0].kind == edn.KEYWORD else {})
            for meta in ("description", "title"):
                if isinstance(entry_props.get(meta), str) and isinstance(schema, dict):
                    schema = {**schema, meta: entry_props[meta]}
            properties[key] = schema
            if not entry_props.get("optional"):
                required.append(key)
        out: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            out["required"] = required
        if props.get("closed"):
            out["additionalProperties"] = False
        return self._with_props(out, "map", {k: v for k, v in props.items() if k != "closed"})

    def _vector(self, form: Form, context: Any) -> dict:
        if not form.items:
            return {"type": "array"}
        head = form.items[0]
        if head.kind == edn.SYMBOL:  # [string? ...] style is not Malli; treat as reference
            return self._convert(head, context)
        if head.kind != edn.KEYWORD:
            return {"$comment": f"unsupported Malli vector {form.text[:80]}"}
        t = str(head.value)
        props, raw_props, children = self._props(form.items[1:])
        conv = lambda f: self._convert(f, context)  # noqa: E731
        if t == "map":
            return self._map(children, props, context)
        if t == "map-of" and len(children) >= 2:
            key_schema = conv(children[0])
            out = {"type": "object", "additionalProperties": conv(children[1])}
            if key_schema.get("type") == "string" and len(key_schema) > 1:
                out["propertyNames"] = key_schema
            return self._with_props(out, t, props)
        if t in ("vector", "sequential", "set", "seqable", "every") and children:
            out = {"type": "array", "items": conv(children[0])}
            if t == "set":
                out["uniqueItems"] = True
            return self._with_props(out, t, props)
        if t == "tuple":
            return {"type": "array", "prefixItems": [conv(c) for c in children], "items": False,
                    "minItems": len(children), "maxItems": len(children)}
        if t == "enum":
            return self._with_props({"enum": [edn_value(c) for c in children]}, t, props)
        if t == "=" and children:
            return {"const": edn_value(children[0])}
        if t == "not=" and children:
            return {"not": {"const": edn_value(children[0])}}
        if t in (">", ">=", "<", "<=") and children:
            key = {">": "exclusiveMinimum", ">=": "minimum", "<": "exclusiveMaximum", "<=": "maximum"}[t]
            return {"type": "number", key: edn_value(children[0])}
        if t == "maybe" and children:
            return {"anyOf": [conv(children[0]), {"type": "null"}]}
        if t in ("or", "orn"):
            alts = [conv(c.items[-1]) if t == "orn" and c.kind == edn.VECTOR and c.items else conv(c) for c in children]
            return {"anyOf": alts}
        if t in ("and", "andn"):
            parts = [conv(c) for c in children]
            objects = [p for p in parts if p.get("type") == "object"]
            if len(objects) == 1 and all(p is objects[0] or not p or "$comment" in p for p in parts):
                return objects[0]
            return {"allOf": parts}
        if t == "not" and children:
            return {"not": conv(children[0])}
        if t == "multi":
            dispatch = raw_props.get("dispatch")
            branches = []
            for c in children:
                if c.kind != edn.VECTOR or len(c.items) < 2:
                    continue
                branch = conv(c.items[-1])
                if dispatch is not None and dispatch.kind == edn.KEYWORD and c.items[0].kind != edn.SYMBOL:
                    branch = merge_objects(branch, {"type": "object", "properties": {
                        str(dispatch.value): {"const": edn_value(c.items[0])}}, "required": [str(dispatch.value)]})
                branches.append(branch)
            return {"oneOf": branches}
        if t == "merge":
            return merge_objects(*(self._inline_object(c, context) for c in children))
        if t == "union":
            return merge_objects(*(self._inline_object(c, context) for c in children))
        if t == "select-keys" and len(children) >= 2:
            return self._call(Form(edn.LIST, form.line, items=[Form(edn.SYMBOL, form.line, "select-keys"), *children]), context)
        if t == "re" and children:
            return {"type": "string", "pattern": children[0].value if children[0].kind in (edn.REGEX, edn.STRING) else children[0].text}
        if t == "fn":
            self.warnings.append(f"opaque :fn schema (line {form.line})")
            return {"$comment": f"Malli :fn predicate {children[0].text[:60] if children else ''}"}
        if t == "ref" and children:
            ref = children[0]
            return self._convert(ref, context) if ref.kind in (edn.KEYWORD, edn.SYMBOL) else {"$ref": f"#/$defs/{edn_value(ref)}"}
        if t == "schema" and children:
            reg = raw_props.get("registry")
            if reg is not None and reg.kind == edn.MAP:
                for rk, rv in reg.pairs():
                    self.registry[str(rk.value)] = (rv, context)
            return conv(children[-1])
        if t in ("cat", "catn", "alt", "altn"):
            return {"type": "array"}
        if t in ("*", "+", "?", "repeat") and children:
            return {"type": "array", "items": conv(children[-1])}
        if t in _SIMPLE:
            return self._keyword(head, context, props)
        return self._keyword(head, context, props)
