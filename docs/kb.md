# Knowledge base

`graphify kb` turns `graphify-out/graph.json` into a knowledge base that you can read, correct and grow. Your notes, papers and edits go into the same graph as the code. A document links to the functions, classes and concepts that it names.

## When to use

- **You want to fix or add nodes and edges by hand**, and keep them after the next `graphify update`. Use [the admin interface](#the-admin-interface).
- **You want notes, PDFs, web pages or images next to the code graph.** Use [Documents](#documents).
- **A document links to the wrong node, or misses a link.** Read [How documents link to the graph](#how-documents-link-to-the-graph).
- **You want to know where the graph is inconsistent.** Use [Ontology and findings](#ontology-and-findings).
- **An agent or a script must read or change the graph.** Use [the HTTP API](#http-api).
- **You want to share a view without a server.** Use [Static export](#static-export).

For the code graph itself, read [How it works](how-it-works.md). For framework flows, read [Domain flows](domain.md).

## The admin interface

Start the server in the project that holds `graphify-out/`:

```bash
graphify kb serve            # http://127.0.0.1:7878
graphify kb serve --open     # also open the browser
```

The page has five views:

- **Graph**: a map of the whole graph. See [Read a large graph](#read-a-large-graph).
- **Ontology**: the classes and relations, with their domain and range, as a map and as tables. Edit them in place, or infer them again from the graph.
- **Findings**: every place where the graph does not fit the ontology. Click a row to open its node.
- **Documents**: Markdown pages with images, PDFs and other files. **Ingest** adds files, web pages and pasted text. The list shows how many names of each document wait for review. Decide them in the **Review** section of the document.
- **History**: every change with its author. Undo one change from its row, or undo the latest change. The **Undo** button in the message after a change undoes only that change, also when somebody else changed the graph later. The message stays for 10 seconds, and longer while the pointer or the keyboard focus is on it. Escape closes it.

Several browsers, agents and `graphify kb` commands can work on one knowledge base at the same time. Each page updates live.

### Read a large graph

A graph of 50,000 nodes is too large to read as one picture. The map therefore starts with an overview and shows details on demand:

1. The first view shows one circle for each community. A line between two circles shows how many edges join them.
2. Click a community to open it. Its nodes and the edges between them appear.
3. Zoom in to see more labels and the edges of the nodes in view.
4. Click a node to open the inspector. There you rename it, set attributes, add or remove connections, or delete it.

Nodes with only one link sit folded under their neighbour. A badge such as `+12` shows the count. Click the neighbour to show them. The layout uses a fixed seed, so the same graph gives the same map at each load.

Search with `/` or Cmd+K. Under **Colour by**, colour the nodes by class or by community. Names that code uses, but that graphify found no source for, are grey and labelled **Unresolved**.

### Use it on a tablet or a phone

The interface works with touch on a tablet and a phone.

- Pinch to zoom and drag to pan the map.
- On a phone, the inspector opens as a sheet from the bottom of the screen. The map settings open as a drawer.
- To ingest a file, use the file picker. Drag and drop works only with a mouse.

## Where the data lives

| Path | Content |
|---|---|
| `graphify-out/graph.json` | The graph. KB nodes and edges are part of it, next to the code nodes. |
| `graphify-out/ontology.json` | The ontology. Without this file, graphify infers one from the graph. |
| `graphify-out/kb/store.sqlite` | Your edits, the node moves that wait for a rebuild, and the change history with the data to undo each change. `graphify update`, `graphify extract` and `graphify watch` apply the edits again after each rebuild. |
| `graphify-out/kb/generation.json` | The version number of the knowledge base. It only grows, also across restarts. |
| `graphify-out/kb/documents/<id>/` | `meta.json`, `body.md` and `assets/` of one document. |
| `graphify-out/kb/trash/` | Deleted documents and files, so that an undo can restore them. |

`kb/store.sqlite` is one SQLite file. A change writes only the records that it touches, so a change stays fast in a large store. From 1,000 records, the store compresses each record with zstd and a dictionary that it makes from its own records. A store from an older graphify version keeps its edits in `kb/overlay.json` and its history in `kb/history.jsonl`. The first load moves them into `kb/store.sqlite` and renames the old files to `*.migrated`.

Nodes that you add get ids that start with `kb:`. Document nodes start with `doc:`. A new KB node joins the community that most of its neighbours belong to.

An edit of an extracted node keeps the file, the label and the class of that node. A KB edge keeps them for each extracted end node. When a rebuild gives the node a new id, the edit, the edge and the document links move to the new id. graphify looks for one node with the same file, label and class. When the file was moved or renamed, one node with that label and class in the whole graph also counts. Otherwise the record waits, and the `stale_overlay_patch` or `stale_overlay_edge` finding names it.

A rename changes the label, so this search cannot find the node. After a rename, send `{"op": "move_node", "from": "<old id>", "to": "<new id>"}` to `POST /api/batch`. Without a server, run `graphify kb move <old id> <new id>`; it takes more pairs too. The edits, edges, deletions and document links of the old id then move to the new id. While the graph still has the old id, the move waits for the next rebuild (`"status": "pending"`). When the graph has the new code already, the records move at once (`"moved"`). When the graph does not have the old id and no record names it, nothing changes (`"skipped"`). An undo removes a move that waits, but records that moved stay at the new id.

```bash
graphify kb move app_old_name app_new_name        # after a rename
graphify kb move lib_util_parse lib_text_parse --json
```

graphify keeps one edge between two nodes. If an edge already joins two nodes, a second edge between them fails with a conflict. Change the existing edge instead.

## Documents

A document is a Markdown page with optional files. Its node has the class `document.page`, and its links to other nodes have the relation `mentions`.

To add a document in the interface:

1. Open **Documents** and click **Ingest**.
2. Drop files, pick files, enter a web address, or paste text.
3. Optional: add tags, turn off linking, or turn on LLM extraction.
4. Open the new document. The **Linked nodes** list shows the nodes that it links to.

To write a page yourself, click **New document**. Its **Link mentions** switch is on by default, so graphify links the names in the text. When you change the title, graphify links the document again.

The same from the command line:

```bash
graphify kb ingest notes.md design.pdf diagram.png https://example.com/adr-12
graphify kb ingest meeting.md --tag meeting
graphify kb ingest https://github.com/acme/app/blob/main/docs/auth.md --no-link
```

Expected output for each source:

```text
Ingested notes.md -> document notes (7 links, 0 nodes, 0 edges)
```

### Supported files

graphify reads the type from the bytes first, then from the `Content-Type`, then from the file name. A renamed file therefore still works.

| Input | Result |
|---|---|
| Markdown or plain text | The text as it is. |
| HTML or a web page | The page as Markdown. Install `markdownify` for the best result. |
| PDF | The text page by page, and the PDF as an attached file. Install `pypdf` to read the text. |
| DOCX | The text as Markdown, and the file as an attachment. `python-docx` gives the best result. |
| Program code, such as `.py`, `.ts`, `.clj` or a script that starts with `#!` | The code as one code block with its language and file name. The title is the file name. See [Code documents](#code-documents). |
| PNG, JPEG, GIF, WebP, SVG, AVIF | The image as an attached file. |

When an optional converter is missing, graphify uses a plain fallback and says so in the result. Other rules:

- An empty file gives `400` with the message `<name> is empty`.
- An unsupported type, such as a ZIP file or an executable, gives `415` with the list of supported types.
- A file over 64 MB gives `413`.
- A DOCX file that is not valid is stored as an attached file without its text. The result says why.
- graphify stores a plain ASCII file name: `Zażółć.pdf` becomes `Zazolc.pdf`. A name without Latin letters becomes a hash, such as `file-a5e9e445.png`.
- graphify does not use the device names that Windows reserves, such as `CON` and `NUL`. A file `con.txt` is stored as `con-doc.txt`.
- A second file with the same name in one document gets a number: `same.png`, then `same-2.png` and `same-3.png`.
- The title is the first heading of the text. A PDF without one uses the heading on page 1, then its metadata title, then the file name. The page-1 heading is the largest text, at least 1.2 times the size of the body text. graphify ignores a metadata title that a program set, such as `untitled` or `Microsoft Word - a.docx`. It never uses the first line of text.

### Web pages

graphify fetches a web address and reads it like a file.

- A GitHub file page (`github.com/<owner>/<repo>/blob/...`) is fetched as its raw file.
- Addresses on your own machine or private network are refused. This blocks requests to internal services (server-side request forgery).
- A server that does not answer gives `502`. Nothing is written.

### LLM extraction

With `--llm` (or the LLM option in the interface), the configured LLM backend also extracts nodes and edges from the text. This costs model calls. Choose the model with `--backend` and `--model`.

- graphify sends a long document in parts of at most 60,000 characters, with one model call for each part. Two parts share 400 characters, so a fact at a cut stays whole. `llm.chunks` in the result gives the number of parts.
- Each fact must come with a quote from the document. graphify drops a fact when the quote is not in the text (Text2KGBench, Mihindukulasooriya et al., ISWC 2023).
- A name reuses an existing node only when the words are the same and the classes fit. Case and separators can differ. One node must clearly win.
- A near spelling, or a name that fits several nodes, goes to review with the reason `possible_match`. graphify does not write that fact.
- A concept never merges into a code node. When a concept has the name of a code node, graphify creates a new node. An `AMBIGUOUS` `mentions` edge joins the two nodes, and the pair goes to review with `possible_same_as`.
- Other names become new nodes.
- Review items are in the `possible` list of the document, next to the review items of the linker.
- An edge that breaks the ontology is marked `AMBIGUOUS` and gets a finding.
- Each LLM edge is `INFERRED` and stores the document, the text offset, the backend and the model.
- If the backend does not answer, the request fails with `502`. graphify calls the model before it writes, so nothing changes.

## How documents link to the graph

When linking is on, graphify finds the names of graph nodes in the document text. It then makes one of three decisions for each name:

- **Link**: the name fits one node, or the context clearly picks one. graphify adds a `mentions` edge.
- **Review**: the name fits several nodes, and the context does not decide. graphify adds no edge. The name goes to the `possible` list of the document, with up to 5 candidates.
- **No link**: the text is not a name, for example a common word.

These three bands follow the record-linkage model of Fellegi and Sunter (1969).

### What links automatically

A text is a name only when its shape says so:

- a code span, such as `` `start_gateway` ``;
- an identifier, such as `snake_case`, `CamelCase`, `lease()`, `app.service` or `app/util.py`;
- a label of two or more words, such as "Payment service".

A plain lower-case word never links. When nodes in three or more files have the same label, that label is a common word, not a name. This rule is a simple form of the keyphraseness filter of Mihalcea and Csomai (2007). A common code name with many matches, such as `get()`, links only when the words in the same sentence or paragraph pick one node. Code names match with case.

Each automatic link is `INFERRED` and has a score that says how sure graphify is:

| Score | Evidence |
|---|---|
| 0.95 | A unique code name, code span or path, or the file of a [code document](#code-documents). A qualifier such as `gateway-client/` in `gateway-client/request!` can also name the file of the node. |
| 0.75 | A unique label of two or more words, or the only match in a module that the text names. |
| 0.65 | A unique heading label. |
| 0.55 | The context picked the node, or a weak name with support from the context. |

### Code documents

A code document is one fenced code block, such as an uploaded `relay.clj`. graphify reads the code in it, not prose:

- The document links the file of the same name with score 0.95. The name comes from `title="relay.clj"` in the fence line, or else from the document title.
- When several files have that name, graphify adds no file link. The name goes to review with the reason `same_file_name`.
- Names with a code shape, such as `wire/json-str` or `PUBLISHER_RELAY_URL`, link with their usual score. Plain words do not link, because in code they are mostly in comments.
- Names that the file defines itself do not link again. The file link covers them.
- A name that fits several nodes gets no review item, because a large file would fill the review list.

In other documents, names in fenced code blocks do not link. In a test on 38 pages with code blocks, linking them lost 19 correct links and added wrong ones.

The `:as` aliases of a Clojure `require` in the text decide qualified names. After `[babashka.http-client :as http]`, `http/client` links only to a node in `babashka.http-client`.

### Why a name goes to review

When a name fits several nodes, graphify compares each candidate with the nodes that the document already links without doubt (Milne and Witten 2008). It counts shared neighbours, the same directory and the same community. Words near the name that match a module or a path also count. A candidate in a test file counts half, because a note rarely means a test. The winner must lead by half of its own score. Otherwise the name goes to review with one of these reasons:

| Reason | Meaning |
|---|---|
| `low_margin` | Two or more candidates are almost equally likely. |
| `no_context` | The document gives no evidence for any candidate. |
| `weak_name` | The name is weak, for example one capitalised word, and the context does not support it. |
| `ambiguous_context` | Words near the name point to different candidates. |
| `ambiguous_wiki` | A `[[wiki link]]` fits several nodes. |
| `test_file` | The only candidate is in a test file, and the name is weak. |
| `same_file_name` | A [code document](#code-documents) has the name of several files. |

One name keeps one meaning in a paragraph. A document can still compare two nodes with the same name in different paragraphs.

Example: the text "The `lease()` call renews the gateway lease" links `lease()` with score 0.95 when only one function has that name. If `Gateway.lease()` and `Pool.lease()` both exist, and the text names neither class, the name goes to review with `no_context`.

### Decide a name in review

Open the document and find its **Review** section. Each row shows the name, the text around it, the reason and the candidates with their scores.

- Click **Link** on the right candidate. graphify links the name to that node with confidence `EXTRACTED`, like a wiki link.
- Click **Not a link** when no candidate is right. graphify adds no edge.

graphify keeps each decision in the `decisions` field of the document. Later edits and new linking runs do not ask about that name again. A decision stops every automatic link of that name. After **Link**, only the chosen node is linked. After **Not a link**, no node is linked. A `[[wiki link]]` in the text still links the name.

A decision is also evidence for the other names in the document. For example, after you link `init!` to a node in `tui/client.clj`, other names from that file can link automatically.

A link decision lasts while its node is linked to the document and exists in the graph. If you remove that node from the document, the decision goes too. graphify then decides the name again from the current text and links. The name can link automatically or come back to review.

To remove a wrong link, select its `mentions` edge in the inspector and click **Remove**. graphify changes the document, so the link does not come back:

- A node that you linked leaves the document, and its decision goes too.
- An automatic link becomes **Not a link** for its name.
- A wiki link stays, because it is in the text. Edit the text instead. **Remove** answers `409`.

Each decision is one change in the history. The **Undo** button in the message after the decision puts the name back in review. Programs send the same decisions through the HTTP API. See [Review decisions](#review-decisions).

### Force a link with a wiki link

Write the link yourself when the automatic decision is wrong or missing:

- `[[Payment service]]` links the node with that label.
- `[[Payment service|concept]]` links only a node of class `concept` or of one of its subclasses. For example, `[[run|code]]` finds a `code.method` node.
- `[[kb:payment_service]]` links the node with that exact id.

The result depends on the matches:

- One match: graphify links it with confidence `EXTRACTED`.
- No match: graphify creates a new node with that label, and links it.
- Several matches: graphify adds no link and creates no node. The link goes to review with `ambiguous_wiki`. Add the class or use the node id.

Wiki links in fenced code blocks are text, not links. To turn off automatic linking for a document, use `--no-link`. Wiki links still work.

The tests check the linker with labelled notes on a real graph, in `tests/fixtures/kb_link/`:

| Set | Notes | Links | Names that must not link | Precision | Recall | Names left unlinked |
|---|---|---|---|---|---|---|
| Tuning set | 5 | 50 | 19 | 1.00 | 1.00 | 19 of 19 |
| Hard set: Polish text, short names | 2 | 9 | 15 | 0.90 | 1.00 | 14 of 15 |

A reviewer judged the one extra link in the hard set correct. The sets are small. Use the numbers as a regression check, not as a general accuracy.

## Ontology and findings

The ontology names the classes of nodes and the relations between them. A class can have a parent and properties with a type and a required flag. A relation has a domain, a range and a cardinality (`many` or `one`), and can be symmetric.

The classes follow graphify's own node types:

| Root class | Subclasses |
|---|---|
| `code` | `code.file`, `code.module`, `code.package`, `code.namespace`, `code.class`, `code.function` (with `code.method`, `code.test`, `code.macro`), `code.variable`, `code.entrypoint`, `code.unresolved` |
| `document` | `document.page`, `document.section` |
| `concept` | `concept.external` |
| `paper`, `image`, `rationale` | none |

A node's `class` attribute wins over the automatic class. `code.unresolved` is a name that code uses, but graphify found no source for it.

```bash
graphify kb infer --save        # write graphify-out/ontology.json from the graph
graphify kb validate            # errors, warnings and info
graphify kb validate --strict   # exit 1 on an error, for CI
```

### Declared and inferred rules

Each rule has an origin, a severity, a support (the number of edges or nodes behind it) and a confidence.

- **Declared** rules are rules that you write. A break is an error.
- **Inferred** rules come from `graphify kb infer`. A break is at most a warning, because the graph itself made the rule.

`graphify kb infer` follows the support and confidence method of QSE (Rabbani et al., PVLDB 2023):

- A relation with fewer than 30 edges keeps no domain or range.
- A class joins a domain or range only with at least 10 distinct nodes and a share of at least 0.01.
- An edge end that fits the rule with a score below 0.01 gives a warning. A score from 0.01 to 0.05 gives info.
- A required edge (minimum count 1) needs at least 50 members. It gives a warning at confidence 0.99, and info from 0.95.

An `ontology.json` of version 1 still loads. Its rules count as inferred.

### Finding codes

| Code | Severity | Meaning |
|---|---|---|
| `unlikely_domain`, `unlikely_range` | warning or info | An edge starts or ends at a class that the relation rarely uses. |
| `missing_edge` | warning or info | A node lacks an edge that almost all nodes of its class have. |
| `in_cardinality` | warning | A node has more incoming edges than the relation allows. |
| `cycle` | error | `contains` or `inherits` forms a loop. |
| `duplicate_entity` | warning or info | Nodes of one class have the same name. One candidate is a warning, more are info. |
| `invalid_confidence`, `confidence_mismatch` | warning or info | The edge confidence or its score breaks the rubric below. |
| `nonconforming_extraction` | warning or error | An LLM edge does not fit the ontology. The ingest result lists it. |
| `orphan_node` | info | A KB node has no edges. |
| `stale_overlay_patch` | warning | An edit waits: no node has its id now, and no single node fits its file, label and class. |
| `rekeyed_overlay_patch` | info | An edit moved to a new node id after a rebuild. Check that the new node is the right one. |
| `rekeyed_overlay_edge` | info | A KB edge moved to new node ids after a rebuild. Check that the new nodes are the right ones. |
| `moved_overlay_node` | info | The records of a node moved to its new id after a `move_node` operation. |
| `pending_overlay_move` | info | A `move_node` operation waits for a rebuild that removes the old id. |
| `stale_overlay_edge` | warning | A KB edge waits: the graph has no node with its id now, and no single node fits its file, label and class. |
| `overlay_conflict` | warning | A KB edge stays out, because another edge already joins the two nodes. |
| `dangling_edge`, `duplicate_id` | error | An edge names a missing node, or an id appears twice. |
| `unknown_class`, `unknown_relation` | warning | The ontology does not define the class or relation. With `strict: true` in the ontology, this is an error. |

The confidence rubric is the same as graphify's:

- `EXTRACTED`: score 1.0.
- `INFERRED`: a score above 0.4 and below 1.0, never 0.5. Usual values are 0.55, 0.65, 0.75, 0.85 and 0.95.
- `AMBIGUOUS`: a score from 0.1 to 0.3.

## Change history and undo

Every change is one entry in the history of `kb/store.sqlite`, with its author and the steps to undo it.

- Undo the latest change at any time.
- Undo an older change only when no later change touched the same records. Otherwise the undo fails with `409`, and the message names the later changes. Undo those first.
- An undo that would bring back an extracted node that a rebuild removed fails with `409`. The code no longer has that node.
- An undo is a new history entry, so the history only grows.

The server and the `graphify kb` commands share a file lock in `kb/.lock`. Each write reads the changes of other processes first. A write waits up to 30 seconds for the lock, then fails with `503`.

## HTTP API

This section is a reference for agents and scripts. All routes return JSON.

- Send the header `X-Graphify-KB: 1` or a bearer token with each write.
- Send `X-Graphify-Actor: <name>` to name the author in the history.
- A successful write answers with the header `X-Graphify-KB-Change: <seq>`. A change in several steps gives a list, such as `41,42`. To undo exactly that change, send `POST /api/undo` with `{"seq": N}`, newest first.

### Routes

| Method and path | Purpose |
|---|---|
| `GET /api/health` | Liveness, version, and whether a token or read-only mode is on. No token needed. |
| `GET /api/stats` | Counts. |
| `GET /api/overview` | One row for each community, and the edge counts between communities. |
| `GET /api/community/<id>` | The nodes and inner edges of one community, and its edges to other communities. |
| `GET /api/graph` | The whole graph in a compact form. |
| `GET /api/search?q=&limit=&class=` | Nodes and documents. `class` also finds the nodes of its subclasses. |
| `GET /api/nodes/<id>`, `GET /api/nodes?label=` | One node with its connections. |
| `POST /api/nodes`, `PATCH /api/nodes/<id>`, `DELETE /api/nodes/<id>` | Add, change (`{"attrs": {...}}`, `null` removes a key) or delete a node. |
| `POST /api/edges`, `PATCH`/`DELETE /api/edges/<id>` | Add, change or delete an edge. |
| `POST /api/batch` | `{"ops": [...]}`, all or nothing. `move_node` moves the records of a renamed node. |
| `GET`/`PUT /api/ontology`, `POST /api/ontology/infer` | Read, replace or infer the ontology. |
| `PUT`/`DELETE /api/ontology/classes/<name>` and `/relations/<name>` | Edit one class or relation. |
| `GET /api/validate?limit=&info=` | Findings. `info=0` leaves out info findings. |
| `GET`/`POST /api/documents`, `GET`/`PATCH`/`DELETE /api/documents/<id>` | Documents. A new document links names unless the body has `"link": false`. |
| `POST /api/documents/<id>/assets?name=`, `GET`/`DELETE .../assets/<name>` | Files of a document. |
| `POST /api/documents/<id>/review` | Decide a name in review. See [Review decisions](#review-decisions). |
| `POST /api/ingest` | Add a file, text or web page. See [Ingest](#ingest). |
| `GET /api/history?limit=`, `POST /api/undo` | History, and undo of the latest change or of `{"seq": N}`. |
| `POST /api/publish` | Write the static page. See [Static export](#static-export). |
| `GET /api/export?assets=1` | The files of the store, for a copy. See [Copy a store](#copy-a-store). |
| `POST /api/flush` | Write pending changes to `graph.json` now. |
| `GET /api/events` | Server-sent events for each change. |
| `POST /api/login`, `POST /api/logout` | Start or end a browser session. See [Security](#security). |

### Example

```bash
curl -s -X POST http://127.0.0.1:7878/api/nodes \
  -H 'X-Graphify-KB: 1' -H 'X-Graphify-Actor: me' -H 'Content-Type: application/json' \
  -d '{"label": "Payment service", "class": "concept", "attrs": {"owner": "billing"}}'
```

The answer is `201` with the new node. Its id is `kb:payment_service`.

### Ingest

Send JSON, or send the raw file as the body:

```bash
# JSON: one of "text", "data_base64" or "url"
curl -s -X POST http://127.0.0.1:7878/api/ingest -H 'X-Graphify-KB: 1' \
  -H 'Content-Type: application/json' \
  -d '{"name": "notes.md", "text": "# Lease policy\nThe `lease()` call renews the gateway lease.", "tags": ["ops"]}'

# Raw body: options go in the query
curl -s -X POST 'http://127.0.0.1:7878/api/ingest?name=design.pdf&tags=design&link=1' \
  -H 'X-Graphify-KB: 1' -H 'Content-Type: application/pdf' --data-binary @design.pdf
```

Other fields are `mode` (`text` or `llm`), `title`, `link` (`false` turns off linking), `backend` and `model`. The answer is `201` with:

- `document`: the new document;
- `linked`: the number of automatic links;
- `possible`: the names for review, with their reason and candidates;
- `nodes_added`, `edges_added`, `findings`, `dropped`: the results of LLM extraction;
- `notes` and `missing_converters`: what graphify could not convert fully.

### Review decisions

Send one decision for a name in the `possible` list of a document:

```bash
# Link the name "lease()" to a node
curl -s -X POST http://127.0.0.1:7878/api/documents/lease-policy/review -H 'X-Graphify-KB: 1' \
  -H 'Content-Type: application/json' -d '{"mention": "lease()", "node": "gateway_lease"}'

# Mark the name as not a link
curl -s -X POST http://127.0.0.1:7878/api/documents/lease-policy/review -H 'X-Graphify-KB: 1' \
  -H 'Content-Type: application/json' -d '{"mention": "lease()", "node": null}'
```

The answer is `200` with the document. The name is no longer in `possible`, and `decisions` holds the node id, or `null` for not a link. A name that is not in review gives `400`. An unknown document or node gives `404`. In `GET /api/documents`, the `review` field of each row counts the names in review.

`DELETE /api/edges/<id>` on a `mentions` edge of a document changes the document, as **Remove** does in the interface. A wiki link gives `409`.

### Overview and community

`GET /api/overview` answers:

```json
{"version": 42,
 "communities": [{"id": 3, "name": "Gateway", "size": 812,
                  "classes": {"code.function": 410, "code.file": 38},
                  "top": [{"id": "service_start_gateway", "label": "start_gateway()", "class": "code.function", "degree": 57}]}],
 "links": [{"source": 3, "target": 7, "weight": 120}]}
```

- `top` holds at most 5 nodes with the highest degree.
- In `links`, `source` is smaller than `target`, and a community has no link to itself.
- Nodes without a community are in community `-1`, **Unassigned**.

`GET /api/community/<id>` answers `version`, `id`, `name`, `nodes`, `edges`, `relations` and `boundary`. The `nodes` rows have the format of `/api/graph`. `boundary` lists edges to other communities, at most 2,000. `boundary_total` and `boundary_truncated` tell you when the list is cut.

On the 47,817-node Vis graph, the overview is 126 KB with gzip, and the largest community is 24 KB. The full `/api/graph` is 1.68 MB.

### Copy a store

`GET /api/export` answers the files of the store, so that you can copy it to another folder or machine:

```json
{"format": "graphify-kb-export", "version": 42, "exported_at": "2026-10-24T09:12:03Z",
 "files": [{"path": "graph.json", "json": {"nodes": [], "links": []}},
           {"path": "kb/overlay.json", "json": {"nodes": {}, "edges": {}}},
           {"path": "kb/documents/notes/meta.json", "text": "{...}"},
           {"path": "kb/documents/notes/body.md", "text": "# Notes"}]}
```

- Each `path` is relative to the output folder. Write each file there to make a copy. Then `graphify kb serve copy/graph.json --read-only` serves it.
- A JSON file comes as `json`, a text file as `text`.
- `ontology.json` is in the list only when the store has one. Without it, the copy infers the same ontology.
- `assets=1` adds the files of the documents in base64, such as `{"path": "kb/documents/notes/assets/plan.png", "base64": "..."}`. Without it, the answer is smaller, but the copy has no images or attachments.
- The history, the trash and the lock stay out. A copy starts with an empty history.
- The first load of the copy moves `kb/overlay.json` into `kb/store.sqlite`.

### Caching

`/api/graph`, `/api/overview` and `/api/community/<id>` send a weak `ETag` and `Cache-Control: private, no-cache`. Send the ETag back in `If-None-Match` to get `304 Not Modified` while the graph is unchanged. The server also accepts a list of tags and `*`.

### Status codes

| Status | Cause |
|---|---|
| `400` | A bad value, such as `limit=abc`, an empty file or an unsafe publish path. |
| `401` | A token is needed, or it is wrong. |
| `403` | A read-only server, a write without `X-Graphify-KB`, a browser write from another origin, or an unknown `Host` header. |
| `404` | No such node, edge, document, community or route. |
| `405` | The route does not accept the method. |
| `408` | The client sent no data for 30 seconds. The server closes the connection. |
| `409` | A conflict: the id is in use, another edge joins the two nodes, or a later change blocks an undo. |
| `413` | The body is too large. A file can be at most 64 MB. |
| `415` | The file type is not supported. |
| `429` | Too many failed logins. Wait for the time in `Retry-After`. |
| `501` | The body uses a transfer encoding other than `chunked`. |
| `502` | An outside service, such as the LLM backend or a web server, did not answer. |
| `503` | Another process held the store lock for more than 30 seconds. |

Each error body is `{"error": "<message>", "status": <code>}`. A `500` error gives a short message, and the details go to the server log.

### Security

- The server listens on `127.0.0.1` by default. For any other address, give `--token` or `GRAPHIFY_KB_TOKEN`.
- The token must have at least 16 characters. Otherwise the server does not start. Make a token with `python -c "import secrets; print(secrets.token_urlsafe(24))"`.
- With a token, every route except `/api/health` needs `Authorization: Bearer <token>` or the browser session cookie.
- The browser signs in once with `POST /api/login`. The cookie holds a random session id, never the token. The cookie is `HttpOnly` and `SameSite=Strict`. Over HTTPS it is also `Secure`.
- The server keeps the sessions in its memory for 7 days. `POST /api/logout` ends one session. A restart of the server ends all sessions.
- After 5 failed logins from one address, each further failure doubles the wait, from 1 second up to 5 minutes. A wrong bearer token counts as a failed login. During the wait, a wrong login gets `429`, but the correct token still works. The count starts again 15 minutes after the last failure.
- The `Host` header must name the server. This blocks DNS rebinding.
- A browser write must come from the server's own address. A write with the `Origin` header of another site gets `403`. The `--allow-host` names count as the server's own.
- A client that sends no data for 30 seconds loses the connection with `408`.
- `--read-only` refuses every write.
- On `SIGTERM` or `SIGHUP`, the server stops as on Ctrl+C. It writes `graph.json` before it exits.

On `0.0.0.0` or `::`, the server accepts IP addresses, loopback names and the machine name. Name each other host with `--allow-host`:

```bash
graphify kb serve --host 0.0.0.0 --token "$TOKEN" --allow-host kb.example.com
```

## Static export

```bash
graphify kb export                         # graphify-out/kb.html
graphify kb export --out graphify-out/share/kb.html
```

The page holds the graph, the ontology, the findings and the documents. It opens from disk without a server. Images and files load from `graphify-out/kb/documents/`, also when the page is in a subfolder of `graphify-out`. Copy that folder with the page when you share it.

`POST /api/publish` writes the same page from the server. Give a `path` in the body, or omit it for `graphify-out/kb.html`. It refuses a file that graphify owns, such as `graph.html`, files in `kb/` and the other graphify folders, and existing files that are not exports.
