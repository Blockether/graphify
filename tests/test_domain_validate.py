"""Tests for the JSON Schema value checker (graphify/domain/validate.py) and Malli schema issues."""
from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_clojure")

from graphify.domain import edn
from graphify.domain.malli import MalliConverter, literal
from graphify.domain.validate import UNKNOWN, Checker, Keyword, check


def _schema(text: str, defs: str = "", is_local=None):
    forms = edn.read_string(defs) if defs else []
    table = {str(f.items[1].value): f.items[2] for f in forms}

    def resolve(sym, ctx):
        name = str(sym.value)
        return (name, table[name], ctx) if name in table else None

    conv = MalliConverter(resolve, is_local=is_local)
    (form,) = edn.read_string(text)
    return conv.document(form, None, None), conv


def _problems(malli: str, value_text: str, defs: str = "") -> list[str]:
    doc, _ = _schema(malli, defs)
    (form,) = edn.read_string(value_text)
    return [f"{p.path}: {p.message}" if p.path else p.message for p in check(doc, literal(form))]


@pytest.mark.parametrize("malli, value", [
    ("[:map [:a :int] [:b {:optional true} :string]]", "{:a 1}"),
    ("[:map [:k :keyword]]", "{:k :card}"),
    ("[:enum :card :cash]", ":cash"),
    ("[:vector {:min 1} [:map [:n pos-int?]]]", "[{:n 2}]"),
    (":uuid", '#uuid "6f1c2a52-6d1e-4a7a-9c43-7a9c1c2d3e4f"'),
    ("[:maybe :string]", "nil"),
    ("[:or :int :string]", '"x"'),
    ("[:tuple :int :string]", '[1 "a"]'),
    ("[:map [:a :int]]", "{:a (compute)}"),
    ("Money", "{:amount 3}"),
])
def test_valid_values_pass(malli, value):
    assert _problems(malli, value, defs="(def Money [:map [:amount :int]])") == []


@pytest.mark.parametrize("malli, value, expected", [
    ("[:map [:a :int]]", "{}", "a: required key is missing"),
    ("[:map [:k :keyword]]", '{:k "card"}', 'k: is string "card", expected keyword'),
    ("[:map {:closed true} [:a :int]]", "{:a 1 :b 2}", "b"),
    ("[:enum :card :cash]", ":wire", ":wire"),
    ("[:vector {:min 1} :int]", "[]", "has 0 items, expected at least 1"),
    ("[:string {:min 3}]", '"ab"', "has 2 characters, expected at least 3"),
    ("[:int {:min 0}]", "-1", "-1"),
    (":uuid", '"nope"', "is not a UUID"),
    ("Money", "{:amount :x}", "amount"),
])
def test_invalid_values_are_reported(malli, value, expected):
    problems = _problems(malli, value, defs="(def Money [:map [:amount :int]])")
    assert problems and expected in "; ".join(problems), problems


def test_unknown_values_always_pass():
    checker = Checker({"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}})
    assert checker.check(UNKNOWN) == []
    assert checker.check({"a": UNKNOWN}) == []


def test_keyword_is_a_string_only_for_keyword_schemas():
    assert Keyword("card") == "card"
    assert check({"type": "string"}, Keyword("card")) != []


@pytest.mark.parametrize("malli, code", [
    ("[:string {:min 3 :max 1}]", "schema-invalid-bounds"),
    ("[:vector {:min -1} :int]", "schema-invalid-bounds"),
    ('[:int {:default "x"}]', "schema-invalid-default"),
    ('[:re "^[0-9+"]', "schema-invalid-regex"),
    ("[:map [:a :int] [:a :string]]", "schema-duplicate-key"),
    (":stirng", "schema-unknown-type"),
    ("[:vector]", "schema-arity"),
])
def test_schema_errors(malli, code):
    _, conv = _schema(malli)
    assert [(i.severity, i.code) for i in conv.issues] == [("error", code)]


@pytest.mark.parametrize("malli", [
    '[:re "^\\\\p{L}+$"]',
    "[:string {:min 1 :max 1}]",
    "[:int {:default 3}]",
    "[:map [:a :int] [:b :int]]",
])
def test_valid_schemas_have_no_issues(malli):
    _, conv = _schema(malli)
    assert conv.issues == []


def test_unresolved_symbol_severity_depends_on_ownership():
    _, conv = _schema("Missing", is_local=lambda sym, ctx: True)
    assert [(i.severity, i.code) for i in conv.issues] == [("error", "schema-unresolved")]
    _, conv = _schema("lib/Missing", is_local=lambda sym, ctx: False)
    assert [(i.severity, i.code) for i in conv.issues] == [("warning", "schema-unresolved")]
