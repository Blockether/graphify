"""Tests for the Malli to JSON Schema converter (graphify/domain/malli.py)."""
from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_clojure")

from graphify.domain import edn
from graphify.domain.malli import JSON_SCHEMA_DIALECT, MalliConverter, merge_objects


def _convert(text: str, defs: str = "", title: str | None = None):
    forms = edn.read_string(defs) if defs else []
    table = {str(f.items[1].value): f.items[2] for f in forms}

    def resolve(sym, ctx):
        name = str(sym.value)
        return (name, table[name], ctx) if name in table else None

    conv = MalliConverter(resolve)
    (form,) = edn.read_string(text)
    return conv.document(form, None, title), conv


@pytest.mark.parametrize("malli, expected", [
    (":string", {"type": "string"}),
    ("[:string {:min 1 :max 3}]", {"type": "string", "minLength": 1, "maxLength": 3}),
    ("[:int {:min 0}]", {"type": "integer", "minimum": 0}),
    (":uuid", {"type": "string", "format": "uuid"}),
    ("pos-int?", {"type": "integer", "minimum": 1}),
    ("[:enum :a \"b\"]", {"enum": ["a", "b"]}),
    ("[:= :x]", {"const": "x"}),
    ("[:maybe :int]", {"anyOf": [{"type": "integer"}, {"type": "null"}]}),
    ("[:vector {:min 1} :int]", {"type": "array", "items": {"type": "integer"}, "minItems": 1}),
    ("[:set :keyword]", {"type": "array", "items": {"type": "string", "x-malli": "keyword"}, "uniqueItems": True}),
    ("[:tuple :int :string]", {"type": "array", "prefixItems": [{"type": "integer"}, {"type": "string"}],
                               "items": False, "minItems": 2, "maxItems": 2}),
    ("[:re #\"^a+$\"]", {"type": "string", "pattern": "^a+$"}),
    ("[:or :int :string]", {"anyOf": [{"type": "integer"}, {"type": "string"}]}),
    ("[:> 5]", {"type": "number", "exclusiveMinimum": 5}),
])
def test_simple_schemas(malli, expected):
    doc, _ = _convert(malli)
    assert doc.pop("$schema") == JSON_SCHEMA_DIALECT
    assert doc == expected


def test_map_entries_optional_closed_and_descriptions():
    doc, _ = _convert('[:map {:closed true} [:a :int] [:b {:optional true :description "B"} :string] :ns/c]')
    assert doc["type"] == "object"
    assert doc["required"] == ["a", "ns/c"]
    assert doc["additionalProperties"] is False
    assert doc["properties"]["b"] == {"type": "string", "description": "B"}
    assert "$comment" in doc["properties"]["ns/c"]


def test_symbols_become_defs_and_nested_refs_follow():
    doc, conv = _convert("[:map [:total Money] [:items [:vector Item]]]",
                         defs="(def Money [:map [:amount :int]]) (def Item [:map [:price Money]])", title="Order")
    assert doc["title"] == "Order"
    assert doc["properties"]["total"] == {"$ref": "#/$defs/Money"}
    assert doc["properties"]["items"]["items"] == {"$ref": "#/$defs/Item"}
    assert set(doc["$defs"]) == {"Money", "Item"}
    assert doc["$defs"]["Item"]["properties"]["price"] == {"$ref": "#/$defs/Money"}
    assert conv.issues == []


def test_recursive_schema_terminates():
    doc, _ = _convert("Node", defs="(def Node [:map [:children [:vector Node]]])")
    assert doc["$ref"] == "#/$defs/Node"
    assert doc["$defs"]["Node"]["properties"]["children"]["items"] == {"$ref": "#/$defs/Node"}


def test_merge_multi_and_map_of():
    doc, _ = _convert("[:merge A [:map [:b :string]]]", defs="(def A [:map [:a :int]])")
    assert doc["properties"] == {"a": {"type": "integer"}, "b": {"type": "string"}}
    assert doc["required"] == ["a", "b"]
    doc, _ = _convert("[:multi {:dispatch :type} [:x [:map [:x :int]]] [:y [:map [:y :int]]]]")
    assert [b["properties"]["type"]["const"] for b in doc["oneOf"]] == ["x", "y"]
    doc, _ = _convert("[:map-of :keyword :int]")
    assert doc["additionalProperties"] == {"type": "integer"}


def test_unknown_and_opaque_schemas_are_reported():
    doc, conv = _convert("[:map [:a Missing] [:b [:fn pos?]] [:c :no-such-type]]")
    assert all("$comment" in doc["properties"][k] for k in "abc")
    assert [(i.severity, i.code) for i in conv.issues] == [
        ("warning", "schema-unresolved"), ("info", "schema-approximation"), ("error", "schema-unknown-type")]


def test_merge_objects_falls_back_to_all_of():
    merged = merge_objects({"type": "object", "properties": {"a": {}}, "required": ["a"]}, {"anyOf": []})
    assert merged == {"allOf": [{"type": "object", "properties": {"a": {}}, "required": ["a"]}, {"anyOf": []}]}
