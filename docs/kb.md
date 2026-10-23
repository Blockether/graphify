# Knowledge base

`graphify kb` turns `graphify-out/graph.json` into a knowledge base that you can read, correct and grow.

## When to use

- **You want to fix or add nodes and edges by hand** and keep the change after the next `graphify update`. Use [the admin interface](#the-admin-interface).
- **You want to know where the graph is inconsistent.** Use [Ontology and findings](#ontology-and-findings).
- **You want notes, PDFs, pages or images next to the code graph.** Use [Documents](#documents).
- **An agent or a script must read or change the graph.** Use [the HTTP API](#http-api).
- **You want to share a view without a server.** Use [Static export](#static-export).

For the code graph itself, read [How it works](how-it-works.md). For framework flows, read [Domain flows](domain.md).

## The admin interface

```bash
graphify kb serve            # http://127.0.0.1:7878
graphify kb serve --open     # also open the browser
```

The page has five views:

- **Graph**: the whole graph as a map. Colour nodes by class or community, hide classes and relations, and search with `/` or Cmd+K. Click a node to open the inspector. There you rename it, set attributes, add or remove connections, or delete it.
- **Ontology**: classes, relations and their domain and range as a map and as tables. Edit them in place, or infer them again from the graph.
- **Findings**: every place where the graph does not fit the ontology. Click a row to open the node.
- **Documents**: Markdown boxes with images, PDFs and other files. `[[Label]]` links a node; `[[Label|class]]` also creates it.
- **History**: every change with its author. Undo walks back one change at a time.

Several browsers and agents can work at the same time. Each page updates live.

## Where the data lives

| Path | Content |
|---|---|
| `graphify-out/graph.json` | The graph. The server writes your edits into it. |
| `graphify-out/ontology.json` | The ontology. Without this file, graphify infers one from the graph. |
| `graphify-out/kb/overlay.json` | Your edits. They are applied again after `graphify update` rebuilds the graph. |
| `graphify-out/kb/history.jsonl` | One line for each change, with the data to undo it. |
| `graphify-out/kb/documents/<id>/` | `meta.json`, `body.md` and `assets/` of one document. |

Nodes that you add get ids that start with `kb:`. Document nodes start with `doc:`.

## Ontology and findings

The ontology names the classes of nodes (the `file_type` field by default) and the relations between them. A class can have a parent and properties with a type and a required flag. A relation has a domain, a range, a cardinality (`many` or `one`), and can be symmetric.

```bash
graphify kb infer --save        # start from the graph as it is
graphify kb validate            # errors, warnings and notes
graphify kb validate --strict   # exit 1 on an error, for CI
```

The checks include unknown classes and relations, missing or mistyped required properties, domain and range violations, cardinality, asymmetric symmetric relations, duplicate ids and labels, dangling edges and nodes without edges. With `strict: true` in the ontology, unknown classes and relations are errors instead of warnings.

## Documents

```bash
graphify kb ingest notes.md design.pdf diagram.png https://example.com/adr-12
graphify kb ingest meeting.md --llm --tag meeting
```

- Markdown and text stay as they are. HTML becomes Markdown. A PDF gives its text (install `pypdf`) and stays attached.
- An image becomes an attached file of a new document.
- Without `--llm`, graphify links the document to every node whose label appears in the text.
- With `--llm`, the configured LLM backend also extracts new nodes and edges from the text. This costs model calls.

## HTTP API

All routes return JSON. Write requests need the header `X-Graphify-KB: 1` or a bearer token. Send `X-Graphify-Actor: <name>` to name the author in the history.

| Method and path | Purpose |
|---|---|
| `GET /api/health`, `GET /api/stats` | Liveness and counts |
| `GET /api/graph` | Compact graph for viewers |
| `GET /api/search?q=&limit=&class=` | Nodes and documents |
| `GET /api/nodes/<id>`, `GET /api/nodes?label=` | One node with its connections |
| `POST /api/nodes`, `PATCH /api/nodes/<id>`, `DELETE /api/nodes/<id>` | Add, change (`{"attrs": {...}}`, `null` removes) or delete a node |
| `POST /api/edges`, `PATCH`/`DELETE /api/edges/<id>` | Add, change or delete an edge |
| `POST /api/batch` | `{"ops": [...]}`, all or nothing |
| `GET`/`PUT /api/ontology`, `POST /api/ontology/infer` | Read, replace or infer the ontology |
| `PUT`/`DELETE /api/ontology/classes/<name>` and `/relations/<name>` | Edit one class or relation |
| `GET /api/validate?limit=&info=` | Findings |
| `GET`/`POST /api/documents`, `GET`/`PATCH`/`DELETE /api/documents/<id>` | Documents |
| `POST /api/documents/<id>/assets?name=`, `GET`/`DELETE .../assets/<name>` | Files of a document |
| `POST /api/ingest` | `{"name", "text" or "data_base64" or "url", "mode": "text" or "llm"}` |
| `GET /api/history`, `POST /api/undo` | History and undo |
| `POST /api/publish` | Write the static page into `graphify-out` |
| `GET /api/events` | Server-sent events for each change |

Example:

```bash
curl -s -X POST http://127.0.0.1:7878/api/nodes \
  -H 'X-Graphify-KB: 1' -H 'X-Graphify-Actor: me' -H 'Content-Type: application/json' \
  -d '{"label": "Payment service", "class": "concept", "attrs": {"owner": "billing"}}'
```

### Security

- The server listens on `127.0.0.1` by default. For any other address, give `--token` or `GRAPHIFY_KB_TOKEN`.
- With a token, send `Authorization: Bearer <token>`. The browser signs in once and keeps a cookie.
- `--read-only` refuses every write. Requests with an unknown `Host` header fail.

## Static export

```bash
graphify kb export        # graphify-out/kb.html
```

The page holds the graph, the ontology, the findings and the documents. It opens from disk without a server. Keep it in `graphify-out`, because images load from `kb/documents`.
