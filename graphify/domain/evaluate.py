"""Resolve computed names to constants, when the code makes that possible.

Framework code often builds an id in a variable instead of a literal::

    (def place-order-id :place-order)
    (let [cmd :reserve-stock] {:cmd-id cmd})
    (defn book [carrier] {:cmd-id (keyword (str "book-" (name carrier)))})
    (book :dhl)

:class:`Evaluator` follows ``def`` vars, ``let`` bindings, parameters bound
at a call site, and the pure core functions that build names. It returns
every possible constant, or an empty list when the value depends on runtime
data. It never runs code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from graphify.domain import edn
from graphify.domain.corpus import Corpus, Definition, SourceFile
from graphify.domain.edn import Form

MAX_CANDIDATES = 16
MAX_DEPTH = 8
LET_HEADS = {"let", "let*", "loop", "when-let", "if-let", "when-some", "if-some", "binding", "with-redefs"}
FN_HEADS = {"fn", "fn*", "clojure.core/fn"}


@dataclass(frozen=True)
class Const:
    """A constant: ``kind`` is ``kw``, ``str``, ``num``, ``bool`` or ``nil``."""

    kind: str
    value: object

    def as_str(self) -> str:
        """Return the text that ``(str x)`` gives in Clojure."""
        if self.kind == "kw":
            return f":{self.value}"
        if self.kind == "nil":
            return ""
        if self.kind == "bool":
            return "true" if self.value else "false"
        return str(self.value)

    def as_name(self) -> str:
        """Return the text that ``(name x)`` gives in Clojure."""
        v = str(self.value)
        return v.split("/", 1)[1] if self.kind == "kw" and "/" in v else v


# A binding is a list of possible constants, or a literal collection form.
Binding = Union[list, Form]
Env = dict  # symbol name -> Binding


def arities(form: Form) -> list[tuple[Form, list[Form]]]:
    """Return (params, body) for each arity of a ``defn`` or ``fn`` form."""
    items = form.items[1:]
    if items and items[0].kind == edn.SYMBOL:  # the name
        items = items[1:]
    while items and items[0].kind in (edn.STRING, edn.MAP):  # docstring, attr-map
        items = items[1:]
    if items and items[0].kind == edn.VECTOR:
        return [(items[0], items[1:])]
    return [(a.items[0], a.items[1:]) for a in items if a.kind == edn.LIST and a.items and a.items[0].kind == edn.VECTOR]


def pick_arity(form: Form, argc: int) -> tuple[Form, list[Form]] | None:
    found = arities(form)
    for params, body in found:
        names = [p for p in params.items if not p.is_sym("&")]
        if len(names) == argc and not any(p.is_sym("&") for p in params.items):
            return params, body
    for params, body in found:
        if any(p.is_sym("&") for p in params.items):
            return params, body
    return found[0] if found else None


def binding_names(target: Form) -> list[str]:
    """Return the local names that a binding target introduces."""
    if target.kind == edn.SYMBOL:
        return [] if target.is_sym("&") else [str(target.value)]
    names: list[str] = []
    if target.kind == edn.VECTOR:
        for t in target.items:
            names += binding_names(t)
    elif target.kind == edn.MAP:
        for k, v in target.pairs():
            if k.kind == edn.KEYWORD and k.name in ("keys", "strs", "syms") and v.kind == edn.VECTOR:
                names += [str(s.value).split("/")[-1] for s in v.items if s.kind in (edn.SYMBOL, edn.KEYWORD)]
            elif k.kind == edn.KEYWORD and k.name == "as" and v.kind == edn.SYMBOL:
                names.append(str(v.value))
            elif k.kind in (edn.SYMBOL, edn.VECTOR, edn.MAP):
                names += binding_names(k)
    return names


class Evaluator:
    def __init__(self, corpus: Corpus):
        self.corpus = corpus
        self._defs: dict[str, list[Const]] = {}
        self._active: set[str] = set()

    # -- environments ------------------------------------------------------
    def bind_let(self, bindings: Form, sf: SourceFile, env: Env) -> Env:
        """Return ``env`` with the bindings of a ``let`` vector added."""
        env = dict(env)
        items = bindings.items if bindings.kind == edn.VECTOR else []
        for i in range(0, len(items) - 1, 2):
            target, value = items[i], items[i + 1]
            if target.kind == edn.SYMBOL:
                env[str(target.value)] = self._binding(value, sf, env)
            else:
                for name in binding_names(target):
                    env[name] = []
        return env

    def bind_params(self, params: Form, args: list[Form], sf: SourceFile, env: Env) -> Env:
        """Bind the parameters of a called function to the call arguments."""
        bound: Env = {}
        rest = False
        for i, p in enumerate(params.items):
            if p.is_sym("&"):
                rest = True
                continue
            if rest:
                for name in binding_names(p):
                    bound[name] = []
                continue
            if p.kind == edn.SYMBOL and i < len(args):
                bound[str(p.value)] = self._binding(args[i], sf, env)
            else:
                for name in binding_names(p):
                    bound[name] = []
        return bound

    def shadow(self, params: Form, env: Env) -> Env:
        env = dict(env)
        for name in binding_names(params):
            env[name] = []
        return env

    def _binding(self, value: Form, sf: SourceFile, env: Env) -> Binding:
        literal = self.literal(value, sf, env)
        if literal is not None:
            return literal
        return self.values(value, sf, env)

    # -- literals ------------------------------------------------------------
    def literal(self, form: Form, sf: SourceFile, env: Env, depth: int = 0) -> Form | None:
        """Return the literal map, vector or set that ``form`` names, if any."""
        if form.kind in (edn.MAP, edn.VECTOR, edn.SET):
            return form
        if form.kind == edn.SYMBOL and depth < MAX_DEPTH:
            name = str(form.value)
            if name in env:
                bound = env[name]
                return bound if isinstance(bound, Form) else None
            d = self.corpus.definition(form, sf)
            if d is not None and d.value is not None:
                return self.literal(d.value, d.file, {}, depth + 1)
        return None

    # -- values --------------------------------------------------------------
    def values(self, form: Form | None, sf: SourceFile, env: Env | None = None, depth: int = 0) -> list[Const]:
        """Return every constant that ``form`` can have, or [] when unknown."""
        if form is None or depth > MAX_DEPTH:
            return []
        env = env or {}
        k = form.kind
        if k == edn.KEYWORD:
            return [Const("kw", str(form.value))]
        if k == edn.STRING:
            return [Const("str", str(form.value))]
        if k == edn.NUMBER:
            return [Const("num", form.value)]
        if k == edn.BOOL:
            return [Const("bool", form.value)]
        if k == edn.NIL:
            return [Const("nil", None)]
        if k == edn.SYMBOL:
            return self._symbol(form, sf, env, depth)
        if k == edn.LIST:
            return self._call(form, sf, env, depth)
        return []

    def _symbol(self, form: Form, sf: SourceFile, env: Env, depth: int) -> list[Const]:
        name = str(form.value)
        if name in env:
            bound = env[name]
            return [] if isinstance(bound, Form) else list(bound)
        d = self.corpus.definition(form, sf)
        if d is None or d.value is None:
            return []
        return self._def_values(d, depth)

    def _def_values(self, d: Definition, depth: int) -> list[Const]:
        if d.qualified in self._defs:
            return list(self._defs[d.qualified])
        if d.qualified in self._active:
            return []
        self._active.add(d.qualified)
        try:
            result = self.values(d.value, d.file, {}, depth + 1)
        finally:
            self._active.discard(d.qualified)
        self._defs[d.qualified] = result
        return list(result)

    @staticmethod
    def _limit(values: list[Const]) -> list[Const]:
        out: list[Const] = []
        for v in values:
            if v not in out:
                out.append(v)
        return out if len(out) <= MAX_CANDIDATES else []

    def _branches(self, forms: list[Form], sf: SourceFile, env: Env, depth: int) -> list[Const]:
        out: list[Const] = []
        for f in forms:
            vals = self.values(f, sf, env, depth + 1)
            if not vals:
                return []
            out += vals
        return self._limit(out)

    def _call(self, form: Form, sf: SourceFile, env: Env, depth: int) -> list[Const]:
        head = form.head
        args = form.args
        if head is None:
            return []
        if head.kind == edn.KEYWORD and args:  # (:k m)
            m = self.literal(args[0], sf, env)
            if m is not None and m.kind == edn.MAP:
                return self.values(m.get(str(head.value)), sf, env, depth + 1)
            return []
        if head.kind != edn.SYMBOL:
            return []
        name = str(head.value)
        if name in env:  # a local function value
            return []
        target = self.corpus.resolve(head, sf)
        if target in self.corpus.definitions:
            return self._user_fn(self.corpus.definitions[target], args, sf, env, depth)
        if "/" in name and not name.startswith(("clojure.core/", "core/")):
            return []
        core = name.split("/", 1)[-1]
        if core in ("keyword", "symbol") and args:
            if len(args) == 1:
                return self._limit([Const("kw", v.as_name()) for v in self.values(args[0], sf, env, depth + 1)
                                    if v.kind in ("kw", "str")])
            nss, names = self.values(args[0], sf, env, depth + 1), self.values(args[1], sf, env, depth + 1)
            return self._limit([Const("kw", f"{a.as_name()}/{b.as_name()}" if a.kind != "nil" else b.as_name())
                                for a in nss for b in names])
        if core == "str":
            combos = [""]
            for a in args:
                vals = self.values(a, sf, env, depth + 1)
                if not vals:
                    return []
                combos = [c + v.as_str() for c in combos for v in vals]
                if len(combos) > MAX_CANDIDATES:
                    return []
            return [Const("str", c) for c in combos]
        if core == "name" and args:
            return self._limit([Const("str", v.as_name()) for v in self.values(args[0], sf, env, depth + 1)
                                if v.kind in ("kw", "str")])
        if core in ("if", "if-not", "if-let", "if-some") and len(args) >= 2:
            if core in ("if-let", "if-some") and args[0].kind == edn.VECTOR:
                env = self.bind_let(args[0], sf, env)
            return self._branches(args[1:3] if len(args) >= 3 else args[1:2] + [Form(edn.NIL, form.line)],
                                  sf, env, depth)
        if core == "cond":
            return self._branches(args[1::2], sf, env, depth)
        if core in ("case", "condp"):
            rest = args[1:] if core == "case" else args[2:]
            outs = rest[1::2] + ([rest[-1]] if len(rest) % 2 == 1 else [])
            return self._branches(outs, sf, env, depth)
        if core in ("or", "do", "when", "when-not", "let", "let*", "when-let", "when-some"):
            if core in ("let", "let*", "when-let", "when-some") and args and args[0].kind == edn.VECTOR:
                env = self.bind_let(args[0], sf, env)
                return self.values(args[-1], sf, env, depth + 1) if len(args) > 1 else []
            if core == "or":
                return self._branches(args, sf, env, depth)
            return self.values(args[-1], sf, env, depth + 1) if args else []
        if core == "get" and len(args) >= 2:
            m = self.literal(args[0], sf, env)
            if m is None or m.kind != edn.MAP:
                return []
            keys = self.values(args[1], sf, env, depth + 1)
            out = []
            for key in keys:
                hit = next((v for kf, v in m.pairs() if self.values(kf, sf, env, depth + 1) == [key]), None)
                if hit is None:
                    if len(args) < 3:
                        return []
                    hit = args[2]
                vals = self.values(hit, sf, env, depth + 1)
                if not vals:
                    return []
                out += vals
            return self._limit(out)
        if core == "identity" and args:
            return self.values(args[0], sf, env, depth + 1)
        return []

    def _user_fn(self, d: Definition, args: list[Form], sf: SourceFile, env: Env, depth: int) -> list[Const]:
        if not d.head.startswith("defn") or depth >= MAX_DEPTH:
            return []
        arity = pick_arity(d.form, len(args))
        if arity is None or not arity[1]:
            return []
        params, body = arity
        local = self.bind_params(params, args, sf, env)
        return self.values(body[-1], d.file, local, depth + 1)
