"""Check static values against the JSON Schemas that graphify generates.

Graphify reads source code, so it knows only part of a value: literals, and
constants that :class:`~graphify.domain.evaluate.Evaluator` can compute. Every
other part is :data:`UNKNOWN`, and an unknown part always passes. A finding
therefore always comes from a value that is in the code.

The checker supports the keywords that :mod:`graphify.domain.malli` writes. It
does not check ``format`` other than ``uuid``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class _Unknown:
    """A value that the source code does not give."""

    _instance: _Unknown | None = None

    def __new__(cls) -> _Unknown:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = _Unknown()


class Keyword(str):
    """A Clojure keyword. It compares equal to its name, but a string schema rejects it."""

    def __repr__(self) -> str:
        return ":" + str.__str__(self)


@dataclass(frozen=True)
class Problem:
    """One reason why a value does not match its schema."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path or '(root)'}: {self.message}"


def describe(value: Any) -> str:
    """Show ``value`` as Clojure code, for messages."""
    if isinstance(value, Keyword):
        return repr(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "nil"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        return "[" + " ".join(describe(v) for v in value[:5]) + (" ..." if len(value) > 5 else "") + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f":{k} {describe(v)}" for k, v in list(value.items())[:4]) + (" ..." if len(value) > 4 else "") + "}"
    return str(value)


def _type_of(value: Any) -> str:
    if isinstance(value, Keyword):
        return "keyword"
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _matches_type(value: Any, expected: str, schema: dict) -> bool:
    actual = _type_of(value)
    malli = schema.get("x-malli", "")
    if actual == "keyword":
        # Graphify writes keyword schemas as strings with an `x-malli` marker.
        return expected == "string" and "keyword" in malli
    if expected == "string" and "keyword" in malli:
        return False
    if expected == "number":
        return actual in ("integer", "number")
    if expected == "integer":
        return actual == "integer" or (actual == "number" and float(value).is_integer())
    return actual == expected


def _type_name(schema: dict) -> str:
    malli = schema.get("x-malli")
    if malli:
        return malli
    t = schema.get("type")
    return " or ".join(t) if isinstance(t, list) else str(t)


class Checker:
    """Check values against one JSON Schema document and its ``$defs``."""

    def __init__(self, document: dict):
        self.root = document
        self._depth = 0

    def check(self, value: Any, schema: dict | None = None, path: str = "") -> list[Problem]:
        return self._check(value, self.root if schema is None else schema, path)

    def _resolve(self, ref: str) -> dict | None:
        if not ref.startswith("#/$defs/"):
            return None
        return (self.root.get("$defs") or {}).get(ref[len("#/$defs/"):])

    def _check(self, value: Any, schema: Any, path: str) -> list[Problem]:
        if value is UNKNOWN or not isinstance(schema, dict) or not schema:
            return []
        if self._depth > 64:
            return []
        self._depth += 1
        try:
            return self._check_schema(value, schema, path)
        finally:
            self._depth -= 1

    def _check_schema(self, value: Any, schema: dict, path: str) -> list[Problem]:
        problems: list[Problem] = []
        ref = schema.get("$ref")
        if isinstance(ref, str):
            target = self._resolve(ref)
            if target is not None:
                problems += self._check(value, target, path)
        if "const" in schema and not _equal(value, schema["const"]):
            problems.append(Problem(path, f"is {describe(value)}, expected {describe(schema['const'])}"))
        if "enum" in schema and not any(_equal(value, e) for e in schema["enum"]):
            allowed = " ".join(describe(e) for e in schema["enum"])
            problems.append(Problem(path, f"is {describe(value)}, expected one of {allowed}"))
        t = schema.get("type")
        if t is not None:
            types = t if isinstance(t, list) else [t]
            if not any(_matches_type(value, x, schema) for x in types):
                problems.append(Problem(path, f"is {_type_of(value)} {describe(value)}, expected {_type_name(schema)}"))
                return problems
        for key in ("anyOf", "oneOf"):
            if key in schema:
                branches = [self._check(value, s, path) for s in schema[key]]
                if branches and all(branches):
                    best = min(branches, key=len)
                    problems += best if len(branches) == 1 else [
                        Problem(path, f"matches none of {len(branches)} alternatives; closest: {best[0].message}")]
        for sub in schema.get("allOf", []):
            problems += self._check(value, sub, path)
        if "not" in schema and isinstance(schema["not"], dict) and not self._check(value, schema["not"], path):
            if not _contains_unknown(value):
                problems.append(Problem(path, f"{describe(value)} is not allowed here"))
        if isinstance(value, str) and not isinstance(value, Keyword):
            problems += self._string(value, schema, path)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            problems += self._number(value, schema, path)
        if isinstance(value, list):
            problems += self._array(value, schema, path)
        if isinstance(value, dict):
            problems += self._object(value, schema, path)
        return problems

    @staticmethod
    def _string(value: str, schema: dict, path: str) -> list[Problem]:
        out = []
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(Problem(path, f"has {len(value)} characters, expected at least {schema['minLength']}"))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(Problem(path, f"has {len(value)} characters, expected at most {schema['maxLength']}"))
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, value) is None:
                    out.append(Problem(path, f"{describe(value)} does not match #\"{pattern}\""))
            except re.error:
                pass
        if schema.get("format") == "uuid" and not _UUID.match(value):
            out.append(Problem(path, f"{describe(value)} is not a UUID"))
        return out

    @staticmethod
    def _number(value: float, schema: dict, path: str) -> list[Problem]:
        out = []
        checks = (("minimum", lambda b: value >= b, "at least"), ("maximum", lambda b: value <= b, "at most"),
                  ("exclusiveMinimum", lambda b: value > b, "more than"),
                  ("exclusiveMaximum", lambda b: value < b, "less than"))
        for key, ok, words in checks:
            bound = schema.get(key)
            if isinstance(bound, (int, float)) and not isinstance(bound, bool) and not ok(bound):
                out.append(Problem(path, f"is {describe(value)}, expected {words} {bound}"))
        return out

    def _array(self, value: list, schema: dict, path: str) -> list[Problem]:
        out = []
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(Problem(path, f"has {len(value)} items, expected at least {schema['minItems']}"))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(Problem(path, f"has {len(value)} items, expected at most {schema['maxItems']}"))
        prefix = schema.get("prefixItems") or []
        for i, item in enumerate(value):
            sub = prefix[i] if i < len(prefix) else schema.get("items")
            if sub is False:
                out.append(Problem(f"{path}[{i}]", "is an extra item: the tuple is full"))
            elif isinstance(sub, dict):
                out += self._check(item, sub, f"{path}[{i}]")
        return out

    def _object(self, value: dict, schema: dict, path: str) -> list[Problem]:
        out = []
        props = schema.get("properties") or {}
        for key in schema.get("required", []):
            if key not in value:
                out.append(Problem(_join(path, key), "required key is missing"))
        extra = schema.get("additionalProperties")
        for key, item in value.items():
            if key in props:
                out += self._check(item, props[key], _join(path, key))
            elif extra is False:
                out.append(Problem(_join(path, key), "key is not allowed: the map is closed"))
            elif isinstance(extra, dict):
                out += self._check(item, extra, _join(path, key))
        return out


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else str(key)


def _equal(value: Any, expected: Any) -> bool:
    if isinstance(value, bool) or isinstance(expected, bool):
        return value is expected
    return value == expected


def _contains_unknown(value: Any) -> bool:
    if value is UNKNOWN:
        return True
    if isinstance(value, list):
        return any(_contains_unknown(v) for v in value)
    if isinstance(value, dict):
        return any(_contains_unknown(v) for v in value.values())
    return False


def check(document: dict, value: Any) -> list[Problem]:
    """Check ``value`` against ``document``; return the problems, or an empty list."""
    return Checker(document).check(value)
