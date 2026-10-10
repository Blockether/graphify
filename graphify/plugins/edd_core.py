"""Domain plugin for edd-core services (https://github.com/alpha-prosoft/edd-core).

It reads ``edd.core`` registrations (``reg-cmd``, ``reg-query``, ``reg-event``,
``reg-event-fx``, ``reg-fx``, ``reg-agg-filter``, ``reg-service-schema``) and
follows each handler into its code to find:

* the events that a command emits (maps with ``:event-id``),
* the queries that a command or a query reads through ``:deps``,
* the commands that an event effect starts (maps with ``:cmd-id``, and a
  ``:service`` key for another service),
* the commands and queries that other code calls.

It converts the ``:consumes`` and ``:produces`` Malli schemas to JSON Schema,
with the keys that edd-core adds (``:id`` and ``:cmd-id``, ``:query-id``,
``:result``), and reports errors in the schemas. It checks each command and
query map in the code against the full ``:consumes`` schema of its target.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphify.domain import edn
from graphify.domain.corpus import Corpus, SourceFile
from graphify.domain.edn import Form
from graphify.domain.evaluate import FN_HEADS, LET_HEADS, Evaluator, arities, pick_arity
from graphify.domain.malli import MalliConverter, literal, merge_objects
from graphify.domain.model import ERROR, INFO, WARNING, DomainModel, Element, Finding, KindSpec, Ref, Relation
from graphify.domain.plugin import DomainPlugin
from graphify.domain.validate import UNKNOWN, Checker, Keyword

EDD = "edd.core"
REG_WITH_ID = {"reg-cmd", "reg-query", "reg-event", "reg-event-fx", "reg-feature"}
REG_WITHOUT_ID = {"reg-fx", "reg-agg-filter", "reg-service-schema"}
REGISTRATIONS = {f"{EDD}/{n}": n for n in REG_WITH_ID | REG_WITHOUT_ID}
ID_KEYS = {"event-id": "event", "cmd-id": "command", "query-id": "query"}
MAX_FOLLOW_DEPTH = 6
MAX_PROBLEMS_SHOWN = 4

COMMAND, QUERY, EVENT, EFFECT, FUNCTION = "command", "query", "event", "effect", "function"


@dataclass
class Invocation:
    kind: str  # command, query or event
    name: str
    service: str | None
    file: str
    line: int
    via: str | None = None  # the source text when the id was computed
    payload: Any = None  # the static value of a command or query map, for schema checks


@dataclass
class Dynamic:
    key: str
    text: str
    file: str
    line: int


class _Scan:
    """The result of following one handler into the code."""

    def __init__(self) -> None:
        self.found: list[Invocation] = []
        self.dynamic: list[Dynamic] = []
        self.visited: set[str] = set()


def _kw_value(form: Form | None) -> str | None:
    if form is None:
        return None
    if form.kind == edn.KEYWORD:
        return str(form.value)
    if form.kind == edn.STRING:
        return str(form.value)
    return None


class EddCorePlugin(DomainPlugin):
    name = "edd-core"
    description = "edd-core commands, queries, events, effects and Malli schemas"
    kinds = {
        COMMAND: KindSpec("Command", "Commands", "stadium", entry=True),
        QUERY: KindSpec("Query", "Queries", "parallelogram"),
        EVENT: KindSpec("Event", "Events", "hexagon"),
        EFFECT: KindSpec("Effect", "Effects", "subroutine"),
        FUNCTION: KindSpec("Code caller", "Code callers", "rect", entry=True),
    }

    def __init__(self, include_tests: bool = False):
        self.include_tests = include_tests

    # -- contract ----------------------------------------------------------
    def detect(self, corpus: Corpus) -> bool:
        return any(sf.requires(EDD) for sf in self._files(corpus))

    def extract(self, corpus: Corpus, model: DomainModel) -> None:
        self.corpus, self.model = corpus, model
        self.ev = Evaluator(corpus)
        self._env: dict = {}
        self._sf: SourceFile | None = None
        self.handler_defs: set[str] = set()
        self.registrations: list[tuple[str, Form, SourceFile, str]] = []
        self.counts: dict[Ref, int] = {}
        self.emitted: set[Ref] = set()
        self.applied: set[Ref] = set()
        self.with_fx: set[Ref] = set()
        self.reg_files: set[str] = set()
        self.reached: set[str] = set()
        self.payloads: dict[tuple, tuple[Ref, Ref, Invocation]] = {}
        self.schema_issues: set[tuple] = set()
        # Pass 1: find the registrations, the services and the handler symbols.
        for sf in self._files(corpus):
            if not sf.requires(EDD):
                continue
            service = self.service_name(sf)
            for target, call, env in self._registration_calls(sf):
                self.registrations.append((REGISTRATIONS[target], call, sf, service, env))
                self.reg_files.add(sf.rel)
                for arg in call.args:
                    if arg.kind == edn.SYMBOL:
                        d = corpus.definition(arg, sf)
                        if d is not None and d.head.startswith("defn"):
                            self.handler_defs.add(d.qualified)
        for sf in self._files(corpus):
            if sf.rel in self.reg_files:
                model.add_service(self.service_name(sf), namespace=sf.ns, file=sf.rel)
        # Pass 2: analyze each registration.
        for kind, call, sf, service, env in self.registrations:
            self._env, self._sf = env, sf
            getattr(self, "_" + kind.replace("-", "_"))(call, sf, service)
        self._env = {}
        # Pass 3: code outside the handlers that calls commands and queries.
        self._code_callers()

    def finalize(self, corpus: Corpus, model: DomainModel) -> None:
        self._check_payloads(model)
        for ref, n in self.counts.items():
            if n > 1:
                e = model.elements.get(ref)
                model.report(Finding(ERROR, f"duplicate-{ref.kind}",
                                     f"{ref} is registered {n} times; the last registration wins.",
                                     e.file if e else None, e.line if e else None, ref, self.name))
        targeted = {r.target for r in model.relations}
        for e in model.of_kind(EVENT):
            if e.ref in self.emitted and e.ref not in self.applied:
                model.report(Finding(WARNING, "event-not-applied",
                                     f"{e.ref} is emitted, but no `reg-event` applies it to the aggregate.",
                                     e.file, e.line, e.ref, self.name))
            if e.ref not in self.emitted and (e.ref in self.applied or e.ref in self.with_fx):
                model.report(Finding(WARNING, "event-never-emitted",
                                     f"{e.ref} has a handler, but no command of `{e.service}` emits it.",
                                     e.file, e.line, e.ref, self.name))
        for e in model.of_kind(COMMAND):
            if e.ref not in targeted:
                model.report(Finding(INFO, "command-entry-point",
                                     f"{e.ref} is not called from code or effects in this corpus; "
                                     "only external clients can start it.", e.file, e.line, e.ref, self.name))
            if "consumes" not in e.sources:
                model.report(Finding(INFO, "command-without-schema",
                                     f"{e.ref} has no `:consumes` schema; edd-core checks only `:id` and `:cmd-id`.",
                                     e.file, e.line, e.ref, self.name))
        for e in model.of_kind(QUERY):
            if e.ref not in targeted and e.name != "get-by-id":
                model.report(Finding(INFO, "query-unused",
                                     f"{e.ref} is not read by any command, query or code in this corpus.",
                                     e.file, e.line, e.ref, self.name))

    # -- discovery helpers ---------------------------------------------------
    def _files(self, corpus: Corpus) -> list[SourceFile]:
        return [sf for sf in corpus.files if self.include_tests or not sf.is_test]

    def service_name(self, sf: SourceFile) -> str:
        """Find the ``:service-name`` of a file, or derive one from its path."""
        for top in sf.forms:
            for form in top.walk():
                items = form.items if form.kind in (edn.LIST, edn.VECTOR, edn.MAP) else []
                for i in range(len(items) - 1):
                    if items[i].is_kw("service-name") and _kw_value(items[i + 1]):
                        return _kw_value(items[i + 1]) or ""
        for part in reversed(Path(sf.rel).parts[:-1]):
            if part.endswith(("-svc", "-service", "_svc", "_service")):
                return part
        return (sf.ns or Path(sf.rel).stem).split(".")[0]

    def _registration_calls(self, sf: SourceFile):
        """Yield (target, call, env) for each edd registration, with ``let`` bindings."""
        def walk(form: Form, env: dict):
            if form.kind == edn.LIST and form.head is not None and form.head.kind == edn.SYMBOL:
                head = str(form.head.value)
                target = self.corpus.call_target(form, sf)
                if target in REGISTRATIONS and head not in env:
                    yield target, form, env
                if head in LET_HEADS and len(form.items) > 1 and form.items[1].kind == edn.VECTOR:
                    inner = self.ev.bind_let(form.items[1], sf, env)
                    for body in form.items[2:]:
                        yield from walk(body, inner)
                    return
                if head in ("defn", "defn-", "fn"):
                    for params, body in arities(form):
                        inner = self.ev.shadow(params, env)
                        for b in body:
                            yield from walk(b, inner)
                    return
            for child in form.items:
                yield from walk(child, env)

        for top in sf.forms:
            yield from walk(top, {})

    def _split(self, call: Form, kind: str) -> tuple[Form | None, Form | None, dict[str, Form]]:
        """Return (id, handler, options) of a registration call.

        The call is threaded (``(-> ctx (reg-cmd :id h))``) when its first
        argument is an id, or a var or binding that holds a keyword. Otherwise
        the first argument is the context.
        """
        args = call.args
        if kind in REG_WITH_ID:
            if args and not args[0].is_kw():
                threaded = bool(self._ids(args[0], self._sf, self._env)) or len(args) % 2 == 0
                if not threaded:
                    args = args[1:]  # explicit ctx argument
            ident = args[0] if args else None
            handler = args[1] if len(args) > 1 else None
            rest = args[2:]
        else:
            if len(args) >= 2:
                args = args[1:]
            ident, handler, rest = None, (args[0] if args else None), []
        opts: dict[str, Form] = {}
        if len(rest) == 1 and rest[0].kind == edn.MAP:
            rest = rest[0].items
        for i in range(0, len(rest) - 1, 2):
            if rest[i].kind == edn.KEYWORD:
                opts[str(rest[i].value)] = rest[i + 1]
        return ident, handler, opts

    def _ident(self, ident: Form | None, call: Form, sf: SourceFile, kind: str) -> str | None:
        names = self._ids(ident, sf, self._env) if ident is not None else []
        if len(names) == 1:
            return names[0]
        detail = f" (one of {', '.join(names)})" if names else ""
        self.model.report(Finding(INFO, "dynamic-registration",
                                  f"`{edn.to_edn(call)[:80]}` registers a {kind} with a computed id{detail}.",
                                  sf.rel, call.line, None, self.name))
        return None

    def _ids(self, value: Form, sf: SourceFile, env: dict) -> list[str]:
        """Return the keyword names that ``value`` can be, or [] when unknown."""
        values = self.ev.values(value, sf, env)
        if not values or any(v.kind != "kw" for v in values):
            return []
        return [v.as_name() for v in values]

    def scan(self, form: Form, sf: SourceFile, scan: _Scan, service: str | None = None, depth: int = 0,
             env: dict | None = None) -> _Scan:
        """Collect the ids in ``form`` and in the corpus functions that it calls.

        ``env`` holds the local bindings (``let`` values and call arguments), so
        an id in a var, a binding or a parameter resolves to its constant.
        """
        env = env if env is not None else {}
        items = form.items
        if form.kind == edn.MAP:
            svc_form = form.get("service")
            if svc_form is not None:
                names = [v.as_name() for v in self.ev.values(svc_form, sf, env) if v.kind in ("kw", "str")]
                if len(names) == 1:
                    service = names[0]
            for key, kind in ID_KEYS.items():
                value = form.get(key)
                if value is None:
                    continue
                names = self._ids(value, sf, env)
                via = None if value.kind == edn.KEYWORD else value.text
                payload = self._payload(form, sf, env) if kind in (COMMAND, QUERY) and names else None
                for name in names:
                    scan.found.append(Invocation(kind, name, service, sf.rel, value.line, via, payload))
                if not names:
                    scan.dynamic.append(Dynamic(key, value.text, sf.rel, value.line))
        elif form.kind == edn.LIST and form.head is not None and form.head.kind == edn.SYMBOL:
            head = str(form.head.value)
            if head in LET_HEADS and len(items) > 1 and items[1].kind == edn.VECTOR:
                bindings = items[1].items
                for i in range(1, len(bindings), 2):
                    self.scan(bindings[i], sf, scan, service, depth, env)
                inner = self.ev.bind_let(items[1], sf, env)
                for body in items[2:]:
                    self.scan(body, sf, scan, service, depth, inner)
                return scan
            if head in FN_HEADS:
                for params, body in arities(form):
                    inner = self.ev.shadow(params, env)
                    for b in body:
                        self.scan(b, sf, scan, service, depth, inner)
                return scan
            for arg in form.args:
                name = _kw_value(arg)
                if name and name in self.model.services and name not in ID_KEYS:
                    service = name
            d = None if head in env else self.corpus.definition(form.head, sf)
            if d is not None and d.head.startswith("defn") and d.qualified not in self.handler_defs:
                self._follow(d, form.args, sf, scan, service, depth, env)
            for child in items[1:]:
                self.scan(child, sf, scan, service, depth, env)
            return scan
        elif form.kind == edn.SYMBOL and depth < MAX_FOLLOW_DEPTH and str(form.value) not in env:
            # A function passed as a value, such as a dep function or a mapper.
            d = self.corpus.definition(form, sf)
            if d is not None and (d.qualified not in self.handler_defs or depth == 0):
                if d.head.startswith("defn"):
                    self._follow(d, None, sf, scan, service, depth, env)
                elif (d.qualified, ()) not in scan.visited:
                    scan.visited.add((d.qualified, ()))
                    self.reached.add(d.qualified)
                    for body in d.body:
                        self.scan(body, d.file, scan, service, depth + 1)
            return scan
        for child in items:
            self.scan(child, sf, scan, service, depth, env)
        return scan

    def _follow(self, d, args: list[Form] | None, sf: SourceFile, scan: _Scan, service: str | None,
                depth: int, env: dict) -> None:
        """Scan the body of a called function with its parameters bound to the arguments."""
        if depth >= MAX_FOLLOW_DEPTH:
            return
        found = [pick_arity(d.form, len(args))] if args is not None else arities(d.form)
        for arity in found:
            if arity is None:
                continue
            params, body = arity
            local = self.ev.bind_params(params, args, sf, env) if args is not None else self.ev.shadow(params, {})
            key = (d.qualified, tuple(sorted((k, tuple(v) if isinstance(v, list) else id(v)) for k, v in local.items())))
            if key in scan.visited:
                continue
            scan.visited.add(key)
            self.reached.add(d.qualified)
            for b in body:
                self.scan(b, d.file, scan, service, depth + 1, local)

    def _handler_scan(self, handler: Form | None, sf: SourceFile) -> _Scan:
        scan = _Scan()
        if handler is not None:
            if handler.kind == edn.SYMBOL:
                d = self.corpus.definition(handler, sf)
                if d is not None and d.head.startswith("defn"):
                    self._follow(d, None, sf, scan, None, 0, {})
                elif d is not None:
                    for body in d.body:
                        self.scan(body, d.file, scan, None, 1)
            else:
                self.scan(handler, sf, scan, None, 0, self._env)
        return scan

    def _handler_info(self, handler: Form | None, sf: SourceFile) -> tuple[str | None, str | None, int | None]:
        """Return (qualified name, file, line) of a handler."""
        if handler is None:
            return None, None, None
        if handler.kind == edn.SYMBOL:
            d = self.corpus.definition(handler, sf)
            if d is not None:
                return d.qualified, d.file.rel, d.line
            return self.corpus.resolve(handler, sf), None, None
        return f"<inline fn at {sf.rel}:{handler.line}>", sf.rel, handler.line

    def _check_handler(self, handler: Form | None, sf: SourceFile, ref: Ref) -> None:
        if handler is None:
            self.model.report(Finding(ERROR, "missing-handler", f"{ref} is registered without a handler.",
                                      sf.rel, None, ref, self.name))
            return
        if handler.kind != edn.SYMBOL:
            return
        qualified = self.corpus.resolve(handler, sf)
        if self.corpus.definitions.get(qualified) is not None:
            return
        ns = qualified.split("/", 1)[0] if "/" in qualified else None
        if ns is None or ns in self.corpus.by_ns:
            self.model.report(Finding(ERROR, "unresolved-handler",
                                      f"{ref} uses handler `{handler.value}`, which is not defined.",
                                      sf.rel, handler.line, ref, self.name))

    def _dynamic(self, scan: _Scan, source: Ref) -> None:
        for d in scan.dynamic:
            self.model.report(Finding(INFO, "dynamic-reference",
                                      f"{source} uses a computed `:{d.key}` ({d.text[:60]}); the graph cannot follow it.",
                                      d.file, d.line, source, self.name))

    def _relate(self, source: Ref, inv: Invocation, relation: str, default_service: str) -> None:
        target = Ref(inv.kind, inv.service or default_service, inv.name)
        attrs = {"remote": target.service != source.service}
        if inv.via:
            attrs["resolved_from"] = inv.via
        self.model.relate(Relation(source, target, relation, inv.file, inv.line, True, attrs, self.name))
        if isinstance(inv.payload, dict):
            self.payloads.setdefault((source, target, inv.file, inv.line), (source, target, inv))

    # -- payload checks ----------------------------------------------------------
    def _payload(self, form: Form, sf: SourceFile, env: dict) -> Any:
        """The value of a command or query map, without the routing key ``:service``."""
        value = self._static(form, sf, env, 0)
        if isinstance(value, dict):
            value.pop("service", None)
        return value

    def _static(self, form: Form, sf: SourceFile, env: dict, depth: int) -> Any:
        """The value of ``form`` as far as the code shows it; :data:`UNKNOWN` elsewhere."""
        if depth > MAX_FOLLOW_DEPTH:
            return UNKNOWN
        k = form.kind
        if k in (edn.VECTOR, edn.SET):
            return [self._static(i, sf, env, depth + 1) for i in form.items]
        if k == edn.MAP:
            out = {}
            for key, item in form.pairs():
                if key.kind not in (edn.KEYWORD, edn.STRING):
                    return UNKNOWN
                out[str(key.value)] = self._static(item, sf, env, depth + 1)
            return out
        if k == edn.SYMBOL:
            bound = env.get(str(form.value))
            if isinstance(bound, Form):
                return self._static(bound, sf, env, depth + 1)
            if bound is None:
                d = self.corpus.definition(form, sf)
                if d is not None and d.head == "def" and d.value is not None and d.value.kind in (edn.MAP, edn.VECTOR):
                    return self._static(d.value, d.file, {}, depth + 1)
        if k in (edn.SYMBOL, edn.LIST):
            consts = self.ev.values(form, sf, env)
            if len(consts) != 1:
                return UNKNOWN
            c = consts[0]
            if c.kind == "kw":
                return Keyword(str(c.value))
            if c.kind == "nil":
                return None
            return c.value if c.kind in ("str", "bool") or isinstance(c.value, (int, float)) else UNKNOWN
        return literal(form)

    def _check_payloads(self, model: DomainModel) -> None:
        for source, target, inv in self.payloads.values():
            element = model.elements.get(target)
            schema = element.schemas.get("consumes") if element is not None else None
            if not schema:
                continue
            problems = Checker(schema).check(inv.payload)
            if not problems:
                continue
            shown = "; ".join(str(p) for p in problems[:MAX_PROBLEMS_SHOWN])
            if len(problems) > MAX_PROBLEMS_SHOWN:
                shown += f"; and {len(problems) - MAX_PROBLEMS_SHOWN} more"
            model.report(Finding(ERROR, "payload-invalid",
                                 f"{source} sends {target.kind} `{target.name}` with data that its `:consumes` "
                                 f"schema rejects: {shown}.", inv.file, inv.line, target, self.name))

    # -- schemas ---------------------------------------------------------------
    def _converter(self, sf: SourceFile) -> MalliConverter:
        corpus = self.corpus

        def resolve(symbol: Form, ctx: SourceFile):
            d = corpus.definition(symbol, ctx)
            if d is None or d.value is None:
                return None
            return d.qualified, d.value, d.file

        def is_local(symbol: Form, ctx: SourceFile) -> bool:
            qualified = corpus.resolve(symbol, ctx)
            return "/" not in qualified or qualified.rsplit("/", 1)[0] in corpus.by_ns

        return MalliConverter(resolve, is_local=is_local)

    def _schema(self, element: Element, part: str, form: Form | None, sf: SourceFile, base: dict | None,
                wrap_result: bool = False) -> None:
        conv = self._converter(sf)
        if form is None:
            if base is not None:
                element.schemas[part] = conv.wrap(base, f"{element.name} {part}")
            return
        body = conv.convert(form, sf)
        if wrap_result:
            body = {"type": "object", "properties": {"result": body}, "required": ["result"]}
        if base is not None:
            body = merge_objects(base, conv._inline_object(form, sf) if not wrap_result else body)
        element.schemas[part] = conv.wrap(body, f"{element.name} {part}")
        element.sources[part] = edn.to_edn(form)
        if form.kind == edn.SYMBOL:
            d = self.corpus.definition(form, sf)
            if d is not None and d.value is not None:
                element.sources[part] = f";; {d.qualified} ({d.file.rel}:{d.line})\n{d.value.text}"
        severity = {"error": ERROR, "warning": WARNING, "info": INFO}
        for issue in conv.issues:
            file = issue.context.rel if isinstance(issue.context, SourceFile) else sf.rel
            line = issue.line or form.line
            key = (issue.code, file, line, issue.message)
            if key in self.schema_issues:
                continue  # a shared schema reports each problem once
            self.schema_issues.add(key)
            self.model.report(Finding(severity[issue.severity], issue.code,
                                      f"{element.ref} {part}: {issue.message}.", file, line, element.ref, self.name))

    # -- registrations -----------------------------------------------------------
    def _element(self, kind: str, service: str, name: str, sf: SourceFile, line: int,
                 handler: Form | None, **attrs: Any) -> Element:
        qualified, hfile, hline = self._handler_info(handler, sf)
        ref = Ref(kind, service, name)
        self.counts[ref] = self.counts.get(ref, 0) + 1
        attrs = {k: v for k, v in attrs.items() if v not in (None, [], {})}
        if hfile:
            attrs["handler_file"], attrs["handler_line"] = hfile, hline
        return self.model.add(Element(ref, sf.rel, line, qualified, None, attrs, plugin=self.name))

    @staticmethod
    def _auth(opts: dict[str, Form]) -> list[str]:
        auth = opts.get("auth")
        role = auth.get("role") if auth is not None and auth.kind == edn.MAP else None
        if role is None:
            return []
        if role.kind in (edn.VECTOR, edn.SET):
            return [str(r.value) for r in role.items if r.kind == edn.KEYWORD]
        return [str(role.value)] if role.kind == edn.KEYWORD else [role.text]

    def _deps(self, opts: dict[str, Form], sf: SourceFile, source: Ref) -> list[str]:
        deps = opts.get("deps")
        if deps is None:
            return []
        pairs: list[tuple[Form, Form]] = []
        if deps.kind == edn.VECTOR:
            pairs = [(deps.items[i], deps.items[i + 1]) for i in range(0, len(deps.items) - 1, 2)]
        elif deps.kind == edn.MAP:
            pairs = deps.pairs()
        keys = []
        for key, dep in pairs:
            keys.append(str(key.value) if key.kind == edn.KEYWORD else key.text)
            scan = self.scan(dep, sf, _Scan(), None, 0, self._env)
            for inv in scan.found:
                if inv.kind == QUERY:
                    self._relate(source, inv, "reads", source.service)
            self._dynamic(scan, source)
        return keys

    def _reg_cmd(self, call: Form, sf: SourceFile, service: str) -> None:
        ident, handler, opts = self._split(call, "reg-cmd")
        name = self._ident(ident, call, sf, COMMAND)
        if name is None:
            return
        ref = Ref(COMMAND, service, name)
        e = self._element(COMMAND, service, name, sf, call.line, handler, auth=self._auth(opts))
        e.attrs["deps"] = self._deps(opts, sf, ref) or e.attrs.get("deps", [])
        if not e.attrs["deps"]:
            e.attrs.pop("deps")
        self._check_handler(handler, sf, ref)
        base = {"type": "object", "properties": {"id": {"type": "string", "format": "uuid"},
                                                  "cmd-id": {"const": name}}, "required": ["id", "cmd-id"]}
        self._schema(e, "consumes", opts.get("consumes") or opts.get("spec"), sf, base)
        scan = self._handler_scan(handler, sf)
        for inv in scan.found:
            if inv.kind == EVENT:
                ev = Ref(EVENT, service, inv.name)
                self.emitted.add(ev)
                self.model.add(Element(ev, inv.file, inv.line, None, None, {}, plugin=self.name))
                self.model.relate(Relation(ref, ev, "emits", inv.file, inv.line, True, {}, self.name))
            elif inv.kind == COMMAND:
                self._relate(ref, inv, "invokes", service)
            elif inv.kind == QUERY:
                self._relate(ref, inv, "reads", service)
        self._dynamic(scan, ref)

    def _reg_query(self, call: Form, sf: SourceFile, service: str) -> None:
        ident, handler, opts = self._split(call, "reg-query")
        name = self._ident(ident, call, sf, QUERY)
        if name is None:
            return
        ref = Ref(QUERY, service, name)
        e = self._element(QUERY, service, name, sf, call.line, handler, auth=self._auth(opts))
        deps = self._deps(opts, sf, ref)
        if deps:
            e.attrs["deps"] = deps
        self._check_handler(handler, sf, ref)
        base = {"type": "object", "properties": {"query-id": {"const": name}}, "required": ["query-id"]}
        self._schema(e, "consumes", opts.get("consumes") or opts.get("spec"), sf, base)
        if opts.get("produces") is not None:
            self._schema(e, "produces", opts.get("produces"), sf, None, wrap_result=True)
        scan = self._handler_scan(handler, sf)
        for inv in scan.found:
            if inv.kind in (QUERY, COMMAND):
                self._relate(ref, inv, "reads" if inv.kind == QUERY else "invokes", service)
        self._dynamic(scan, ref)

    def _reg_event(self, call: Form, sf: SourceFile, service: str) -> None:
        ident, handler, _ = self._split(call, "reg-event")
        name = self._ident(ident, call, sf, EVENT)
        if name is None:
            return
        ref = Ref(EVENT, service, name)
        self.applied.add(ref)
        qualified, _, _ = self._handler_info(handler, sf)
        e = self.model.add(Element(ref, sf.rel, call.line, None, None, {}, plugin=self.name))
        e.file, e.line = sf.rel, call.line
        e.attrs["apply"] = qualified
        self.counts[Ref("event-apply", service, name)] = self.counts.get(Ref("event-apply", service, name), 0) + 1
        self._check_handler(handler, sf, ref)

    def _reg_event_fx(self, call: Form, sf: SourceFile, service: str) -> None:
        ident, handler, _ = self._split(call, "reg-event-fx")
        name = self._ident(ident, call, sf, EVENT)
        if name is None:
            return
        ref = Ref(EVENT, service, name)
        self.with_fx.add(ref)
        qualified, _, _ = self._handler_info(handler, sf)
        e = self.model.add(Element(ref, sf.rel, call.line, None, None, {}, plugin=self.name))
        e.attrs["fx"] = qualified
        self._check_handler(handler, sf, ref)
        scan = self._handler_scan(handler, sf)
        for inv in scan.found:
            if inv.kind == COMMAND:
                self._relate(ref, inv, "triggers", service)
            elif inv.kind == QUERY:
                self._relate(ref, inv, "reads", service)
        self._dynamic(scan, ref)

    def _reg_fx(self, call: Form, sf: SourceFile, service: str) -> None:
        _, handler, _ = self._split(call, "reg-fx")
        qualified, hfile, hline = self._handler_info(handler, sf)
        name = (qualified or "fx").rsplit("/", 1)[-1]
        ref = Ref(EFFECT, service, name)
        self.model.add(Element(ref, sf.rel, call.line, qualified, "Effect that runs after every command.",
                               {"handler_file": hfile, "handler_line": hline}, plugin=self.name))
        scan = self._handler_scan(handler, sf)
        for inv in scan.found:
            if inv.kind == COMMAND:
                self._relate(ref, inv, "triggers", service)
            elif inv.kind == QUERY:
                self._relate(ref, inv, "reads", service)
        self._dynamic(scan, ref)

    def _reg_agg_filter(self, call: Form, sf: SourceFile, service: str) -> None:
        _, handler, _ = self._split(call, "reg-agg-filter")
        qualified, _, _ = self._handler_info(handler, sf)
        self.model.services[service].setdefault("aggregate_filters", []).append(qualified)

    def _reg_service_schema(self, call: Form, sf: SourceFile, service: str) -> None:
        _, schema, _ = self._split(call, "reg-service-schema")
        if schema is None:
            return
        conv = self._converter(sf)
        self.model.services[service]["schema"] = conv.document(schema, sf, f"{service} service schema")
        self.model.services[service]["schema_source"] = edn.to_edn(schema)

    def _reg_feature(self, call: Form, sf: SourceFile, service: str) -> None:
        ident, _, _ = self._split(call, "reg-feature")
        if ident is not None:
            self.model.services[service].setdefault("features", []).append(edn.to_edn(ident).lstrip(":"))

    # -- other code --------------------------------------------------------------
    def _code_callers(self) -> None:
        reached: set[str] = set(self.handler_defs) | self.reached
        for _kind, call, sf, _service, _env in self.registrations:
            d = self.corpus.enclosing_definition(sf, call.line)
            if d is not None:
                reached.add(d.qualified)
        for sf in self._files(self.corpus):
            file_service = self.service_name(sf) if sf.requires(EDD) else None
            for d in [d for d in self.corpus.definitions.values() if d.file is sf]:
                if d.qualified in reached or d.head not in ("defn", "defn-", "def"):
                    continue
                scan = _Scan()
                if d.head.startswith("defn"):
                    self._follow(d, None, sf, scan, None, 0, {})
                else:
                    for body in d.body:
                        self.scan(body, sf, scan, None, 1)
                invocations = [i for i in scan.found if i.kind in (COMMAND, QUERY)]
                if not invocations:
                    continue
                owner = file_service or (sf.ns or sf.rel)
                if file_service is None:
                    self.model.add_service(owner, role="client", namespace=sf.ns, file=sf.rel)
                ref = Ref(FUNCTION, owner, d.name if owner == sf.ns else d.qualified)
                self.model.add(Element(ref, sf.rel, d.line, d.qualified, None, {}, plugin=self.name))
                for inv in invocations:
                    if inv.service is None and file_service is None:
                        self.model.report(Finding(WARNING, "unscoped-call",
                                                  f"`{d.qualified}` calls {inv.kind} `{inv.name}` without a `:service`.",
                                                  inv.file, inv.line, ref, self.name))
                        continue
                    self._relate(ref, inv, "invokes" if inv.kind == COMMAND else "reads", owner)


plugin = EddCorePlugin
