"""Exact references and renames of Clojure vars, on the extractor's resolution.

``extract_clojure(path, positions=True)`` records every resolved symbol with its
byte range. This module binds those sites to one var across files with the
same rule as ``resolve_clojure_references``: the first candidate namespace that
defines the name wins. Locals, quoted data, strings and keywords never match.
"""
from __future__ import annotations

import re
from pathlib import Path

from graphify.extractors.clojure import CLOJURE_SUFFIXES, extract_clojure
from graphify.refactor import Edit, Mention, Occurrence, RenamePlan, StaleFile, apply_rename, digest, rewrite

__all__ = ["Occurrence", "RenamePlan", "StaleFile", "apply_rename", "plan_rename", "references", "rewrite"]

# Characters that end a Clojure token. Anything else can be part of a symbol.
_TOKEN = re.compile(rb"[^\s()\[\]{}\"\\;@^`~,]+")
# Strings, comments and character literals: tokens inside them are text, not code.
_TEXT = re.compile(rb'"(?:\\.|[^"\\])*"|;[^\n]*|\\.')
_INVALID_NAME = re.compile(r"[\s()\[\]{}\"\\;@^`~,/]|^[:#'0-9]")
_FACTORIES = ("->", "map->")


def _scan(name: str, files) -> dict[str, tuple[bytes, dict]]:
    """Parse the Clojure files whose text contains ``name``."""
    needle = name.encode("utf-8")
    out: dict[str, tuple[bytes, dict]] = {}
    for f in files:
        path = Path(f)
        if path.suffix.lower() not in CLOJURE_SUFFIXES or not path.is_file():
            continue
        data = path.read_bytes()
        if needle not in data:
            continue
        info = extract_clojure(path, positions=True).get("clojure")
        if info:
            out[str(path)] = (data, info)
    return out


def _binder(parsed: dict):
    """Return ``var_of(occurrence) -> (ns, var name) | None`` over the parsed files."""
    by_ns: dict[str, list[dict]] = {}
    for _, info in parsed.values():
        if info.get("ns"):
            by_ns.setdefault(info["ns"], []).append(info.get("vars", {}))

    def var_of(occ: dict) -> tuple[str, str] | None:
        for ns in occ.get("ns") or ():
            for table in by_ns.get(ns, ()):
                var = table.get(occ.get("name"))
                if var:
                    return ns, var
        return None

    return var_of


def _sites(ns: str, name: str, parsed: dict) -> list[tuple[str, dict]]:
    var_of = _binder(parsed)
    found = []
    for path, (_, info) in parsed.items():
        for occ in info.get("occurrences", ()):
            if var_of(occ) == (ns, name):
                found.append((path, occ))
    found.sort(key=lambda s: (s[0], s[1]["start"]))
    return found


def references(ns: str, name: str, files) -> list[Occurrence]:
    """Every site in ``files`` that resolves to the var ``ns/name``."""
    parsed = _scan(name, files)
    return [
        Occurrence(path, o["line"], o["col"], o["start"], o["end"], o["role"], o["text"], o["qualified"])
        for path, o in _sites(ns, name, parsed)
    ]


def _replacement(text: str, old: str, new: str) -> str | None:
    if text == old:
        return new
    for prefix in _FACTORIES:
        if text == prefix + old:
            return prefix + new
    return None  # a :rename alias keeps its local name


def _mentions(path: str, data: bytes, old: str, covered: set[int]) -> list[Mention]:
    """Tokens that spell ``old`` outside the edits: code symbols, keywords, strings and quoted data."""
    text = [(m.start(), m.end()) for m in _TEXT.finditer(data)]
    out = []
    for m in _TOKEN.finditer(data):
        token = m.group().decode("utf-8", "replace")
        bare = token.rsplit("/", 1)[-1].lstrip(":#'")
        if bare != old or any(m.start() <= c < m.end() for c in covered):
            continue
        line = data.count(b"\n", 0, m.start()) + 1
        data_like = token.startswith(":") or any(start <= m.start() < end for start, end in text)
        out.append(Mention(path, line, token, "text" if data_like else "symbol"))
    return out


def plan_rename(ns: str, old: str, new: str, files) -> RenamePlan:
    """Plan the rename of ``ns/old`` to ``ns/new`` over ``files``, without writes.

    A conflict makes the plan unsafe: an invalid name, a var that already has
    the new name, or a file where the new unqualified name would bind to a
    local, to another var or to ``clojure.core``.
    """
    conflicts: list[str] = []
    if not new or _INVALID_NAME.search(new) or new == old:
        conflicts.append(f"`{new}` is not a valid new name for `{old}`.")
    parsed = _scan(old, files)
    edits: list[Edit] = []
    unqualified: dict[str, list[dict]] = {}
    for path, occ in _sites(ns, old, parsed):
        text = _replacement(occ["text"], old, new)
        if text is None:
            continue
        edits.append(Edit(path, occ["line"], occ["col"], occ["start"], occ["end"], occ["text"], text))
        if not occ["qualified"]:
            unqualified.setdefault(path, []).append(occ)
    if not edits:
        conflicts.append(f"No sites of `{ns}/{old}` found.")
    for path, (_, info) in sorted(parsed.items()):
        if info.get("ns") == ns and new in info.get("vars", {}):
            conflicts.append(f"`{ns}` already defines `{new}` in {path}.")
    var_of = _binder(parsed)
    for path, occs in sorted(unqualified.items()):
        info = parsed[path][1]
        for occ in occs:
            if new in occ.get("shadow", ()):
                conflicts.append(f"A local `{new}` at {path}:{occ['line']} would capture the renamed var.")
        taken = new in info.get("unresolved", ()) or any(
            o["text"] == new and not o["qualified"] and var_of(o) != (ns, old)
            for o in info.get("occurrences", ())
        )
        if taken or (info.get("ns") != ns and new in info.get("vars", {})):
            conflicts.append(f"{path} already uses `{new}` for another var.")
    mentions: list[Mention] = []
    for path, (data, _) in sorted(parsed.items()):
        covered = {e.start for e in edits if e.file == path}
        mentions.extend(_mentions(path, data, old, covered))
    digests = {path: digest(parsed[path][0]) for path in {e.file for e in edits}}
    return RenamePlan(f"{ns}/{old}", old, new, tuple(edits), tuple(conflicts), tuple(mentions), digests)
