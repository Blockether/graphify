"""Import and LLM ingest for knowledge base documents (graphify.kb_ingest).

The cases come from the E2E report (bugs 5, 6, 8, 15, 17, 19, 20) and from
memo F6 / R5 (grounded triples, entity resolution, ontology gate).
"""

import io
import json
import sys
import zipfile
from typing import Any

import pytest

from graphify import kb_ingest as KI
from graphify import kb_link as KL
from graphify.kb import KBError, Unavailable, UnsupportedType
from graphify.ontology import ClassSpec, Ontology, RelationSpec, with_kb_vocabulary


def _pdf(text: str, title: str | None = None, *, size: int = 12, body: str = "") -> bytes:
    """Build a small valid one-page PDF: one line of ``text`` at ``size``, then ``body`` at 12."""
    stream = f"BT /F1 {size} Tf 72 720 Td ({text}) Tj ET".encode()
    if body:
        stream += f" BT /F1 12 Tf 72 690 Td ({body}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if title:
        objs.append(f"<< /Title ({title}) >>".encode())
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    info = b" /Info %d 0 R" % len(objs) if title else b""
    out += b"trailer\n<< /Size %d /Root 1 0 R%s >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        info,
        xref,
    )
    return bytes(out)


def _docx(heading: str, para: str) -> bytes:
    """Build a minimal .docx with one heading and one paragraph (no python-docx)."""
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    doc = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{w}"><w:body>'
        f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{heading}</w:t></w:r></w:p>'
        f"<w:p><w:r><w:t>{para}</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        zf.writestr("word/document.xml", doc)
    return buf.getvalue()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


# -- names and titles (bugs 5, 20) ----------------------------------------


def test_split_name_keeps_only_real_suffixes():
    assert KI.split_name("notes.md") == ("notes", ".md")
    assert KI.split_name(".md") == ("", ".md")
    assert KI.split_name("1706.03762") == ("1706.03762", "")  # an arXiv id has no suffix
    assert KI.split_name("archive.tar.gz") == ("archive.tar", ".gz")


def test_safe_name_keeps_suffix_for_non_ascii_and_long_names():
    # Bug 5: non-ASCII and long names lost their suffix.
    assert KI.safe_name("Zażółć gęślą jaźń.md") == "Zazolc_gesla_jazn.md"
    assert KI.safe_name("é.md") == "e.md"
    a, b = KI.safe_name("日本語.png"), KI.safe_name("中文.png")
    assert a.endswith(".png") and b.endswith(".png") and a != b
    long = KI.safe_name("x" * 400 + ".pdf")
    assert long.endswith(".pdf") and len(long) <= KI.MAX_NAME


def test_safe_name_stays_in_folder_and_is_unique():
    for hostile in ("../../etc/passwd", "..\\..\\boot.ini", "/abs/path.md", "a/../../b.md"):
        out = KI.safe_name(hostile)
        assert "/" not in out and "\\" not in out and not out.startswith(".")
    assert KI.safe_name("a.md", taken={"a.md", "A-2.md"}) == "a-3.md"


def test_safe_name_follows_the_detected_kind():
    assert KI.safe_name("photo", kind="png") == "photo.png"
    assert KI.safe_name("photo.jpeg", kind="jpeg") == "photo.jpeg"
    assert KI.safe_name("paper.txt", kind="pdf") == "paper.pdf"
    assert KI.safe_name("....md") == "document.md"


def test_titles_never_fall_back_to_the_suffix():
    # Bug 20: "....md", ".md" and "é.md" gave the title "md".
    assert KI.title_for("Plain words here.", "é.md") == "é"
    assert KI.title_for("Plain words here.", "....md") == "Plain words here."
    assert KI.title_for("", ".md") == "Untitled document"
    assert KI.title_for("```\n# not a title\n```\n# Real title\n", "x.md") == "Real title"
    assert KI.title_for("no heading", "release-checklist_v2.md") == "release checklist v2"
    assert KI.title_for("text", "page.html", hint="Page title") == "Page title"


# -- type detection (bugs 5, 6) -------------------------------------------


def test_sniff_uses_bytes_before_name():
    assert KI.sniff(_pdf("hi"), "1706.03762") == "pdf"
    assert KI.sniff(_pdf("hi"), "paper.md") == "pdf"
    assert KI.sniff(PNG, "photo.md") == "png"
    assert KI.sniff(_docx("H", "p"), "file.bin") == "docx"
    assert KI.sniff(b"\x00\x01\x02binary", "x.md") == "binary"
    assert KI.sniff(b'<?xml version="1.0"?><svg xmlns="x"></svg>', "pic") == "svg"


def test_sniff_uses_content_type_then_suffix_then_shape():
    page = b"<!doctype html><html><body><p>x</p></body></html>"
    assert KI.sniff(page, "README", "text/html; charset=utf-8") == "html"
    assert KI.sniff(b"# Title\n", "README", "text/plain") == "text"
    assert KI.sniff(page, "page") == "html"
    assert KI.sniff(b"# Title\n", "notes") == "text"
    assert KI.sniff(page, "notes.md") == "text"  # a markdown file may show HTML


def test_decode_text_handles_bom_cut_and_legacy_bytes():
    assert KI.decode_text(b"\xef\xbb\xbfhello") == "hello"
    assert KI.decode_text("zażółć".encode()[:-1]) == "zażół"  # a cut at the size limit
    assert KI.decode_text("café".encode("cp1252")) == "café"
    assert KI.decode_text(b"\x00\x00\x00") is None


# -- conversion (bugs 5, 6, 17, 19) ---------------------------------------


def test_convert_empty_file_is_an_error():
    # Bug 17 and C2 #8073 item 2: an empty file fails with 400 on every path.
    for data in (b"", None, bytearray()):
        with pytest.raises(KBError, match="empty") as err:
            KI.convert(data, "empty.md")  # pyright: ignore[reportArgumentType]
        assert err.value.status == 400


def test_convert_markdown_and_title():
    out = KI.convert("# Gateway lease\n\nBody.".encode(), "Zażółć.md")
    assert (out.kind, out.name, out.title, out.keep_asset) == (
        "text",
        "Zazolc.md",
        "Gateway lease",
        False,
    )
    assert out.body.startswith("# Gateway lease")


def test_convert_text_without_suffix_gets_md():
    out = KI.convert(b"plain words", "notes")
    assert out.name == "notes.md" and out.title == "notes"


def test_convert_pdf_by_bytes_with_text_and_title():
    # Bug 6: an arXiv /pdf/<id> URL has no suffix; the bytes decide.
    out = KI.convert(
        _pdf("Attention is all you need"), "1706.03762", content_type="application/pdf"
    )
    assert out.kind == "pdf" and out.name == "1706.03762.pdf" and out.keep_asset
    assert "Attention is all you need" in out.body
    assert out.body.startswith("[1706.03762.pdf](assets/1706.03762.pdf)")
    # D9: one font size and no /Title: no heading, so the file name, not the first line.
    assert out.title == "1706.03762"
    titled = KI.convert(_pdf("Body", title="Paper title"), "x")
    assert titled.title == "Paper title"


def test_pdf_title_order():
    # D9: the first Markdown heading, then the PDF heading or /Title unless it is a
    # generator default, then the file name.  Never the first text line.
    plain = KI.convert(_pdf("First line of the body"), "report-2024.pdf")
    assert plain.title == "report 2024"
    for default in ("Microsoft Word - a.docx", "untitled", "Untitled-1", "slides.pptx", "  "):
        out = KI.convert(_pdf("First line of the body", title=default), "scan.pdf")
        assert out.title == "scan", default
    assert KI.convert(_pdf("Body", title="Lease rules"), "scan.pdf").title == "Lease rules"
    big = _pdf("Lease rules", size=24, body="The body text is in a smaller font.")
    assert KI.convert(big, "scan.pdf").title == "Lease rules"
    assert KI.pdf_meta_title("Microsoft PowerPoint - deck.pptx") is None
    assert KI.pdf_meta_title(" Real  title ") == "Real title"


def test_convert_pdf_without_pypdf_says_so(monkeypatch):
    # Bug 19: without the converter, store the asset and name what is missing.
    monkeypatch.setitem(sys.modules, "pypdf", None)
    out = KI.convert(_pdf("secret words"), "paper.pdf")
    assert out.missing_converters == ["pypdf"] and "secret words" not in out.body
    assert "%PDF" not in out.body and out.keep_asset


def test_convert_docx_with_and_without_python_docx(monkeypatch):
    data = _docx("Design", "Alpha calls Beta.")
    out = KI.convert(data, "spec.docx")
    assert out.kind == "docx" and "Alpha calls Beta." in out.body and out.missing_converters == []
    monkeypatch.setitem(sys.modules, "docx", None)
    plain = KI.convert(data, "spec.docx")
    assert plain.missing_converters == ["python-docx"]
    assert "# Design" in plain.body and "Alpha calls Beta." in plain.body and "PK" not in plain.body
    assert plain.title == "Design"


def test_convert_docx_with_a_dtd_is_stored_without_text():
    # N5: a DTD (billion laughs, external entity) is refused before any XML parse.
    laughs = (
        '<?xml version="1.0"?><!DOCTYPE w [<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY x SYSTEM "file:///etc/passwd">]>'
        "<w:document><w:body><w:p><w:r><w:t>&b;&x;</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", laughs)
    out = KI.convert(buf.getvalue(), "bomb.docx")
    assert out.kind == "docx" and out.keep_asset
    assert "aaaa" not in out.body and "root:" not in out.body
    assert any("DTD" in n for n in out.notes)
    # A broken document.xml without a DTD gives no 500 either.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document><w:body>")
    assert KI.convert(buf.getvalue(), "broken.docx").keep_asset


def test_program_text_is_one_code_block_titled_by_its_name():
    # N8: "#" comments in a program are not headings, and the title is the file name.
    script = b"#!/usr/bin/env bash\n# Deploy helper\necho hi\n"
    out = KI.convert(script, "deploy.sh")
    assert out.kind == "text" and out.title == "deploy.sh"
    # D11: the fence info string names the file, so the linker finds the graph file.
    assert out.body.startswith(
        '```bash title="deploy.sh"\n#!/usr/bin/env bash\n# Deploy helper'
    ) and out.body.endswith("```\n")
    shebang = KI.convert(b"#!/usr/bin/python3\n# tool\nprint(1)\n", "tool")
    assert shebang.body.startswith('```python title="tool"\n') and shebang.title == "tool"
    fenced = KI.convert(b"x = '```'\n", "q.py")
    assert fenced.body.startswith('````python title="q.py"\n') and fenced.body.endswith("````\n")
    assert KI.code_block("x", "sh", 'a "b".sh').startswith('```sh title="a_b_.sh"\n')
    # Prose keeps its first heading as the title.
    assert KI.convert(b"# Notes\ntext\n", "notes.md").title == "Notes"
    with pytest.raises(UnsupportedType):
        KI.convert(b"MZ\x90\x00" + b"\x00" * 64, "tool.exe")


def test_windows_reserved_names_are_changed():
    # N9: CON, PRN, AUX, NUL, COM1-9 and LPT1-9 cannot be file names on Windows.
    for name in ("CON", "prn", "aux", "nul", "com1", "LPT9"):
        assert KI.slugify(name) == f"{name.lower()}_doc"
        stem = KI.split_name(KI.safe_name(f"{name}.md"))[0]
        assert stem.lower() == f"{name.lower()}-doc"
    assert KI.safe_name("con.png", kind="png") == "con-doc.png"
    assert KI.safe_name("console.md") == "console.md" and KI.slugify("com10") == "com10"


def test_convert_docx_made_by_python_docx():
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_heading("Release plan", level=1)
    doc.add_paragraph("The gateway restarts after the upgrade.")
    buf = io.BytesIO()
    doc.save(buf)
    out = KI.convert(buf.getvalue(), "plan.docx")
    assert out.title == "Release plan" and "The gateway restarts after the upgrade." in out.body
    assert out.missing_converters == [] and out.notes == []


def test_convert_image_and_unknown_binary():
    img = KI.convert(PNG, "shot")
    assert img.name == "shot.png" and img.body == "![shot](assets/shot.png)\n" and img.keep_asset


def test_convert_refuses_programs_archives_and_unknown_binary():
    # C2 #8073 item 1: tool.exe gave a document; bug 5: raw bytes as body.  Both are 415 now.
    cases = [
        (b"MZ\x90\x00\x03" + b"\x00" * 40, "tool.exe", "application/x-msdownload"),
        (b"echo hi\n", "tool.exe", None),
        (b"plain text", "notes.txt", "application/x-msdownload"),
        (b"\x7fELF\x02\x01", "run", None),
        (b"\x00\x01\x02\x03", "data.md", None),
        (b"PK\x03\x04" + b"\x00" * 30, "a.zip", None),
    ]
    for data, name, ctype in cases:
        with pytest.raises(KBError, match="not supported") as err:
            KI.convert(data, name, content_type=ctype)
        assert err.value.status == 415 and "PDF" in str(err.value)
    # A text file sent as application/octet-stream is still text.
    assert KI.convert(b"# Hi", "hi.md", content_type="application/octet-stream").kind == "text"


GITHUB_PAGE = """<!doctype html><html><head><title>acme/widget: A widget</title></head><body>
<nav><a href="/">Home</a> Pull requests Issues Marketplace</nav>
<main><div>Code Issues</div>
<article class="markdown-body entry-content"><h1>Widget</h1><p>The <code>Widget</code> renders a chart.</p></article>
</main><footer>Terms Privacy</footer><script>var x = 1;</script></body></html>"""


def test_html_keeps_only_the_readme_region():
    # Bug 6: a GitHub page gives its README as markdown, not menus.
    out = KI.convert(
        GITHUB_PAGE.encode(),
        "widget",
        content_type="text/html",
        url="https://github.com/acme/widget",
    )
    assert out.kind == "html" and out.title == "Widget"
    assert "# Widget" in out.body and "`Widget`" in out.body
    for noise in ("Pull requests", "Marketplace", "Privacy", "var x"):
        assert noise not in out.body


def test_html_fallback_without_markdownify(monkeypatch):
    monkeypatch.setitem(sys.modules, "markdownify", None)
    out = KI.convert(GITHUB_PAGE.encode(), "widget.html")
    assert out.missing_converters == ["markdownify"]
    assert "Widget" in out.body and "<" not in out.body and "Marketplace" not in out.body


def test_html_title_from_title_tag_without_h1():
    page = b"<html><head><title>Only title</title></head><body><p>Text.</p></body></html>"
    assert KI.convert(page, "p.html").title == "Only title"


# -- URL fetch (bug 6; the SSRF guard stays) -------------------------------


def test_rewrite_url_points_github_blob_at_raw():
    assert (
        KI.rewrite_url("https://github.com/a/b/blob/main/docs/x.md")
        == "https://raw.githubusercontent.com/a/b/main/docs/x.md"
    )
    assert KI.rewrite_url("https://github.com/a/b") == "https://github.com/a/b"
    assert KI.rewrite_url("https://arxiv.org/pdf/1706.03762") == "https://arxiv.org/pdf/1706.03762"


def test_fetch_blocks_private_and_bad_urls():
    for url in (
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest",
        "file:///etc/passwd",
        "ftp://x.org/a",
    ):
        with pytest.raises(KBError):
            KI.fetch(url, max_bytes=1024)


class _Resp:
    def __init__(self, data, ctype, url, status=200):
        self._buf, self.status, self._url = io.BytesIO(data), status, url
        self.headers = {"Content-Type": ctype}

    def read(self, n):
        return self._buf.read(n)

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(monkeypatch, resp=None, exc=None):
    import graphify.security as sec

    class _O:
        def open(self, req, timeout=None):
            if exc:
                raise exc
            return resp

    monkeypatch.setattr(sec, "_build_opener", lambda: _O())
    monkeypatch.setattr(sec, "validate_url", lambda url: url)


def test_fetch_keeps_content_type_and_final_name(monkeypatch):
    _opener(monkeypatch, _Resp(_pdf("x"), "application/pdf", "https://arxiv.org/pdf/1706.03762v7"))
    got = KI.fetch("https://arxiv.org/pdf/1706.03762", max_bytes=1 << 20)
    assert got.content_type == "application/pdf" and got.name == "1706.03762v7"
    assert KI.convert(got.data, got.name, content_type=got.content_type).kind == "pdf"


def test_fetch_size_cap_and_unreachable(monkeypatch):
    _opener(monkeypatch, _Resp(b"x" * 5000, "text/plain", "https://e.org/a"))
    with pytest.raises(KBError, match="larger"):
        KI.fetch("https://e.org/a", max_bytes=100)
    import urllib.error

    _opener(monkeypatch, exc=urllib.error.URLError("connection refused"))
    with pytest.raises(Unavailable):
        KI.fetch("https://e.org/a", max_bytes=100)


# -- LLM ingest (bugs 8, 15; memo R5) -------------------------------------


def _llm_graph():
    nodes = [
        {
            "id": "cart",
            "label": "ShoppingCart",
            "source_file": "shop/cart.py",
            "file_type": "code",
            "_callable_class": True,
        },
        {
            "id": "calc",
            "label": "PriceCalculator",
            "source_file": "shop/price.py",
            "file_type": "code",
            "_callable_class": True,
        },
        {
            "id": "run_a",
            "label": "run()",
            "source_file": "a/x.py",
            "file_type": "code",
            "_callable": True,
        },
        {
            "id": "run_b",
            "label": "run()",
            "source_file": "b/y.py",
            "file_type": "code",
            "_callable": True,
        },
        {
            "id": "doc:design",
            "label": "Design",
            "source_file": "graphify-out/kb/documents/design/body.md",
            "file_type": "document",
            "node_kind": "page",
            "_origin": "kb",
        },
    ]
    links = [{"source": "cart", "target": "calc", "relation": "calls"}]
    return nodes, links


def _onto():
    onto = with_kb_vocabulary(Ontology())
    onto.relations["delegates_to"] = RelationSpec(
        "delegates_to", domain=["code.class"], range=["code.class"]
    )
    onto.relations["describes"] = RelationSpec(
        "describes", domain=["document"], range=["code", "concept"]
    )
    onto.classes["concept.policy"] = ClassSpec("concept.policy", parent="concept")
    return onto


def _plan(answer, body, **kw):
    nodes, links = _llm_graph()
    onto = _onto()
    return KI.plan_extraction(
        answer,
        body=body,
        doc_id="design",
        doc_node="doc:design",
        doc_title="Design",
        index=KL.LinkIndex.build(nodes, links),
        onto=onto,
        class_of=onto.node_class,
        backend="fake",
        model="m1",
        **kw,
    )


BODY = "# Design\n\nThe ShoppingCart delegates pricing to PriceCalculator.\nA refund policy limits returns to 30 days.\n"


def test_plan_reuses_existing_nodes_and_grounds_evidence():
    plan = _plan(
        {
            "triples": [
                {
                    "subject": "ShoppingCart",
                    "relation": "delegates to",
                    "object": "PriceCalculator",
                    "evidence": "The ShoppingCart delegates pricing to PriceCalculator.",
                    "confidence": 1.0,
                }
            ]
        },
        BODY,
    )
    [edge] = plan.edges
    assert (edge["source"], edge["target"], edge["relation"]) == ("cart", "calc", "delegates_to")
    # Bug 8: LLM edges are INFERRED and never 1.0.
    assert edge["confidence"] == "INFERRED" and edge["confidence_score"] == 0.85
    assert edge["attrs"]["offset"] == BODY.index("The ShoppingCart")
    assert (
        edge["attrs"]["doc_id"] == "design"
        and edge["attrs"]["backend"] == "fake"
        and edge["attrs"]["model"] == "m1"
    )
    assert plan.nodes == {} and plan.stats["reused"] == 2


def test_plan_drops_ungrounded_triples():
    plan = _plan(
        {
            "triples": [
                {
                    "subject": "ShoppingCart",
                    "relation": "calls",
                    "object": "PriceCalculator",
                    "evidence": "never said",
                },
                {
                    "subject": "ShoppingCart",
                    "relation": "calls",
                    "object": "Inventory",
                    "evidence": "ShoppingCart delegates",
                },
                {
                    "subject": "",
                    "relation": "calls",
                    "object": "PriceCalculator",
                    "evidence": "ShoppingCart delegates",
                },
                {
                    "subject": "ShoppingCart",
                    "relation": "calls",
                    "object": "ShoppingCart",
                    "evidence": "ShoppingCart delegates",
                },
            ]
        },
        BODY,
    )
    assert plan.edges == []
    assert [d["reason"] for d in plan.dropped] == [
        "evidence_not_in_text",
        "entity_not_in_text",
        "incomplete",
        "self_loop",
    ]


def test_plan_grounding_folds_case_space_and_quotes():
    body = "The  **ShoppingCart**\ndelegates pricing to `PriceCalculator`."
    plan = _plan(
        {
            "triples": [
                {
                    "subject": "ShoppingCart",
                    "relation": "delegates_to",
                    "object": "PriceCalculator",
                    "evidence": "the shoppingcart delegates pricing to pricecalculator",
                }
            ]
        },
        body,
    )
    assert len(plan.edges) == 1 and plan.edges[0]["attrs"]["offset"] == 0


def test_plan_maps_the_document_to_its_own_node():
    # Bug 8: never a second document node beside doc:<id>.
    plan = _plan(
        {
            "entities": [{"name": "refund policy", "class": "concept.policy"}],
            "triples": [
                {
                    "subject": "THIS_DOCUMENT",
                    "relation": "describes",
                    "object": "refund policy",
                    "evidence": "A refund policy limits returns",
                    "confidence": 0.75,
                }
            ],
        },
        BODY,
    )
    [edge] = plan.edges
    assert edge["source"] == "doc:design" and edge["target"] == "new:refund policy"
    assert plan.nodes["new:refund policy"]["class"] == "concept.policy"
    assert plan.nodes["new:refund policy"]["file_type"] == "concept"
    assert edge["confidence"] == "INFERRED" and edge["confidence_score"] == 0.75
    assert all(not ref.startswith("new:design") for ref in plan.nodes)


def test_plan_marks_nonconforming_triples_ambiguous():
    # memo R5: a domain/range error is kept for review as AMBIGUOUS with a finding.
    plan = _plan(
        {
            "triples": [
                {
                    "subject": "PriceCalculator",
                    "relation": "describes",
                    "object": "ShoppingCart",
                    "evidence": "delegates pricing to PriceCalculator",
                }
            ]
        },
        BODY,
    )
    [edge] = plan.edges
    assert edge["confidence"] == "AMBIGUOUS" and edge["confidence_score"] == KI.AMBIGUOUS_SCORE
    assert edge["attrs"]["finding"] == "nonconforming_extraction"
    assert plan.findings and plan.stats["nonconforming"] == 1


def test_plan_sends_ambiguous_names_to_review():
    body = "The run step starts the job: run calls ShoppingCart."
    plan = _plan(
        {
            "triples": [
                {
                    "subject": "run",
                    "relation": "calls",
                    "object": "ShoppingCart",
                    "evidence": "run calls ShoppingCart",
                }
            ]
        },
        body,
    )
    assert plan.edges == [] and plan.nodes == {}
    [item] = plan.review
    assert item["mention"] == "run" and item["reason"] == "possible_match"
    assert {c["id"] for c in item["candidates"]} == {"run_a", "run_b"}


def test_plan_parses_fenced_json_text():
    raw = (
        "```json\n"
        + '{"triples": [{"subject": "ShoppingCart", "relation": "calls", "object": "PriceCalculator", '
        '"evidence": "ShoppingCart delegates pricing"}]}' + "\n```"
    )
    assert len(_plan(raw, BODY).edges) == 1


def test_resolve_entity_bands():
    # Review 2, item 1: label similarity x class agreement; reuse needs >= .9 and a compatible class.
    nodes, links = _llm_graph()
    index = KL.LinkIndex.build(nodes, links)
    onto = _onto()
    kw = {"onto": onto, "class_of": onto.node_class}
    # Same class, exact name: reuse.
    exact = KI.resolve_entity("ShoppingCart", "code.class", index, **kw)
    assert exact.status == "reuse" and exact.node_id == "cart"
    assert KI.resolve_entity("ShoppingCart", None, index, **kw).status == "reuse"
    # Near spelling: review, never a silent merge.
    near = KI.resolve_entity("PriceCalculater", None, index, **kw)
    assert near.status == "review" and KI.REVIEW_AT <= near.score < KI.REUSE_AT
    assert near.candidates[0]["id"] == "calc"
    # Unrelated name: new.
    assert KI.resolve_entity("Inventory", None, index, **kw).status == "new"
    # Two equal names and no context: review.
    assert KI.resolve_entity("run", None, index, **kw).status == "review"
    # A concept never merges into a code class; the code node is kept as same_as.
    concept = KI.resolve_entity("shopping cart", "concept", index, **kw)
    assert concept.status == "new" and concept.same_as == "cart"
    # Anchors break a tie between two equal names.
    anchored = KI.resolve_entity(
        "run",
        None,
        KL.LinkIndex.build(
            nodes, links + [{"source": "run_a", "target": "cart", "relation": "calls"}]
        ),
        anchors=["cart"],
        **kw,
    )
    assert anchored.status == "reuse" and anchored.node_id == "run_a"


def test_label_similarity():
    assert KI.label_similarity("Session", "Session") == 1.0
    assert KI.label_similarity("shopping cart", "ShoppingCart") >= 0.96
    assert KI.label_similarity("Inventory", "PriceCalculator") < KI.REVIEW_AT
    assert KI.jaro_winkler("martha", "marhta") == pytest.approx(0.9611, abs=1e-4)


def test_plan_links_a_new_concept_to_the_same_name_code_node():
    # Review 2, item 1 (one graph): concept "Shopping cart" is not the class
    # ShoppingCart, but it must not be an isolated component.
    plan = _plan(
        {
            "entities": [{"name": "shopping cart", "class": "concept"}],
            "triples": [
                {
                    "subject": "THIS_DOCUMENT",
                    "relation": "mentions",
                    "object": "shopping cart",
                    "evidence": "The ShoppingCart delegates pricing",
                }
            ],
        },
        BODY,
    )
    assert plan.nodes["new:shopping cart"]["class"] == "concept"
    same = [e for e in plan.edges if e["attrs"].get("reason") == "possible_same_as"]
    assert len(same) == 1
    [edge] = same
    assert (edge["source"], edge["target"], edge["relation"]) == (
        "new:shopping cart",
        "cart",
        "mentions",
    )
    assert edge["confidence"] == "AMBIGUOUS" and 0 < edge["confidence_score"] < 0.5
    [item] = [r for r in plan.review if r["reason"] == "possible_same_as"]
    assert item["node"] == "new:shopping cart" and item["candidates"][0]["id"] == "cart"


def test_grounder_names_whole_words_near_the_evidence():
    # Review 2, item 2: no match inside a longer word, and only near the evidence.
    g = KI.Grounder("Set the target. The report is ready.\n\nFar away: get the port.")
    span = g.find_span("Set the target")
    assert span and not g.names("get", g.window(*span))
    span = g.find_span("The report is ready")
    assert span and not g.names("port", g.window(*span))
    assert g.names("get") and g.names("port")
    assert g.names("ShoppingCart") is False
    g2 = KI.Grounder("The shopping cart is here.")
    span = g2.find_span("cart is here")
    assert span and g2.names("ShoppingCart", g2.window(*span))


def test_plan_drops_a_triple_whose_name_is_far_from_the_evidence():
    body = BODY + "\n" + ("Filler text. " * 20) + "\n\nInventory is listed at the end.\n"
    plan = _plan(
        {
            "triples": [
                {
                    "subject": "ShoppingCart",
                    "relation": "calls",
                    "object": "Inventory",
                    "evidence": "The ShoppingCart delegates pricing",
                }
            ]
        },
        body,
    )
    assert plan.edges == [] and plan.dropped[0]["reason"] == "entity_not_in_text"


def test_extract_sends_one_call_per_chunk_and_keeps_offsets(monkeypatch):
    # Review 2, item 3: a long body is split; each chunk is one LLM call.
    monkeypatch.setattr(KI, "MAX_PROMPT_CHARS", 2000)
    monkeypatch.setattr(KI, "CHUNK_OVERLAP", 200)
    tail = "ShoppingCart delegates pricing to PriceCalculator at the end."
    body = "# Design\n\n" + ("Plain filler words here. " * 150) + "\n\n## Late\n\n" + tail + "\n"
    calls = []

    def fake(prompt, *, backend, model):
        calls.append(prompt)
        triple = {
            "subject": "ShoppingCart",
            "relation": "delegates_to",
            "object": "PriceCalculator",
            "evidence": tail,
        }
        # Every chunk that shows the tail returns the same fact.
        return json.dumps({"triples": [triple] if tail[:30] in prompt else []})

    chunks = KI.chunk_body(body, limit=2000, overlap=200)
    assert len(chunks) >= 2 and all(body[s : s + len(t)] == t for s, t in chunks)
    assert all(len(t) <= 2000 for _, t in chunks)
    answers = KI.extract(
        body, doc_id="design", title="Design", summary="", backend="fake", model="m1", call=fake
    )
    assert len(calls) == len(chunks) and [s for s, _ in answers] == [s for s, _ in chunks]
    plan = _plan(answers, body)
    [edge] = plan.edges
    assert edge["attrs"]["offset"] == body.index(tail)
    assert edge["attrs"]["chunk_offset"] in {s for s, _ in chunks}
    assert plan.stats["chunks"] == len(chunks)
    assert plan.stats["duplicates"] == sum(tail[:30] in p for p in calls) - 1


def test_snap_score_keeps_llm_scores_below_code_evidence():
    assert KI.snap_score(1.0) == 0.85 and KI.snap_score(0.6) == 0.55 and KI.snap_score("x") == 0.55
    assert KI.snap_score(0.7) == 0.65


def test_prompt_has_ontology_and_evidence_rule():
    summary = KI.ontology_summary(_onto())
    assert "delegates_to" in summary and "concept.policy" in summary
    prompt, cut = KI.build_prompt("Body text.", doc_id="design", title="Design", summary=summary)
    assert "evidence" in prompt and "THIS_DOCUMENT" in prompt and "Body text." in prompt and not cut
    _, more = KI.build_prompt(
        "word " * (KI.MAX_PROMPT_CHARS // 4), doc_id="d", title="t", summary=""
    )
    assert more


def test_call_llm_maps_backend_failures(monkeypatch):
    # Bug 15: an unreachable backend is Unavailable (HTTP 502), not a 500.
    import graphify.llm as llm

    def down(*a, **k):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(llm, "call_llm", down, raising=False)
    monkeypatch.setattr(llm, "_call_llm", down, raising=False)
    with pytest.raises(Unavailable) as err:
        KI.call_llm("p", backend="ollama", model=None)
    assert "ollama" in str(err.value) and getattr(err.value, "status", 502) == 502


def test_resolve_backend_rejects_unknown_names():
    with pytest.raises(KBError):
        KI.resolve_backend("no-such-backend")


# -- end to end through KnowledgeBase.ingest --------------------------------


def _kb(tmp_path):
    import json

    from graphify.kb import KnowledgeBase

    out = tmp_path / "graphify-out"
    out.mkdir()
    nodes, links = _llm_graph()
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [n for n in nodes if n["id"] != "doc:design"],
        "links": links,
    }
    (out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    return KnowledgeBase(out / "graph.json")


def _mentions(kb, doc):
    from graphify.ontology import MENTIONS

    nid = doc["node_id"]
    return {
        str(e["target"]): e
        for e in kb.data[kb.links_key]
        if e["source"] == nid and e["relation"] == MENTIONS
    }


def test_ingest_doc_node_is_page_with_scored_text_links(tmp_path):
    # Bug 9 and memo R4: a page node; text links are INFERRED with the linker's score.
    kb = _kb(tmp_path)
    res = kb.ingest(
        "pricing.md", text="# Pricing\n\nThe `ShoppingCart` asks `PriceCalculator` for totals."
    )
    doc = res["document"]
    assert kb._nodes[doc["node_id"]]["node_kind"] == "page"
    links = _mentions(kb, doc)
    assert set(links) == {"cart", "calc"} and res["linked"] == 2
    assert {e["confidence"] for e in links.values()} == {"INFERRED"}
    assert {e["confidence_score"] for e in links.values()} == {0.95}


def test_ingest_ambiguous_name_goes_to_review(tmp_path):
    # Bug 7: two run() nodes and no context: no guess, a possible link instead.
    kb = _kb(tmp_path)
    res = kb.ingest("ops.md", text="# Ops\n\nCall `run()` after the deploy.")
    assert "run_a" not in _mentions(kb, res["document"]) and "run_b" not in _mentions(
        kb, res["document"]
    )
    assert [p["mention"] for p in res["possible"]] == ["run()"]
    assert {c["id"] for c in res["possible"][0]["candidates"]} == {"run_a", "run_b"}


def test_wiki_links_extracted_only_when_unique(tmp_path):
    # Memo R4: [[x]] is EXTRACTED for one node, review for several, a new node for none.
    kb = _kb(tmp_path)
    before = len(kb._nodes)
    doc = kb.create_document("Notes", body="See [[ShoppingCart]], [[run]] and [[Inventory]].")
    links = _mentions(kb, doc)
    assert links["cart"]["confidence"] == "EXTRACTED"
    assert not {"run_a", "run_b"} & set(links)
    meta = kb._read_meta(doc["id"])
    assert meta and [p["reason"] for p in meta["possible"]] == ["ambiguous_wiki"]
    assert len(kb._nodes) == before + 2  # the document node and Inventory, no copy of run()


def test_ingest_names_and_titles(tmp_path):
    # Bugs 5 and 20: non-ASCII and dot-only names keep a suffix and give a real title.
    kb = _kb(tmp_path)
    doc = kb.ingest("Zażółć gęślą.md", data="Plain text, no heading.".encode())["document"]
    # The title keeps the real text; only the file name is ASCII.
    assert doc["title"] == "Zażółć gęślą" and doc["source"] == "Zazolc_gesla.md"
    doc = kb.ingest("....md", data=b"# Real title\n\nbody")["document"]
    assert doc["title"] == "Real title"
    doc = kb.ingest(".md", data=b"just words")["document"]
    assert doc["title"] not in ("md", "")


def test_ingest_pdf_by_bytes_keeps_asset(tmp_path):
    # Bug 6: no suffix; the bytes say PDF, the asset keeps a .pdf name.
    kb = _kb(tmp_path)
    res = kb.ingest("1706.03762", data=_pdf("Attention is all you need"))
    doc = res["document"]
    assert res["kind"] == "pdf" and [a["name"] for a in doc["assets"]] == ["1706.03762.pdf"]
    assert kb.asset_path(doc["id"], "1706.03762.pdf").is_file()
    assert "Attention is all you need" in doc["body"]


def test_ingest_empty_and_unsupported_write_nothing(tmp_path):
    # Bug 17: empty data and empty text fail the same way; programs get 415.
    kb = _kb(tmp_path)
    cases: list[dict[str, Any]] = [{"data": b""}, {"text": ""}]
    for kw in cases:
        with pytest.raises(KBError, match="empty") as err:
            kb.ingest("a.md", **kw)
        assert err.value.status == 400
    with pytest.raises(KBError) as err:
        kb.ingest("tool.exe", data=b"MZ\x90\x00" + b"\x00" * 64)
    assert err.value.status == 415
    assert kb.list_documents() == []


def test_ingest_url_uses_fetched_type(tmp_path, monkeypatch):
    # Bug 6: an arXiv /pdf/<id> URL gives PDF text.
    kb = _kb(tmp_path)
    monkeypatch.setattr(
        KI,
        "fetch",
        lambda url, **_: KI.Fetched(
            _pdf("Attention is all you need"),
            "application/pdf",
            "https://arxiv.org/pdf/1706.03762",
            "1706.03762",
        ),
    )
    res = kb.ingest("", url="https://arxiv.org/pdf/1706.03762")
    assert res["kind"] == "pdf" and res["document"]["source"] == "https://arxiv.org/pdf/1706.03762"


def _fake_llm(monkeypatch, answer):
    monkeypatch.setattr(KI, "resolve_backend", lambda backend: "fake")
    monkeypatch.setattr(KI, "call_llm", lambda prompt, **_: answer)


def test_llm_ingest_grounded_one_document_node(tmp_path, monkeypatch):
    # Bug 8 and memo R5: grounded INFERRED edges with provenance, no second document node.
    kb = _kb(tmp_path)
    body = "# Refunds\n\nThis document describes the refund policy. The ShoppingCart delegates pricing to PriceCalculator."
    _fake_llm(
        monkeypatch,
        {
            "entities": [{"name": "refund policy", "class": "concept"}],
            "triples": [
                {
                    "subject": "THIS_DOCUMENT",
                    "relation": "describes",
                    "object": "refund policy",
                    "evidence": "This document describes the refund policy",
                },
                {
                    "subject": "ShoppingCart",
                    "relation": "delegates_to",
                    "object": "PriceCalculator",
                    "evidence": "The ShoppingCart delegates pricing to PriceCalculator.",
                },
                {
                    "subject": "THIS_DOCUMENT",
                    "relation": "mentions",
                    "object": "PriceCalculator",
                    "evidence": "delegates pricing to PriceCalculator",
                },
                {
                    "subject": "ShoppingCart",
                    "relation": "owns",
                    "object": "Warehouse",
                    "evidence": "not in the text",
                },
            ],
        },
    )
    res = kb.ingest("refunds.md", text=body, mode="llm")
    doc = res["document"]
    docs = [
        n
        for n in kb._nodes.values()
        if n.get("doc_id") == doc["id"] and n.get("file_type") == "document"
    ]
    assert [n["id"] for n in docs] == [doc["node_id"]]
    [policy] = res["nodes_added"]
    edge = next(
        e for e in kb.data[kb.links_key] if e["source"] == doc["node_id"] and e["target"] == policy
    )
    # The fixture ontology has no "describes": the triple stays, but AMBIGUOUS with a finding.
    assert edge["relation"] == "describes" and edge["confidence"] == "AMBIGUOUS"
    assert (
        edge["doc_id"] == doc["id"]
        and edge["backend"] == "fake"
        and edge["offset"] == body.index("This document")
    )
    assert {f["edge"]["relation"] for f in res["findings"]} == {"describes", "delegates_to"}
    calc = _mentions(kb, doc)["calc"]
    assert calc["confidence"] == "INFERRED" and calc["backend"] == "fake"
    # The graph is simple: cart and calc already share a calls edge.
    assert {
        "source": "cart",
        "target": "calc",
        "relation": "delegates_to",
        "reason": "pair_has_edge",
    } in res["skipped"]
    assert [d["reason"] for d in res["dropped"]] == ["evidence_not_in_text"]
    assert res["llm"]["backend"] == "fake" and res["edges_added"] == 2


def test_llm_backend_down_raises_502_and_writes_nothing(tmp_path, monkeypatch):
    # Bug 15: an unreachable backend is a clear 502, and the ingest leaves no document.
    kb = _kb(tmp_path)
    monkeypatch.setattr(KI, "resolve_backend", lambda backend: "ollama")

    def down(prompt, **_):
        raise Unavailable("the ollama backend does not answer at http://localhost:11434")

    monkeypatch.setattr(KI, "call_llm", down)
    with pytest.raises(Unavailable) as err:
        kb.ingest("a.md", text="# A\n\nThe `ShoppingCart` is here.", mode="llm")
    assert err.value.status == 502 and "ollama" in str(err.value)
    assert kb.list_documents() == []


def test_update_relinks_and_undo_removes_ingest(tmp_path):
    # Memo R4: a new body links again; one ingest is one undo step.
    kb = _kb(tmp_path)
    doc = kb.ingest("n.md", text="# N\n\nThe `ShoppingCart` here.")["document"]
    assert set(_mentions(kb, doc)) == {"cart"}
    kb.update_document(doc["id"], body="# N\n\nNow `PriceCalculator` only.")
    assert set(_mentions(kb, doc)) == {"calc"}
    kb.undo()
    kb.undo()
    assert kb.list_documents() == [] and doc["node_id"] not in kb._nodes


def _cart_kb(tmp_path):
    """A knowledge base whose graph has the file node ``shop/cart.py``."""
    import json

    from graphify.kb import KnowledgeBase

    out = tmp_path / "graphify-out"
    out.mkdir()
    nodes, links = _llm_graph()
    nodes = [n for n in nodes if n["id"] != "doc:design"]
    nodes.append(
        {"id": "cart_file", "label": "cart.py", "source_file": "shop/cart.py", "file_type": "code"}
    )
    links = [*links, {"source": "cart_file", "target": "cart", "relation": "contains"}]
    graph = {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": links}
    (out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    return KnowledgeBase(out / "graph.json")


def test_ingest_code_file_links_the_file_and_is_no_orphan(tmp_path):
    # D11: an uploaded code file was one fenced block with no link: an orphan_node finding.
    kb = _cart_kb(tmp_path)
    res = kb.ingest(
        "cart.py", data=b"from shop.price import PriceCalculator\n\nclass ShoppingCart:\n    pass\n"
    )
    doc = res["document"]
    assert doc["title"] == "cart.py" and res["linked"] >= 1
    assert "cart_file" in _mentions(kb, doc)
    findings = kb.validate()["findings"]
    assert not any(f["code"] == "orphan_node" and f.get("node") == doc["node_id"] for f in findings)


def test_pasted_code_block_links_the_file_named_by_its_title(tmp_path):
    # D11: a pasted code block has no title="..." in its fence; the document title names the file.
    kb = _cart_kb(tmp_path)
    body = "```python\nclass ShoppingCart:\n    pass\n```\n"
    doc = kb.create_document("cart.py", body=body, link=True)
    assert "cart_file" in _mentions(kb, doc)
