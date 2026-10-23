"""Entity linking for knowledge base documents (no LLM).

A document names graph nodes.  This module finds those names and decides,
for each one, whether to link it, to keep it as a possible link for review,
or to drop it.  It follows the usual entity linking steps [E1]:

1. Candidate generation.  A mention is a candidate only when it has a shape
   that names something: a code span, an identifier (``snake_case``,
   ``CamelCase``, ``name()``, ``a.b``, ``a::b``, ``a/b``, ``kebab-case``) or a
   label of two or more words.  Bare lower-case words are never candidates.
   A label that occurs in three or more files is a common word, not a name
   (keyphraseness, Mihalcea and Csomai 2007 [E3]).
2. Ranking.  When one name fits several nodes, each candidate is scored by
   its relatedness to the document's unambiguous anchors (Milne and Witten
   2008 [E2]).  Relatedness uses the shared graph neighbours (graph distance
   of at most 2), the same directory and the same community.
   Anchors in the same paragraph count fully, others half.
3. Qualifiers.  Words near the mention that name a candidate's path, module
   or owner file decide first: the sentence, then the paragraph, then the
   document at half weight, each word weighted by its inverse frequency over
   the candidates.  Two places with their own nearby words make the mention
   ambiguous, so it is not linked.
4. Decision.  A unique candidate is linked.  An ambiguous one is linked only
   when the winner leads by a normalised margin of at least 0.5.  Otherwise
   the mention is a possible link for review, the middle band of
   Fellegi and Sunter (1969) [E6].  A confidence cut-off follows DBpedia
   Spotlight [E4].  One name keeps one sense per paragraph, not per
   document (Gale, Church and Yarowsky 1992 give one sense per discourse;
   a document can compare two nodes of the same name).

Language.  Identifiers, words and sentence ends use Unicode classes, so a
Polish or Cyrillic code name is found.  The stoplist, the title-run rule and
the sentence-start rule assume English prose: in other languages a
capitalised common word can stay a candidate, and a few names at a sentence
start are not linked.

Tuned policy.  The SCORE_* values are graphify's discrete INFERRED scores.
The other constants below have no paper value: they are tuned policy.  The
evaluator set (5 documents, 50 gold links, 19 NIL names) tunes them, and
the review-2 hard set (Polish text, short names) checks them.  Both live in
tests/fixtures/kb_link/ and run in tests/test_kb_link.py.

References: [E1] Shen et al., TKDE 2015;
[E2] Milne and Witten, CIKM 2008; [E3] Mihalcea and Csomai, CIKM 2007;
[E4] Mendes et al., I-SEMANTICS 2011; [E6] Fellegi and Sunter, JASA 1969;
Gale, Church and Yarowsky, HLT 1992 (one sense per discourse).
"""

from __future__ import annotations

import math
import posixpath
import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Iterable

from graphify.build import _is_file_node_label
from graphify.export import _strip_diacritics

# Discrete INFERRED scores of graphify's rubric (export._CONFIDENCE_SCORE_DEFAULTS).
SCORE_CODE = 0.95  # a unique identifier, code span or path
SCORE_PHRASE = 0.75  # a unique label of two or more words
SCORE_SCOPE = 0.75  # the only candidate defined in a module that the text names
SCORE_HEADING = 0.65  # a unique heading label of two or more words, also at a sentence start
SCORE_CONTEXT = 0.55  # a winner of the context ranking, or a weak unique name
# Tuned policy (see the module docstring), not values from a paper.
LINK_MARGIN = 0.5  # the winner must lead by half of its own score
MIN_RELATEDNESS = 0.05  # below this the context gives no evidence
COMMON_FILES = 3  # a label in this many files is a common word
MAX_CANDIDATES = 400  # rank at most this many nodes for one name
REVIEW_TOP = 5  # candidates kept for a possible link
# D11 c: also spot code names in the fenced blocks of prose documents.  Measured
# on the evaluator and hard sets before it was set (see link_text).
FENCED_CODE_NAMES = False
COLLECTIVE_MAX = 5  # names with more candidates never take part in the collective pass
TEST_PRIOR = 0.5  # context scores of a test-file candidate count half: a note rarely means a test
SCOPE_WINDOW = 400  # characters: a module named this close before a name sets its scope
SCOPE_AFTER = 120  # characters: a module named this close after it, in the same sentence
FAR_WEIGHT = 0.5  # an anchor in another paragraph counts half
MIN_EVIDENCE = 0.3  # qualifier evidence that can decide or block a choice
EVIDENCE_RATIO = 1.5  # the qualifier winner needs this many times the runner-up's evidence
DOC_WEIGHT = 0.5  # qualifier words elsewhere in the document count half
OWNERS_MAX = 20  # owner nodes (files, modules) read for the qualifier words of one node

# Weights of the relatedness signals.  Shared neighbours carry the most weight.
W_NEIGHBOURS = 0.6
W_DIRECTORY = 0.25  # the same file counts fully, the same directory half
W_COMMUNITY = 0.15

STOPWORDS = frozenset(
    """
    a about above after again against all also an and any are as at be because been before being below
    between both but by can could did do does doing done down during each else even every few for from
    further get gets got had has have having he her here hers him his how i if in into is it its itself
    just let like made make many may me might more most much must my new no nor not now of off often old
    on once one only or other our out over own per same see set she should so some such than that the
    their them then there these they this those though through to too two under until up upon us use
    used uses using very via was we were what when where which while who whom why will with within
    without would yet you your
    """.split()
)

_FENCE_RE = re.compile(r"(?ms)^\s*(```|~~~).*?^\s*\1[^\n]*$")
_CODE_SPAN_RE = re.compile(r"(`+)([^`\n]{1,200}?)\1")
_SKIP_RE = re.compile(
    r"\[\[[^\]\n]{0,200}\]\]"  # wiki links, resolved apart
    r"|\]\([^)\n]{0,500}\)"  # markdown link targets
    r"|<[^>\n]{0,300}>"  # html tags and autolinks
    r"|\bhttps?://\S+"  # bare urls
)
_IDENT_RE = re.compile(
    r"(?<![\w./:-])"
    r"(?:\.?[^\W\d][\w$]*(?:(?:\.|::|/|-)[^\W\d][\w$]*)*[!?*]?(?:\(\))?)"
)
_WORD_RE = re.compile(r"[^\W_][\w'\-]*")
# CamelCase needs a word after the hump: ``GatewayClient``, ``OAuthFlow``,
# ``parseJSON``.  ``iOS`` is a product name, not a code name.
_CAMEL_RE = re.compile(r"[a-z0-9][A-Z][a-z]|[A-Z]{2}[a-z]|[a-z]{2}[A-Z]{2,}")
_WIKI_RE = re.compile(r"\[\[([^\]|\n]{1,200})(?:\|([^\]\n]{1,80}))?\]\]")
_SENTENCE_END_RE = re.compile(r"[.!?:;…。#>\-*|]\s*$|^\s*$")
_FILE_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,6}$")
# A code document is one fenced block (kb_ingest.code_block).  Its info string
# can carry the file name: ```clojure title="relay.clj"
_CODE_DOC_RE = re.compile(
    r"\A\s*(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)\n(?P<code>.*?)\n?(?P=fence)[ \t]*\s*\Z", re.S
)
_FENCE_NAME_RE = re.compile(r'\btitle="([^"\n]{1,255})"')
# A Clojure require alias: ``[babashka.http-client :as http]``.
_ALIAS_RE = re.compile(r"\[\s*([A-Za-z][\w.\-]*)\b[^\[\]]*?:as\s+([A-Za-z][\w.\-]*)")
# Code names: the kinds of mention that a code block can hold.
_CODE_KINDS = ("ident", "qualified", "path", "code")
# A lowercase word in a code span is often a command or an edge type; two
# hyphenated words in prose are often an English compound ("code-only").
_PLAIN_RE = re.compile(r"[a-z]+")
# Acronyms joined by a slash are prose: "TLS/SSL", "CI/CD", "I/O".
_ACRONYMS_RE = re.compile(r"[A-Z0-9]+(?:/[A-Z0-9]+)+")
_COMPOUND_RE = re.compile(r"[a-z]+-[a-z]+")
_SEGMENT_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_PARTS_RE = re.compile(r"[^0-9A-Za-z]+")
_SENTENCE_RE = re.compile(r"[.!?;…](?=\s)|。|\n\s*\n")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_TAG_RE = re.compile(r"(?<=\S) \([^()]{1,40}\)$")
_MARKED_RE = re.compile(
    r'"([^"\n]{3,120})"|\u201c([^\u201d\n]{3,120})\u201d|\*\*([^*\n]{3,120})\*\*|\*([^*\n]{3,120})\*'
)


def label_key(label: Any) -> str:
    """Return the match key of a label: folded case, no ``()``, no leading dot."""
    return _label_key(str(label or ""))


@lru_cache(maxsize=1 << 16)
def _label_key(label: str) -> str:
    text = " ".join((_strip_diacritics(label) if not label.isascii() else label).lower().split())
    if text.endswith("()"):
        text = text[:-2]
    # graphify tags some labels, for example ``fork-boundary-test (test)``.
    text = _TAG_RE.sub("", text)
    return text.lstrip(".").strip()


def loose_key(label: Any) -> str:
    """Return the label key with every separator removed (``shopping cart`` = ``ShoppingCart``)."""
    return re.sub(r"[^0-9a-z]+", "", label_key(label))


def is_identifier(text: str) -> bool:
    """Tell if ``text`` has the shape of a code name, not of a plain word."""
    t = text.strip()
    if not t or _ACRONYMS_RE.fullmatch(t):
        return False
    if t.endswith("()") or "_" in t or "::" in t or "/" in t:
        return True
    core = t.lstrip(".")
    if "." in core:
        return True
    if _CAMEL_RE.search(core) or _camel(core):
        return True
    parts = [p for p in core.split("-") if p]
    return len(parts) >= 2


def _camel(text: str) -> bool:
    """CamelCase in any script: a lower letter or digit, then an upper and a lower letter."""
    for a, b, c in zip(text, text[1:], text[2:]):
        if (a.islower() or a.isdigit()) and b.isupper() and c.islower():
            return True
    return False


def _camel_only(text: str) -> bool:
    """Tell if only the case shows that ``text`` is a code name."""
    t = text.strip()
    return not (t.endswith("()") or any(sep in t.lstrip(".") for sep in ("_", "::", "/", ".", "-")))


def linkable(node: dict[str, Any]) -> bool:
    """Tell if a node can be the target of a text link.

    External stubs (``babashka.process``) stay linkable: they are graphify's
    nodes for dependencies, and only an identifier or a code span reaches them.
    """
    if node.get("file_type") == "rationale":
        return False
    key = label_key(node.get("label"))
    return 2 <= len(key) <= 120 and any(ch.isalpha() for ch in key)


@dataclass
class LinkIndex:
    """A label index over one graph version.  Build it once per version."""

    nodes: dict[str, dict[str, Any]]
    keys: dict[str, list[str]]
    files: dict[str, int]
    paths: dict[str, list[str]]
    adj: dict[str, set[str]]
    total: int
    loose: dict[str, list[str]] = field(
        default_factory=dict
    )  # loose_key -> ids, for entity resolution
    _tails: dict[str, list[str]] = field(default_factory=dict, repr=False)  # path_nodes cache
    _segs: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)  # segments cache

    @classmethod
    def build(cls, nodes: Iterable[dict[str, Any]], links: Iterable[dict[str, Any]]) -> "LinkIndex":
        by_id: dict[str, dict[str, Any]] = {}
        keys: dict[str, list[str]] = {}
        key_files: dict[str, set[str]] = {}
        paths: dict[str, list[str]] = {}
        modules: dict[str, list[str]] = {}
        for node in nodes:
            nid = str(node.get("id"))
            by_id[nid] = node
            if not linkable(node):
                continue
            key = label_key(node.get("label"))
            keys.setdefault(key, []).append(nid)
            key_files.setdefault(key, set()).add(str(node.get("source_file") or nid))
            src = str(node.get("source_file") or "").lower()
            if src and _is_file_node(node):
                paths.setdefault(src, []).append(nid)
            elif src and _is_module_node(node):
                modules.setdefault(src, []).append(nid)
        # A file without a file node (a Clojure file) resolves to its namespace.
        for src, ids in modules.items():
            paths.setdefault(src, ids)
        for ids in list(keys.values()) + list(paths.values()):
            ids.sort()
        loose: dict[str, list[str]] = {}
        for key, ids in keys.items():
            lk = re.sub(r"[^0-9a-z]+", "", key)
            if lk:
                loose.setdefault(lk, []).extend(ids)
        adj: dict[str, set[str]] = {}
        for link in links:
            s, t = str(link.get("source")), str(link.get("target"))
            if s == t:
                continue
            adj.setdefault(s, set()).add(t)
            adj.setdefault(t, set()).add(s)
        files = {k: len(v) for k, v in key_files.items()}
        return cls(
            by_id,
            keys,
            files,
            paths,
            adj,
            max(len(by_id), 2),
            {k: sorted(v) for k, v in loose.items()},
        )

    def candidates(self, key: str) -> list[str]:
        return self.keys.get(key, [])

    def is_common(self, key: str) -> bool:
        """A label in ``COMMON_FILES`` or more files, or a stopword, is a common word."""
        words = key.split()
        if not words or all(w in STOPWORDS for w in words):
            return True
        return self.files.get(key, 0) >= COMMON_FILES

    def path_nodes(self, path: str) -> list[str]:
        """Return the file nodes whose ``source_file`` is ``path`` or ends with ``/path``."""
        want = path.strip().lstrip("./").lower()
        if not want:
            return []
        exact = self.paths.get(want)
        if exact:
            return list(exact)
        if "/" not in want and not _FILE_SUFFIX_RE.search(want):
            return []
        if want not in self._tails:
            tail = "/" + want
            self._tails[want] = sorted(
                nid for src, ids in self.paths.items() if src.endswith(tail) for nid in ids
            )
        return list(self._tails[want])

    def segments(self, nid: str) -> frozenset[str]:
        """Return the qualifier words of a node: its path and its owner files and modules."""
        if nid not in self._segs:
            node = self.nodes.get(nid) or {}
            src = str(node.get("source_file") or "")
            words = set(_segments(src or nid.replace("_", " ")))
            owners = [
                x
                for x in self.adj.get(nid, ())
                if _is_file_node(self.nodes.get(x) or {})
                or _is_module_node(self.nodes.get(x) or {})
            ]
            for x in sorted(owners)[:OWNERS_MAX]:
                words |= _segments(str(self.nodes[x].get("label") or ""))
                words |= _segments(str(self.nodes[x].get("source_file") or ""))
            self._segs[nid] = frozenset(words)
        return self._segs[nid]

    # -- relatedness [E2] ------------------------------------------------
    def neighbours(self, nid: str) -> set[str]:
        return self.adj.get(nid, set()) | {nid}

    def relatedness(self, a: str, b: str) -> float:
        """Milne-Witten relatedness of two nodes, plus file, directory and community signals.

        The Milne-Witten measure uses the shared neighbours.  A node counts as
        its own neighbour, so two adjacent nodes share both ends and two nodes
        at distance 2 share the middle node.
        """
        if a == b:
            return 1.0
        na, nb = self.neighbours(a), self.neighbours(b)
        shared = len(na & nb)
        mw = 0.0
        if shared:
            big, small = max(len(na), len(nb)), min(len(na), len(nb))
            denom = math.log(self.total) - math.log(small)
            if denom > 0:
                mw = 1.0 - (math.log(big) - math.log(shared)) / denom
            mw = min(1.0, max(0.0, mw))
        node_a, node_b = self.nodes.get(a) or {}, self.nodes.get(b) or {}
        src_a, src_b = str(node_a.get("source_file") or ""), str(node_b.get("source_file") or "")
        if src_a and src_a == src_b:
            same_dir = 1.0
        elif src_a and posixpath.dirname(src_a) == posixpath.dirname(src_b):
            same_dir = 0.5
        else:
            same_dir = 0.0
        com_a, com_b = node_a.get("community"), node_b.get("community")
        same_com = 1.0 if com_a is not None and com_a == com_b else 0.0
        return W_NEIGHBOURS * mw + W_DIRECTORY * same_dir + W_COMMUNITY * same_com

    def coherence(self, anchors: Iterable[str]) -> Callable[[str], float]:
        """Return ``cand -> mean relatedness to the anchors`` [E2]."""
        sums = self.coherence_sums(anchors)

        def mean(cand: str) -> float:
            total, count = sums(cand)
            return total / count if count else 0.0

        return mean

    def coherence_sums(self, anchors: Iterable[str]) -> Callable[[str], tuple[float, int]]:
        """Return ``cand -> (sum of relatedness to the anchors, anchor count)`` [E2].

        The result equals the mean of ``relatedness(cand, a)``.  An inverted
        index from each neighbour to its anchors makes one candidate cost its
        own degree, not its degree times the number of anchors.
        """
        pool = [a for a in dict.fromkeys(anchors) if a in self.nodes]
        pool_set = set(pool)
        by_neighbour: dict[str, list[str]] = {}
        by_file: dict[str, int] = {}
        by_dir: dict[str, int] = {}
        by_com: dict[Any, int] = {}
        size: dict[str, int] = {}
        for a in pool:
            na = self.neighbours(a)
            size[a] = len(na)
            for x in na:
                by_neighbour.setdefault(x, []).append(a)
            node = self.nodes[a]
            src = str(node.get("source_file") or "")
            by_file[src] = by_file.get(src, 0) + 1
            by_dir[posixpath.dirname(src)] = by_dir.get(posixpath.dirname(src), 0) + 1
            if node.get("community") is not None:
                by_com[node["community"]] = by_com.get(node["community"], 0) + 1
        log_total = math.log(self.total)

        def score(cand: str) -> tuple[float, int]:
            count = len(pool) - (cand in pool_set)
            if count <= 0:
                return 0.0, 0
            nc = self.neighbours(cand)
            shared: dict[str, int] = {}
            for x in nc:
                for a in by_neighbour.get(x, ()):
                    if a != cand:
                        shared[a] = shared.get(a, 0) + 1
            mw = 0.0
            for a, inter in shared.items():
                big, small = max(size[a], len(nc)), min(size[a], len(nc))
                denom = log_total - math.log(small)
                if denom > 0:
                    mw += min(1.0, max(0.0, 1.0 - (math.log(big) - math.log(inter)) / denom))
            node = self.nodes.get(cand) or {}
            src = str(node.get("source_file") or "")
            own = cand in pool_set
            locality = 0.0
            if src:
                same_file = by_file.get(src, 0) - own
                same_dir = by_dir.get(posixpath.dirname(src), 0) - own - same_file
                locality = same_file + 0.5 * same_dir
            com = node.get("community")
            same_com = by_com.get(com, 0) - (own and com is not None) if com is not None else 0
            return W_NEIGHBOURS * mw + W_DIRECTORY * locality + W_COMMUNITY * same_com, count

        return score

    def rank(
        self, cands: list[str], anchors: Iterable[str], table: Callable[[str], float] | None = None
    ) -> list[tuple[str, float]]:
        """Score each candidate by its mean relatedness to the anchors, best first [E2]."""
        table = table or self.coherence(anchors)
        scored = [(cand, round(table(cand), 4)) for cand in cands[:MAX_CANDIDATES]]
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return scored


def _is_file_node(node: dict[str, Any]) -> bool:
    """Tell if a node stands for a whole file (graphify labels it with the file name)."""
    src = str(node.get("source_file") or "")
    if not src:
        return False
    return node.get("node_kind") == "page" or _is_file_node_label(node.get("label"), src)


def _is_module_node(node: dict[str, Any]) -> bool:
    meta = node.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    return meta.get("kind") in ("namespace", "file") or node.get("type") in ("module", "package")


@dataclass
class Mention:
    text: str
    key: str
    start: int
    kind: str  # code, path, ident, qualified, phrase, marked or proper
    named: bool = False  # a phrase inside a sentence, not at its start
    exact: bool = False  # only a label with the same case fits (``GatewayClient``)
    weak: bool = False  # a plain word (``calls``, "tool-call"): the context must agree
    full: str = ""  # the name with its require alias expanded (``wire/x`` -> ``a.b.wire/x``)


@dataclass
class Decision:
    """The result of linking one document."""

    links: list[dict[str, Any]] = field(default_factory=list)
    possible: list[dict[str, Any]] = field(default_factory=list)
    # ``ambiguous_code_names``: names in code that stayed open without a review item.
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def ids(self) -> list[str]:
        return [x["id"] for x in self.links]


def _mask(text: str) -> str:
    """Blank fenced code blocks, links, tags and urls.  Offsets stay the same."""

    def blank(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    return _SKIP_RE.sub(blank, _FENCE_RE.sub(blank, text))


def _sentence_start(text: str, pos: int) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    return bool(_SENTENCE_END_RE.search(text[line_start:pos]))


def spot(text: str, index: LinkIndex) -> list[Mention]:
    """Find the candidate mentions in ``text`` [E1], [E3]."""
    masked = _mask(text)
    found: list[Mention] = []
    # A coverage mask keeps the overlap test linear in the text size.
    covered = bytearray(len(masked) + 1)
    code_spans: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return not any(covered[a:b])

    def take(a: int, b: int) -> None:
        covered[a:b] = b"\x01" * (b - a)

    # 1. Code spans: the strongest evidence.
    for m in _CODE_SPAN_RE.finditer(masked):
        inner = m.group(2).strip()
        start = m.start(2)
        code_spans.append((m.start(), m.end()))
        take(m.start(), m.end())
        if not inner:
            continue
        if _path_like(inner) and index.path_nodes(inner):
            found.append(Mention(inner, inner.lower().lstrip("./"), start, "path"))
            continue
        key = label_key(inner)
        if index.candidates(key):
            found.append(Mention(inner, key, start, "code", weak=bool(_PLAIN_RE.fullmatch(inner))))
            continue
        if any(sep in inner.strip(".").rstrip("()") for sep in ("::", ".", "/")):
            found.append(Mention(inner, label_key(inner), start, "qualified"))
    # Blank code spans so the prose passes do not see them again.
    prose = list(masked)
    for a, b in code_spans:
        prose[a:b] = " " * (b - a)
    prose_text = "".join(prose)
    # 2. Identifier-shaped tokens in prose.
    for m in _IDENT_RE.finditer(prose_text):
        tok = m.group(0).rstrip(".-")
        if not is_identifier(tok):
            continue
        a, b = m.start(), m.start() + len(tok)
        key = label_key(tok)
        exact = _camel_only(tok)
        if exact and not any(_same_case(index.nodes[c], tok) for c in index.candidates(key)):
            continue
        if index.candidates(key):
            found.append(
                Mention(tok, key, a, "ident", exact=exact, weak=bool(_COMPOUND_RE.fullmatch(tok)))
            )
            take(a, b)
        elif _path_like(tok) and index.path_nodes(tok):
            found.append(Mention(tok, tok.lower(), a, "path"))
            take(a, b)
        elif any(sep in tok.strip(".").rstrip("()") for sep in ("::", ".", "/")):
            found.append(Mention(tok, key, a, "qualified"))
            take(a, b)
    # 3. Labels of two to four words, longest first, never common phrases.
    marked = bytearray(len(prose_text) + 1)
    for m in _MARKED_RE.finditer(prose_text):
        marked[m.start() : m.end()] = b"\x01" * (m.end() - m.start())
    words = [(w.group(0), w.start(), w.end()) for w in _WORD_RE.finditer(prose_text)]
    i = 0
    while i < len(words):
        step = 1
        for n in (4, 3, 2):
            if i + n > len(words):
                continue
            span = words[i : i + n]
            gap_ok = all(
                prose_text[span[k][2] : span[k + 1][1]].strip() == "" for k in range(n - 1)
            )
            if not gap_ok:
                continue
            a, b = span[0][1], span[-1][2]
            key = label_key(" ".join(w for w, _, _ in span))
            if free(a, b) and index.candidates(key) and not index.is_common(key):
                kind = "marked" if all(marked[a:b]) else "phrase"
                # A phrase needs two content words: "How it works" is prose.
                if sum(label_key(w) not in STOPWORDS for w, _, _ in span) < 2:
                    continue
                named = not _sentence_start(prose_text, a)
                found.append(Mention(prose_text[a:b], key, a, kind, named))
                take(a, b)
                step = n
                break
        i += step
    # 4. A capitalised word inside a sentence that equals a code label exactly.
    for pos, (w, a, b) in enumerate(words):
        if not (w[:1].isupper() and w[1:].islower() and len(w) >= 3) or not free(a, b):
            continue
        if _in_title_run(prose_text, words, pos):
            continue
        key = label_key(w)
        if key in STOPWORDS or index.is_common(key) or _sentence_start(prose_text, a):
            continue
        if any(_proper_match(index.nodes[c], w) for c in index.candidates(key)):
            found.append(Mention(w, key, a, "proper"))
            take(a, b)
    found.sort(key=lambda x: x.start)
    return found


def _path_like(text: str) -> bool:
    """True for ``a/b`` or ``name.ext``; a bare suffix such as ``.pas`` is no path."""
    if "/" in text:
        return True
    m = _FILE_SUFFIX_RE.search(text)
    return bool(m) and bool(text[: m.start()].strip("."))


def _in_title_run(text: str, words: list[tuple[str, int, int]], pos: int) -> bool:
    """True when a capitalised neighbour makes the word part of a longer name ("Long Beach")."""
    for other in (pos - 1, pos + 1):
        if 0 <= other < len(words):
            w, a, b = words[other]
            lo, hi = (b, words[pos][1]) if other < pos else (words[pos][2], a)
            if w[:1].isupper() and w.lower() not in STOPWORDS and text[lo:hi] in (" ", "-"):
                return True
    return False


def _same_case(node: dict[str, Any], word: str) -> bool:
    label = str(node.get("label") or "").lstrip(".")
    if label.endswith("()"):
        label = label[:-2]
    return label == word.lstrip(".").removesuffix("()")


def _case_only(node: dict[str, Any], word: str) -> bool:
    """True when the label equals ``word`` only if the case is ignored."""
    label = str(node.get("label") or "").lstrip(".").removesuffix("()")
    text = word.lstrip(".").removesuffix("()")
    return label != text and label.lower() == text.lower()


def _proper_match(node: dict[str, Any], word: str) -> bool:
    if node.get("file_type") == "code":
        return _same_case(node, word)
    return _is_page_title(node) and str(node.get("label") or "") == word


def _is_page_title(node: dict[str, Any]) -> bool:
    """A page node, or the heading on the first line of a page ("Drafts")."""
    if node.get("file_type") != "document":
        return False
    return node.get("node_kind") == "page" or (
        node.get("node_kind") == "heading" and node.get("source_location") == "L1"
    )


def _candidates_for(m: Mention, index: LinkIndex) -> list[str]:
    if m.kind == "path":
        return index.path_nodes(m.text)
    if m.kind == "proper":
        return [c for c in index.candidates(m.key) if _proper_match(index.nodes[c], m.text)]
    if m.kind == "qualified":
        return _qualified(m.full or m.text, index)
    if m.exact:
        return [c for c in index.candidates(m.key) if _same_case(index.nodes[c], m.text)]
    if m.kind == "phrase":
        # A section heading reads like prose ("Continuous deployment").  Plain
        # text links it as a name (the same case, inside a sentence), or when
        # its label of two or more words is unique in the graph (score 0.65).
        cands = index.candidates(m.key)
        unique = (
            len(cands) == 1
            and len(m.key.split()) >= 2
            and _full_key(index.nodes[cands[0]]) == m.key
        )
        return [
            c
            for c in cands
            if not _is_heading(index.nodes[c]) or unique or _named_heading(index.nodes[c], m)
        ]
    if m.kind in ("code", "ident"):
        # Code-shaped text names code, not a section heading of a page.  Code
        # is case-sensitive: ``Task`` does not name ``task()``.
        return [
            c
            for c in index.candidates(m.key)
            if not _is_heading(index.nodes[c]) and not _case_only(index.nodes[c], m.text)
        ]
    return index.candidates(m.key)


def _is_heading(node: dict[str, Any]) -> bool:
    return (
        node.get("file_type") == "document"
        and not node.get("doc_id")
        and node.get("node_kind") != "page"
    )


def _full_key(node: dict[str, Any]) -> str:
    """The label key with any graphify tag kept: ``Companion app (iPhone / Android)`` stays whole."""
    return " ".join(_strip_diacritics(str(node.get("label") or "")).lower().split())


def _named_heading(node: dict[str, Any], m: Mention) -> bool:
    return m.named and str(node.get("label")) == m.text


def _segments(text: str) -> frozenset[str]:
    """Split text into lower-case words: path parts and their camel-case parts."""
    out = set()
    for part in _PARTS_RE.split(text):
        if part:
            out.add(part.lower())
            out.update(p.lower() for p in _SEGMENT_RE.findall(part))
    return frozenset(w for w in out if len(w) > 1 and w not in STOPWORDS)


class _Scopes:
    """Qualifier words of the sentence, the paragraph and the whole document around a position."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.bounds = [
            [0] + [m.end() for m in rx.finditer(text)] + [len(text) + 1]
            for rx in (_SENTENCE_RE, _PARAGRAPH_RE)
        ]
        self.cache: dict[tuple[int, int], frozenset[str]] = {}

    def paragraph(self, pos: int) -> int:
        return bisect_right(self.bounds[1], pos) - 1

    def words(self, tier: int, pos: int) -> frozenset[str]:
        if tier >= len(self.bounds):
            key = (tier, 0)
            if key not in self.cache:
                self.cache[key] = _segments(self.text)
            return self.cache[key]
        bounds = self.bounds[tier]
        i = bisect_right(bounds, pos) - 1
        if (tier, i) not in self.cache:
            self.cache[(tier, i)] = _segments(self.text[bounds[i] : bounds[i + 1]])
        return self.cache[(tier, i)]


def _qualified(text: str, index: LinkIndex) -> list[str]:
    """Resolve ``Owner.member``, ``ns::member`` or a Clojure ``alias/var``.

    A member candidate is kept only when a graph neighbour (its class, file
    or namespace) has a key that ends with the owner, on a name boundary.
    ``gateway-client/request!`` so finds ``request!`` in the namespace
    ``com.blockether.vis.internal.gateway.client``.  A dotted module name
    (``blockether.vis.extension``) first resolves to its file.  For a Clojure
    alias only, a neighbour that contains the owner as whole parts counts.
    """
    raw = text.strip().rstrip("()")
    files = _module_files(raw, index)
    if files:
        return files
    cut = max(raw.rfind("::"), raw.rfind("/"), raw.rfind("."))
    if cut <= 0:
        return []
    sep_len = 2 if raw[cut : cut + 2] == "::" else 1
    alias = raw[cut] == "/"
    owner, member = raw[:cut].strip("."), raw[cut + sep_len :]
    owner_key = _dotted(label_key(owner))
    if len(owner_key.replace(".", "")) < 2 or not member:
        return []
    suffix, inside = [], []
    for cand in index.candidates(label_key(member)):
        # The owner can be a neighbour (class, namespace, file node) or the
        # candidate's own file: ``gateway-client`` = ``gateway/client.clj``.
        # ``-``, ``.``, ``/`` and ``_`` are one separator.
        own = str((index.nodes.get(cand) or {}).get("source_file") or "")
        keys = [
            _dotted(_FILE_SUFFIX_RE.sub("", label_key(x)))
            for x in [own]
            + [(index.nodes.get(nb) or {}).get("label") for nb in index.adj.get(cand, ())]
            if x
        ]
        if any(k == owner_key or k.endswith("." + owner_key) for k in keys):
            suffix.append(cand)
        elif alias and any(f".{owner_key}." in f".{k}." for k in keys):
            inside.append(cand)
    # An alias often names the end of a namespace; else any part of it.
    return suffix or inside


def _module_files(raw: str, index: LinkIndex) -> list[str]:
    """Resolve a dotted module name to its file node (Python, Clojure, JS)."""
    if "." not in raw or any(sep in raw for sep in ("/", "::", " ")):
        return []
    parts = [p for p in raw.strip(".").split(".") if p]
    if len(parts) < 2:
        return []
    base = "/".join(parts)
    clj = "/".join(p.replace("-", "_") for p in parts)
    for path in (
        f"{base}.py",
        f"{base}/__init__.py",
        f"{clj}.clj",
        f"{clj}.cljc",
        f"{base}.ts",
        f"{base}.js",
    ):
        found = index.path_nodes(path)
        if found:
            return found
    return []


_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|test-\w+|spec|__tests__)/"
    r"|(?:^|/)test_[^/]*$"
    r"|[_.\-](?:test|tests|spec)\.[A-Za-z0-9]+$"
)


def is_test_file(node: dict[str, Any]) -> bool:
    """Tell if a node lives in a test file (``tests/``, ``*_test.clj``, ``test_*.py``, ``*.spec.ts``)."""
    return bool(_TEST_PATH_RE.search(str(node.get("source_file") or "").replace("\\", "/")))


def _dotted(key: str) -> str:
    """Join the parts of a name with dots: ``gateway-client`` -> ``gateway.client``."""
    return ".".join(p for p in re.split(r"[\s\-_./:]+", key) if p)


def _mention_score(m: Mention) -> float:
    return SCORE_CONTEXT if m.weak else _base_score(m.kind)


def _base_score(kind: str) -> float:
    if kind in ("code", "path", "ident", "qualified"):
        return SCORE_CODE
    if kind in ("phrase", "marked"):
        return SCORE_PHRASE
    return SCORE_CONTEXT


def _view(index: LinkIndex, nid: str, score: float | None = None) -> dict[str, Any]:
    node = index.nodes.get(nid) or {}
    out = {"id": nid, "label": node.get("label"), "source_file": node.get("source_file")}
    if score is not None:
        out["score"] = score
    return out


def link_text(
    text: str,
    index: LinkIndex,
    *,
    anchors: Iterable[str] = (),
    exclude: Iterable[str] = (),
    limit: int = 60,
    title: str | None = None,
) -> Decision:
    """Link the names in ``text`` to nodes.

    ``anchors`` are nodes that the document already names for sure (explicit
    nodes and wiki links).  ``exclude`` are nodes never to link (the document
    node itself).  ``title`` is the document title; a code document without a
    ``title="..."`` fence attribute takes its file name from it.

    D11: a code document (one fenced block) links the graph file of the same
    name ("same_file") and the code names inside the block.  An ambiguous
    code name gives no review item: a code file would flood the review queue.
    ``Decision.stats["ambiguous_code_names"]`` counts them.
    """
    banned = set(exclude)
    code_doc = _code_document(text)
    in_code: list[Mention] = []
    if code_doc or FENCED_CODE_NAMES:
        # A plain word in code is mostly a comment word; code names have a code shape.
        in_code = [m for m in spot(_code_text(text), index) if m.kind in _CODE_KINDS and not m.weak]
    mentions = sorted(([] if code_doc else spot(text, index)) + in_code, key=lambda x: x.start)
    code_at = {m.start for m in in_code}
    decision = Decision()
    linked: dict[str, dict[str, Any]] = {}
    same_file: list[str] = []
    file_mention = Mention("", "", 0, "path")
    if code_doc:
        named = _FENCE_NAME_RE.search(code_doc.group("info"))
        name = posixpath.basename((named.group(1) if named else title or "").replace("\\", "/"))
        file_mention = Mention(name.strip(), name.strip().lower(), 0, "path")
        same_file = [c for c in _same_name_files(index, file_mention.key) if c not in banned]
        if len({str(index.nodes[c].get("source_file")) for c in same_file}) == 1:
            # The file node first, then its namespace or module node.
            nid = min(same_file, key=lambda c: (not _is_file_node(index.nodes[c]), c))
            linked[nid] = _link(nid, file_mention, SCORE_CODE, "same_file", 1)
            same_file = []
            # The names that the file itself defines are inside the file node already.
            own = str(index.nodes[nid].get("source_file") or "")
            kept = [m for m in mentions if not _defined_in(index, m, own)]
            decision.stats["own_names"] = len({m.key for m in mentions} - {m.key for m in kept})
            mentions = kept
    # The text's own require aliases decide their names: ``http/client`` after
    # ``[babashka.http-client :as http]`` is in that namespace or nowhere.
    aliases = {a: ns for ns, a in _ALIAS_RE.findall(text) if "." in ns}
    for m in mentions:
        owner = m.text.split("/", 1)[0] if m.kind == "qualified" and "/" in m.text else ""
        if owner in aliases:
            m.full = aliases[owner] + m.text[len(owner) :]
    scopes = _Scopes(text)
    # One sense per paragraph: a document can compare two nodes of the same name (Gale et al. 1992).
    by_key: dict[tuple[str, str, int], list[Mention]] = {}
    for m in mentions:
        kind = m.kind if m.kind in ("path", "qualified", "proper") else ""
        by_key.setdefault((kind, m.key, scopes.paragraph(m.start)), []).append(m)
    pending: list[tuple[Mention, list[str], int, list[Mention]]] = []
    weak: list[tuple[str, Mention, int]] = []
    for group in by_key.values():
        best = max(group, key=_mention_score)
        cands = [c for c in _candidates_for(best, index) if c not in banned]
        if not cands:
            continue
        if len(cands) == 1 and best.kind == "proper" and _is_page_title(index.nodes[cands[0]]):
            # A unique page title inside a sentence names that page ("the Drafts page").
            if cands[0] not in linked:
                linked[cands[0]] = _link(cands[0], best, SCORE_HEADING, "unique", len(group))
        elif len(cands) == 1 and (best.kind == "proper" or best.weak):
            weak.append((cands[0], best, len(group)))
        elif len(cands) == 1:
            nid = cands[0]
            score = _base_score(best.kind)
            node = index.nodes[nid]
            if best.kind == "phrase" and _is_heading(node) and not _named_heading(node, best):
                score = SCORE_HEADING
            if nid not in linked or linked[nid]["confidence_score"] < score:
                linked[nid] = _link(nid, best, score, "unique", len(group))
        else:
            pending.append((best, cands, len(group), group))
    context = list(dict.fromkeys(list(anchors) + list(linked)))
    doc_sums = index.coherence_sums(context)
    # The paragraphs where each linked node is named; caller anchors count everywhere.
    in_para: dict[int, list[str]] = {}
    for (_, _, para), group in by_key.items():
        for nid in linked:
            if any(linked[nid]["mention"] == g.text for g in group):
                in_para.setdefault(para, []).append(nid)
    tables: dict[int, Callable[[str], float]] = {}

    def table_for(para: int) -> Callable[[str], float]:
        """Mean relatedness with paragraph anchors at weight 1 and the others at ``FAR_WEIGHT``."""
        if para not in tables:
            local = list(dict.fromkeys(list(anchors) + in_para.get(para, [])))
            if len(local) == len(context) or not in_para.get(para):
                tables[para] = index.coherence(context)
            else:
                local_sums = index.coherence_sums(local)

                def weighted(cand: str, local_sums=local_sums) -> float:
                    total, count = doc_sums(cand)
                    near, near_count = local_sums(cand)
                    weight = near_count + FAR_WEIGHT * (count - near_count)
                    return (near + FAR_WEIGHT * (total - near)) / weight if weight else 0.0

                tables[para] = weighted
        return tables[para]

    modules = _named_modules(index, linked, anchors, mentions, text)
    # A lone capitalised or plain word is a weak name: a unique label is not enough, the context must agree [E2].
    for nid, m, count in weak:
        rel = table_for(scopes.paragraph(m.start))(nid)
        if nid in linked or rel < MIN_RELATEDNESS:
            continue
        if is_test_file(index.nodes[nid]):
            # A lone test-file candidate of a weak name is only a possible link.
            decision.possible.append(_possible(index, m, [nid], [(nid, rel)], "test_file"))
            continue
        linked[nid] = _link(nid, m, SCORE_CONTEXT, "context", count)
    # Words that name alternatives for one name ("On iOS and Android") qualify nothing else there.
    contested: dict[int, frozenset[str]] = {}
    names = frozenset().union(
        *(_segments(x.text) for x in mentions if x.kind in ("ident", "code", "proper"))
    )
    first: list[tuple[str | None, float, str, frozenset[str]]] = []

    def prior(ranked: list[tuple[str, float]]) -> list[tuple[str, float]]:
        # Test files rank lower when the context is weak or tied.
        out = [(c, x * TEST_PRIOR if is_test_file(index.nodes[c]) else x) for c, x in ranked]
        return sorted(out, key=lambda r: -r[1])

    def generic(m: Mention, cands: list[str]) -> bool:
        # A common name with many senses (``get()``): only nearby words can decide it.
        return index.is_common(m.key) and len(cands) > COLLECTIVE_MAX

    for m, cands, _count, group in pending:
        depth = 2 if generic(m, cands) else 3
        first.append(_qualify(index, group, cands, scopes, names=names, depth=depth))
        if first[-1][2] == "nil":
            para = scopes.paragraph(m.start)
            contested[para] = contested.get(para, frozenset()) | first[-1][3]
    # Each distinct ambiguous name votes once in the collective pass, however often it repeats.
    distinct = list(dict.fromkeys(tuple(c) for _, c, _, _ in pending if len(c) <= COLLECTIVE_MAX))
    # The collective rank depends only on the candidate set: a name repeated in
    # many paragraphs is ranked once.
    collective: dict[tuple[str, ...], list[tuple[str, float]]] = {}
    for pos, (m, cands, count, group) in enumerate(pending):
        others = [list(c) for c in distinct if c != tuple(cands)]
        if m.weak:
            # A plain word with several senses never links by itself [E2].  With
            # context evidence it is a possible link; without, a non-link [E6].
            ranked = prior(index.rank(cands, context, table_for(scopes.paragraph(m.start))))
            winner = first[pos][0]
            if winner:
                ranked = [(winner, first[pos][1])] + [x for x in ranked if x[0] != winner]
            if (winner or ranked[0][1] >= MIN_RELATEDNESS) and not any(c in linked for c in cands):
                decision.possible.append(_possible(index, m, cands, ranked, "weak_name"))
            continue
        # Each mention gets its own scope: one surface can name two entities.
        scoped: dict[str, Mention] = {}
        for g in group:
            nid = _scope(index, g, cands, modules, text)
            if nid:
                scoped.setdefault(nid, g)
        for nid, g in scoped.items():
            if nid not in linked:
                rec = _link(
                    nid,
                    g,
                    SCORE_SCOPE,
                    "scope",
                    sum(1 for x in group if _scope(index, x, cands, modules, text) == nid),
                )
                rec["candidates"] = len(cands)
                linked[nid] = rec
        if scoped:
            continue
        winner, q_score, status, _ = first[pos]
        if status != "nil" and scopes.paragraph(m.start) in contested:
            para_words = contested[scopes.paragraph(m.start)]
            winner, q_score, status, _ = _qualify(
                index, group, cands, scopes, para_words, names, depth=2 if generic(m, cands) else 3
            )
        if winner:
            if winner not in linked:
                rec = _link(winner, m, q_score, "qualifier", count)
                rec["candidates"] = len(cands)
                linked[winner] = rec
            continue
        ranked = prior(index.rank(cands, context, table_for(scopes.paragraph(m.start))))
        if status == "nil":
            if not any(c in linked for c in cands):
                decision.possible.append(_possible(index, m, cands, ranked, "ambiguous_context"))
            continue
        if generic(m, cands):
            # Review 2 item 5: far context never decides a generic name.
            if not any(c in linked for c in cands):
                decision.possible.append(_possible(index, m, cands, ranked, "no_context"))
            continue
        if not context or ranked[0][1] < MIN_RELATEDNESS:
            key = tuple(cands)
            if key not in collective:
                collective[key] = _collective(index, cands, others)
            ranked = prior(collective[key] or ranked)
        top, top_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = (top_score - second) / top_score if top_score > 0 else 0.0
        if top_score >= MIN_RELATEDNESS and margin >= LINK_MARGIN:
            if top not in linked:
                rec = _link(top, m, SCORE_CONTEXT, "context", count)
                rec["margin"] = round(margin, 3)
                rec["candidates"] = len(cands)
                linked[top] = rec
            continue
        if top in linked:
            continue
        reason = "no_context" if top_score < MIN_RELATEDNESS else "low_margin"
        decision.possible.append(_possible(index, m, cands, ranked, reason))
    # Ambiguous names in code stay open without a review item; the stats count them.
    open_code = [x for x in decision.possible if x["offset"] in code_at]
    decision.possible = [x for x in decision.possible if x["offset"] not in code_at]
    decision.stats["ambiguous_code_names"] = len({label_key(x["mention"]) for x in open_code})
    if same_file:
        # Several files have the document's name: one review item names them.
        ranked = prior(index.rank(same_file, list(dict.fromkeys(context + list(linked)))))
        decision.possible.append(
            _possible(index, file_mention, same_file, ranked, "same_file_name")
        )
    ordered = sorted(linked.values(), key=lambda x: (-x["confidence_score"], x["offset"]))
    decision.links = ordered[: max(0, int(limit))]
    # A name that several paragraphs leave open is one review item.
    seen: set[tuple[str, ...]] = set()
    possible = []
    for item in sorted(decision.possible, key=lambda x: x["offset"]):
        key = (label_key(item["mention"]), *sorted(c["id"] for c in item["candidates"]))
        if key not in seen:
            seen.add(key)
            possible.append(item)
    decision.possible = possible
    return decision


def _code_document(text: str) -> re.Match[str] | None:
    """Match a text that is one fenced block and nothing else (a code document)."""
    m = _CODE_DOC_RE.fullmatch(text)
    if not m:
        return None
    fence = m.group("fence")
    # A fence line of the same kind and length inside would close the block earlier.
    closer = re.compile(rf"(?m)^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
    return None if closer.search(m.group("code")) else m


def _code_text(text: str) -> str:
    """Blank all but the code inside fenced blocks.  Offsets stay the same."""
    out = re.sub(r"[^\n]", " ", text)
    for m in _FENCE_RE.finditer(text):
        block = m.group(0)
        first, last = block.find("\n"), block.rfind("\n")
        if 0 <= first < last:
            a, b = m.start() + first + 1, m.start() + last
            out = out[:a] + text[a:b] + out[b:]
    return out


def _defined_in(index: LinkIndex, m: Mention, src: str) -> bool:
    """True when a candidate of ``m`` is defined in the file ``src``."""
    return any(
        str(index.nodes[c].get("source_file") or "") == src for c in _candidates_for(m, index)
    )


def _same_name_files(index: LinkIndex, name: str) -> list[str]:
    """Return the file or module nodes whose ``source_file`` is ``name`` or ends with ``/name``."""
    want = name.strip().lstrip("./").lower()
    if not want or "/" in want or not _path_like(want):
        return []
    tail = "/" + want
    return sorted(
        nid for src, ids in index.paths.items() if src == want or src.endswith(tail) for nid in ids
    )


def _possible(
    index: LinkIndex, m: Mention, cands: list[str], ranked: list[tuple[str, float]], reason: str
) -> dict[str, Any]:
    return {
        "mention": m.text,
        "offset": m.start,
        "kind": m.kind,
        "reason": reason,
        "candidates_total": len(cands),
        "candidates": [_view(index, nid, score) for nid, score in ranked[:REVIEW_TOP]],
    }


def _qualify(
    index: LinkIndex,
    group: list[Mention],
    cands: list[str],
    scopes: _Scopes,
    contested: frozenset[str] = frozenset(),
    names: frozenset[str] = frozenset(),
    depth: int = 3,
) -> tuple[str | None, float, str, frozenset[str]]:
    """Decide by qualifier words near the mention, nearest scope first.

    A qualifier is a word of a candidate's path, module or owner file
    ("Android" for ``native/android/OAuthLoopback.java``).  Words shared by
    every candidate say nothing; the others weigh ``log(n/df) / log(n)``
    (inverse document frequency over the candidates).  The sentence decides
    first, then the paragraph, then the document at half weight.  Farther
    words never outvote nearer ones.  Candidates in one file count as one
    place.  When two places both have evidence in the sentence or paragraph,
    the text is ambiguous: return ``nil`` and the words that caused it.
    ``contested`` words named alternatives elsewhere in the paragraph; they
    are no evidence.  ``names`` are the words of other code names in the
    text: a name is another entity, not a qualifier.

    ``depth`` limits the scopes: 2 keeps the sentence and the paragraph only.

    Return ``(node, score, status, words)``; ``status`` is ``link``, ``nil`` or ``none``.
    """
    none = (None, 0.0, "none", frozenset())
    cands = cands[:MAX_CANDIDATES]
    n = len(cands)
    if n < 2:
        return none
    own: set[str] = set(names)
    for g in group:
        own |= _segments(g.text)
    segs = {c: index.segments(c) - own for c in cands}
    df: Counter[str] = Counter(w for words in segs.values() for w in words)
    excluded = own | contested
    texts = {g.text.lstrip(".").removesuffix("()") for g in group}
    # The word sets of each scope; nearer scopes are inside farther ones.  No set is copied.
    windows = [
        list({id(w): w for w in (scopes.words(t, g.start) for g in group)}.values())
        for t in range(3)
    ]

    def first_tier(word: str) -> int:
        return next((t for t in range(3) if any(word in w for w in windows[t])), 3)

    tiers = {w: first_tier(w) for w in df if df[w] < n and w not in excluded}
    for tier in range(depth):
        weight = DOC_WEIGHT if tier == 2 else 1.0
        hits = {c: {w for w in segs[c] if tiers.get(w) == tier} for c in cands}
        scores = {
            c: weight * sum(math.log(n / df[w]) / math.log(n) for w in hits[c]) for c in cands
        }
        if not any(scores.values()):
            continue
        # The best candidate of each file: the class and its constructor are one place.
        places: dict[str, str] = {}
        for c in sorted(
            cands,
            key=lambda c: (
                -scores[c],
                str(index.nodes[c].get("label")).lstrip(".").removesuffix("()") not in texts,
                c,
            ),
        ):
            places.setdefault(str(index.nodes[c].get("source_file") or c), c)
        ranked = sorted(places.values(), key=lambda c: -scores[c])
        q1 = scores[ranked[0]]
        q2 = scores[ranked[1]] if len(ranked) > 1 else 0.0
        if len(ranked) > 1:
            # Contrastive evidence: the runner-up has its own words ("iOS" against "Android").
            rival = hits[ranked[1]] - hits[ranked[0]]
            q_rival = weight * sum(math.log(n / df[w]) / math.log(n) for w in rival)
            # Two places with comparable words make the text ambiguous ("iOS"
            # against "Android").  A clear majority still decides: more words
            # and EVIDENCE_RATIO times the weight ("companion app" against "engine").
            majority = len(hits[ranked[0]]) > len(rival) and q1 >= EVIDENCE_RATIO * q_rival
            if tier < 2 and q_rival >= MIN_EVIDENCE / 2 and not majority:
                return None, q1, "nil", frozenset(hits[ranked[0]] | hits[ranked[1]])
        if q1 >= MIN_EVIDENCE and q1 >= EVIDENCE_RATIO * q2 and q1 - q2 >= MIN_EVIDENCE / 2:
            return (
                ranked[0],
                SCORE_SCOPE if tier < 2 else SCORE_CONTEXT,
                "link",
                frozenset(hits[ranked[0]]),
            )
        return none
    return none


def _named_modules(
    index: LinkIndex,
    linked: dict[str, dict[str, Any]],
    anchors: Iterable[str],
    mentions: list[Mention],
    text: str,
) -> dict[str, list[int]]:
    """Map each file that the text names (a file or module node) to its offsets.

    An anchor given by the caller has no offset; it counts as named at the top.
    """
    out: dict[str, list[int]] = {}
    for nid in anchors:
        node = index.nodes.get(nid) or {}
        src = str(node.get("source_file") or "")
        if src and (_is_file_node(node) or _is_module_node(node)):
            out.setdefault(src, []).append(-1)
    by_text: dict[str, list[int]] = {}
    for m in mentions:
        by_text.setdefault(m.text, []).append(m.start)
    for nid, rec in linked.items():
        node = index.nodes.get(nid) or {}
        src = str(node.get("source_file") or "")
        if src and (_is_file_node(node) or _is_module_node(node)):
            out.setdefault(src, []).extend(by_text.get(rec["mention"], [rec["offset"]]))
    return out


def _scope(
    index: LinkIndex, m: Mention, cands: list[str], modules: dict[str, list[int]], text: str
) -> str | None:
    """Pick the candidate that a named module defines, like a reader does.

    "Its `request!` function" after ``gateway.client`` means the
    ``request!`` of that namespace.  graphify resolves calls the same way:
    by the import scope of the file.  With one such candidate it wins.  With
    several, the nearest module wins: named before the mention within
    ``SCOPE_WINDOW`` characters, or after it in the same sentence within
    ``SCOPE_AFTER``.  Else the context ranking decides.
    """
    if not modules:
        return None
    hits = [c for c in cands if str((index.nodes.get(c) or {}).get("source_file") or "") in modules]
    if len(hits) == 1:
        return hits[0]
    near = []
    for c in hits:
        for at in modules[str(index.nodes[c].get("source_file"))]:
            if 0 <= at < m.start and m.start - at <= SCOPE_WINDOW:
                near.append((m.start - at, c))
            elif (
                at > m.start
                and at - m.start <= SCOPE_AFTER
                and not re.search(r"[.!?]\s", text[m.start : at])
            ):
                near.append((at - m.start, c))
    near.sort()
    if near and (len(near) == 1 or near[0][0] != near[1][0] or near[0][1] == near[1][1]):
        return near[0][1]
    return None


def _collective(
    index: LinkIndex, cands: list[str], others: list[list[str]]
) -> list[tuple[str, float]]:
    """Rank by coherence with the other ambiguous names when no anchor helps.

    Collective disambiguation picks the candidates that fit together
    (Cucerzan, EMNLP-CoNLL 2007; Hoffart et al., EMNLP 2011).  Only names
    with at most ``COLLECTIVE_MAX`` candidates take part, so very common names
    such as ``main()`` or ``run()`` never vote and are never chosen this way.
    """
    if len(cands) > COLLECTIVE_MAX:
        return []
    groups = [g for g in others if len(g) <= COLLECTIVE_MAX]
    if len(groups) < 2:
        return []
    scored = []
    for cand in cands:
        score = sum(max(index.relatedness(cand, o) for o in g) for g in groups) / len(groups)
        scored.append((cand, round(score, 4)))
    scored.sort(key=lambda kv: (-kv[1], kv[0]))
    return scored


def _link(nid: str, m: Mention, score: float, method: str, count: int) -> dict[str, Any]:
    return {
        "id": nid,
        "mention": m.text,
        "offset": m.start,
        "kind": m.kind,
        "method": method,
        "confidence_score": score,
        "count": count,
    }


def wiki_links(text: str) -> list[tuple[str, str | None, int]]:
    """Return ``(target, class, offset)`` for each ``[[target]]`` or ``[[target|class]]``.

    Wiki links inside fenced code blocks are text, not links.
    """
    fences = [(m.start(), m.end()) for m in _FENCE_RE.finditer(text)]
    out = []
    for m in _WIKI_RE.finditer(text):
        if any(a <= m.start() < b for a, b in fences):
            continue
        target = m.group(1).strip()
        cls = (m.group(2) or "").strip() or None
        if target:
            out.append((target, cls, m.start()))
    return out


@dataclass
class WikiResult:
    status: str  # id, unique, ambiguous or missing
    ids: list[str]
    target: str
    cls: str | None = None


def resolve_wiki(
    target: str,
    index: LinkIndex,
    *,
    cls: str | None = None,
    class_of: Callable[[dict[str, Any]], str | None] | None = None,
    onto: Any = None,
    anchors: Iterable[str] = (),
) -> WikiResult:
    """Resolve ``[[target]]`` or ``[[target|class]]``.

    An exact node id wins.  Else the label must fit exactly one node (of the
    class, when one is given).  With ``onto``, the subclasses fit too:
    ``[[run|code]]`` finds a ``code.method`` node.  An ambiguous label is
    never guessed: the caller stores it for review and creates no duplicate
    node (memo R4).
    """
    name = str(target or "").strip()
    if name in index.nodes:
        return WikiResult("id", [name], name, cls)
    cands = index.candidates(label_key(name))
    if cls and class_of is not None:
        if onto is not None:
            cands = [c for c in cands if onto.is_a(class_of(index.nodes[c]), cls)]
        else:
            cands = [c for c in cands if class_of(index.nodes[c]) == cls]
    if len(cands) == 1:
        return WikiResult("unique", cands, name, cls)
    if not cands:
        return WikiResult("missing", [], name, cls)
    ranked = index.rank(cands, anchors)
    return WikiResult("ambiguous", [nid for nid, _ in ranked], name, cls)
