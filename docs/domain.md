# Domain flows

Use `graphify domain` when your Clojure services register commands, queries and events through a
framework, and you want to see how they connect. Graphify reads the registrations, follows the
calls between services and writes one Markdown report with Mermaid diagrams and JSON Schemas.

## When to use

- You want to know which events a command emits and which services react to them: see
  [Run the analysis](#run-the-analysis).
- Code calls a command or a query that no service registers: see [Findings](#findings).
- You want JSON Schemas for the Malli schemas of your commands and queries: see [Outputs](#outputs).
- Your framework is not edd-core: see [Write a plugin](#write-a-plugin).

## Run the analysis

```bash
graphify domain .                       # detect plugins, write graphify-out/domain/
graphify domain . --plugin edd-core     # use only the named plugin
graphify domain . --strict              # exit 1 when a finding has the severity "error"
graphify domain . --json                # print the model as JSON
graphify domain --list-plugins          # show the plugins that graphify can find
```

`graphify update .` also runs the analysis. It adds the domain elements to `graph.json` as
concept nodes (`domain:<kind>:<service>:<name>`), with `handled_by` edges to the code that handles
them. Set `GRAPHIFY_DOMAIN=0` to skip this step.

## Outputs

The command writes these files to `graphify-out/domain/` (or to `--out`):

| File | Content |
|---|---|
| `DOMAIN.md` | Summary, findings, a system flowchart, one flowchart for each service, one sequence diagram for each entry point, cycles and schemas. |
| `domain.json` | The full model: elements, relations, findings and services. |
| `schemas/<service>/<kind>.<name>.<part>.json` | One JSON Schema (draft 2020-12) for each Malli schema. |

The Malli converter supports maps, `:map-of`, `:vector`, `:sequential`, `:set`, `:tuple`, `:enum`,
`:=`, `:maybe`, `:or`, `:and`, `:multi`, `:re`, `:fn` (as an approximation), predicates such as
`string?` and `uuid?`, size and range properties, and schema vars. A named schema var becomes a
`$defs` entry. A schema that has no exact JSON Schema form gives a `schema-approximation` finding.

The converter also checks each schema. It reports unknown types, `:min` above `:max`, a `:default`
that the schema rejects, a regex that cannot compile, duplicate map keys and schema vars that are
not defined. The plugin then checks each call with constant data against the full `:consumes`
schema of its target. `DOMAIN.md` lists these problems under each schema.

## The edd-core plugin

The built-in `edd-core` plugin reads [edd-core](https://github.com/alpha-prosoft/edd-core)
services. It finds:

- `reg-cmd`, `reg-query`, `reg-event`, `reg-fx`, `reg-event-fx`, `reg-agg-filter`, `reg-feature`
  and `reg-service-schema`, in the threaded form (`(-> ctx (edd/reg-cmd ...))`) and the explicit
  form (`(edd/reg-cmd ctx ...)`);
- the events that each command handler returns (maps with `:event-id`);
- the commands and queries that handlers and effects start (maps with `:cmd-id` or `:query-id`,
  with an optional `:service` key for another service), also in the functions that they call;
- clients outside a service, such as an API namespace, as callers with the role `client`.

Identifiers can be literals, `def` vars, `let` bindings or function parameters. The plugin follows
a value through these when it is constant. A `case` or `cond` gives every possible branch. A
value that it cannot compute gives a `dynamic-reference` finding.

## Findings

| Code | Severity | Meaning |
|---|---|---|
| `missing-command`, `missing-query`, `missing-event` | error | Code calls an element that no service registers. |
| `duplicate-command`, `duplicate-query`, ... | error | Two registrations use the same identifier in one service. |
| `unresolved-handler`, `missing-handler` | error | A registration names a handler that is not defined, or has no handler. |
| `unknown-service` | warning | A call targets a service that the corpus does not contain. |
| `event-not-applied`, `event-never-emitted` | warning | An event has no apply function, or no command emits it. |
| `unscoped-call` | warning | A function outside a service calls an element without a `:service` key. |
| `command-entry-point`, `command-without-schema`, `query-unused` | info | Only clients start a command, a command has no `:consumes` schema, or no code calls a query. |
| `dynamic-reference`, `dynamic-registration` | info | A value is not constant, so the link is not certain. |
| `payload-invalid` | error | A call sends data that the `:consumes` schema of its target rejects. |
| `schema-unknown-type`, `schema-invalid-bounds`, `schema-invalid-default` | error | A schema names an unknown type, has impossible bounds or a default that it rejects. |
| `schema-invalid-regex`, `schema-duplicate-key`, `schema-invalid-entry`, `schema-arity` | error | A schema has a regex that cannot compile, a repeated key or a malformed entry. |
| `schema-unresolved` | error or warning | A schema var is not defined (error), or comes from a library outside the corpus (warning). |
| `schema-approximation` | info | A Malli schema has no exact JSON Schema form. |

Test files are not part of the analysis. Calls from tests do not hide a missing registration.

## Write a plugin

A plugin is a Python object with a `name` and an `extract(corpus, model)` method. Subclass
`DomainPlugin` to get the defaults:

```python
from graphify.domain import edn
from graphify.domain.model import Element, KindSpec, Ref, Relation
from graphify.domain.plugin import DomainPlugin


class RoutesPlugin(DomainPlugin):
    name = "routes"
    description = "Reads (defroute :name handler :calls [:other ...])."
    kinds = {"route": KindSpec("Route", "Routes", "stadium", entry=True)}

    def detect(self, corpus):
        return any(f.ns and f.ns.startswith("web.") for f in corpus.files)

    def extract(self, corpus, model):
        for sf in corpus.files:
            for top in sf.forms:
                if top.kind == edn.LIST and top.head is not None and top.head.is_sym("defroute"):
                    ref = Ref("route", "web", top.items[1].name)
                    model.add(Element(ref, sf.rel, top.line, plugin=self.name))


plugin = RoutesPlugin
```

- `corpus.files` holds the parsed files (`rel`, `ns`, `aliases`, `forms`).
- `corpus.resolve`, `corpus.definition` and `corpus.calls` resolve symbols across namespaces.
- `graphify.domain.evaluate.Evaluator` computes constant values of forms.
- `graphify.domain.malli.MalliConverter` converts Malli forms to JSON Schema.
- After all plugins run, graphify reports a `missing-<kind>` error for each relation whose
  target is not defined. Use `finalize` for checks that need the complete model.

Graphify loads plugins from these places. A later plugin with the same name replaces an earlier one.

1. The built-in plugins.
2. Installed packages with a `graphify.domain_plugins` entry point.
3. Python files in `<project>/.graphify/plugins/`.
4. The specs in `GRAPHIFY_PLUGINS`, separated by `:` (`;` on Windows).

A spec is a plugin name, `module:attribute`, `module`, a `.py` file or a directory.
