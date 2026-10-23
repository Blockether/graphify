"""Write the knowledge base as one read-only HTML file.

The file embeds the viewer from ``graphify/kb_ui`` and a JSON copy of the
graph, the ontology, the findings and the documents. It needs no server.
Document assets stay in ``kb/documents`` and load with relative links, so
keep the file next to the ``kb`` folder (the default place).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from graphify.kb import KnowledgeBase

UI_DIR = Path(__file__).with_name("kb_ui")

# Node fields that the viewer shows in the read-only inspector.
_DETAIL_SKIP = {"id", "label", "_origin", "_src", "_tgt", "norm_label"}
_MAX_VALUE = 400
_MAX_FIELDS = 24


def _short(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _MAX_VALUE else value[: _MAX_VALUE - 1] + "\u2026"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = json.dumps(value, ensure_ascii=False, default=str)
    return _short(text) if len(text) > _MAX_VALUE else value


def _details(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes:
        row: dict[str, Any] = {}
        for key, value in node.items():
            if key in _DETAIL_SKIP or value in (None, "", [], {}):
                continue
            row[key] = _short(value)
            if len(row) >= _MAX_FIELDS:
                break
        out.append(row)
    return out


def _script_json(payload: Any) -> str:
    """Encode JSON so that it is safe inside a ``<script>`` element."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") \
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _inline_text(text: str, tag: str) -> str:
    # A closing tag inside inline CSS or JS would end the element early.
    return re.sub(rf"</({tag})", r"<\\/\1", text, flags=re.IGNORECASE)


def static_payload(kb: KnowledgeBase, *, name: str | None = None) -> dict[str, Any]:
    """Collect the data that the read-only viewer needs."""
    with kb._lock:
        view = kb.graph_view()
        nodes = list(kb.data["nodes"])
    documents = []
    for meta in kb.list_documents():
        try:
            documents.append(kb.get_document(str(meta["id"])))
        except Exception:  # noqa: BLE001 - a broken document must not stop the export
            continue
    return {
        "name": name or kb.out_dir.parent.name or "Knowledge base",
        "view": view,
        "ontology": kb.ontology_view(),
        "report": kb.validate(limit=2000),
        "documents": documents,
        "details": _details(nodes),
    }


def render_static_html(kb: KnowledgeBase, *, name: str | None = None) -> str:
    """Return the complete read-only HTML page as text."""
    payload = static_payload(kb, name=name)
    page = (UI_DIR / "index.html").read_text(encoding="utf-8")
    css = _inline_text((UI_DIR / "style.css").read_text(encoding="utf-8"), "style")
    js = _inline_text((UI_DIR / "app.js").read_text(encoding="utf-8"), "script")
    title = f"{payload['name']} \u00b7 graphify knowledge base"
    page = page.replace(
        "<title>graphify knowledge base</title>",
        "<title>" + title.replace("&", "&amp;").replace("<", "&lt;") + "</title>",
    )
    page = page.replace('<link rel="stylesheet" href="/ui/style.css">', f"<style>\n{css}\n</style>")
    data = f'<script id="kb-data" type="application/json">{_script_json(payload)}</script>'
    page = page.replace('<script src="/ui/app.js"></script>', f"{data}\n<script>\n{js}\n</script>")
    # The raw API links in <noscript> do not exist without a server.
    page = re.sub(
        r"Turn on JavaScript, or read the raw data at .*?</p>",
        "Turn on JavaScript to read it.</p>",
        page,
        flags=re.DOTALL,
    )
    return page


def write_static_html(kb: KnowledgeBase, path: str | Path | None = None, *, name: str | None = None) -> Path:
    """Write the read-only page and return its path (default ``<out>/kb.html``)."""
    target = Path(path) if path else kb.out_dir / "kb.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render_static_html(kb, name=name), encoding="utf-8")
    tmp.replace(target)
    return target
