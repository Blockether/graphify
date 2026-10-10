"""End-to-end tests for the edd-core domain plugin on tests/fixtures/edd_shop.

The fixture is an eight-service shop with deliberate defects (see its
README). The tests check the extracted flow, the resolved names, the checks,
the JSON Schemas, the report, the ``graphify domain`` command and the merge
into ``graph.json`` by ``graphify update``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_clojure")

from graphify.domain import analyze, write_outputs
from graphify.domain.model import Ref
from graphify.plugins.edd_core import EddCorePlugin

FIXTURE = Path(__file__).parent / "fixtures" / "edd_shop"
SERVICES = ["catalog-svc", "customers-svc", "inventory-svc", "notifications-svc", "orders-svc",
            "payments-svc", "shipping-svc"]


@pytest.fixture(scope="module")
def model():
    res = analyze(FIXTURE)
    assert [p.name for p in res.plugins] == ["edd-core"]
    return res.model


def _rel(model, source: Ref, relation: str, target: Ref):
    hits = [r for r in model.relations if (r.source, r.relation, r.target) == (source, relation, target)]
    assert hits, f"missing relation {source} {relation} {target}"
    return hits[0]


def _finding(model, code: str, text: str):
    hits = [f for f in model.findings if f.code == code and text in f.message]
    assert hits, f"missing finding {code} containing {text!r}: {[f.message for f in model.findings]}"
    return hits[0]


def cmd(service, name):
    return Ref("command", service, name)


def evt(service, name):
    return Ref("event", service, name)


def qry(service, name):
    return Ref("query", service, name)


def test_services_and_registrations(model):
    services = {s for s, info in model.services.items() if info.get("role") != "client"}
    assert sorted(services) == SERVICES
    assert model.services["storefront.api"]["role"] == "client"
    assert {e.name for e in model.of_kind("command", "orders-svc")} == {
        "place-order", "confirm-order", "cancel-order", "mark-shipped", "retry-placement", "archive-order"}
    assert {e.name for e in model.of_kind("query", "orders-svc")} == {
        "get-by-id", "get-order-summary", "list-customer-orders"}
    place = model.elements[cmd("orders-svc", "place-order")]
    assert place.handler == "orders.commands/place-order"
    assert place.attrs["auth"] == ["customer"]
    assert place.attrs["deps"] == ["product"]
    assert place.attrs["handler_file"] == "orders-svc/src/orders/commands.clj"
    assert model.elements[cmd("orders-svc", "cancel-order")].attrs["auth"] == ["customer", "support"]
    assert model.elements[evt("orders-svc", "order-placed")].attrs == {
        "apply": "orders.events/order-placed", "fx": "orders.fx/on-order-placed"}
    assert model.elements[Ref("effect", "payments-svc", "audit-fx")].handler == "payments.main/audit-fx"


def test_happy_path_flow_across_services(model):
    _rel(model, Ref("function", "storefront.api", "place-order!"), "invokes", cmd("orders-svc", "place-order"))
    _rel(model, cmd("orders-svc", "place-order"), "reads", qry("catalog-svc", "get-product"))
    _rel(model, cmd("orders-svc", "place-order"), "emits", evt("orders-svc", "order-placed"))
    _rel(model, cmd("orders-svc", "place-order"), "emits", evt("orders-svc", "order-rejected"))
    _rel(model, evt("orders-svc", "order-placed"), "triggers", cmd("inventory-svc", "reserve-stock"))
    _rel(model, cmd("inventory-svc", "reserve-stock"), "reads", qry("inventory-svc", "get-stock-levels"))
    _rel(model, evt("inventory-svc", "stock-reserved"), "triggers", cmd("payments-svc", "authorize-payment"))
    _rel(model, cmd("payments-svc", "authorize-payment"), "reads", qry("customers-svc", "get-customer"))
    _rel(model, evt("payments-svc", "payment-authorized"), "triggers", cmd("orders-svc", "confirm-order"))
    _rel(model, evt("orders-svc", "order-confirmed"), "triggers", cmd("shipping-svc", "create-shipment"))
    _rel(model, evt("orders-svc", "order-confirmed"), "triggers", cmd("notifications-svc", "send-email"))
    _rel(model, evt("shipping-svc", "shipment-dispatched"), "triggers", cmd("orders-svc", "mark-shipped"))
    _rel(model, Ref("effect", "payments-svc", "audit-fx"), "triggers", cmd("notifications-svc", "log-audit"))
    flow = model.flow(Ref("function", "storefront.api", "place-order!"))
    reached = {r.target for r in flow}
    assert {cmd("shipping-svc", "create-shipment"), evt("notifications-svc", "email-sent")} <= reached


def test_local_effects_stay_in_their_service(model):
    _rel(model, evt("orders-svc", "order-rejected"), "triggers", cmd("orders-svc", "retry-placement"))
    _rel(model, evt("notifications-svc", "email-bounced"), "triggers", cmd("notifications-svc", "send-email"))


def test_computed_names_resolve_to_constants(model):
    # Registration ids in a var of another namespace and in a let binding.
    assert qry("orders-svc", "get-order-summary") in model.elements
    assert cmd("inventory-svc", "release-stock") in model.elements
    assert cmd("inventory-svc", "adjust-stock") in model.elements
    # :service and :cmd-id in vars.
    r = _rel(model, Ref("function", "storefront.api", "place-order!"), "invokes", cmd("orders-svc", "place-order"))
    assert r.attrs["resolved_from"] == "place-order-cmd"
    # A let binding inside an effect.
    _rel(model, evt("notifications-svc", "email-bounced"), "triggers", cmd("notifications-svc", "send-email"))
    # A parameter bound at the call site, built by (keyword (str ...)) from a case.
    for carrier in ("book-inpost", "book-dhl"):
        r = _rel(model, evt("shipping-svc", "shipment-created"), "triggers", cmd("carriers-gateway", carrier))
        assert r.attrs["resolved_from"] == '(keyword (str "book-" (name carrier)))'
    assert not [f for f in model.findings if f.code == "dynamic-registration"]


def test_deliberate_defects_are_reported(model):
    f = _finding(model, "missing-command", "`release-stok`")
    assert (f.severity, f.file) == ("error", "orders-svc/src/orders/fx.clj")
    f = _finding(model, "missing-query", "`get-credit-limit`")
    assert f.file == "payments-svc/src/payments/main.clj"
    f = _finding(model, "missing-query", "`search-products`")
    assert f.file == "storefront/src/storefront/api.clj"
    assert _finding(model, "unknown-service", "`billing-svc`").severity == "warning"
    assert _finding(model, "unknown-service", "`carriers-gateway`").severity == "warning"
    assert _finding(model, "event-not-applied", "stock-adjusted").severity == "warning"
    assert _finding(model, "event-never-emitted", "email-bounced").severity == "warning"
    assert _finding(model, "duplicate-command", "update-address").severity == "error"
    assert _finding(model, "unresolved-handler", "archive-order-handler").severity == "error"
    f = _finding(model, "dynamic-reference", ":next-step")
    assert (f.severity, f.file) == ("info", "shipping-svc/src/shipping/main.clj")
    errors = sorted(f.code for f in model.findings if f.severity == "error")
    assert errors == ["duplicate-command", "missing-command", "missing-query", "missing-query", "payload-invalid",
                      "payload-invalid", "schema-invalid-bounds", "schema-invalid-default", "schema-invalid-regex",
                      "schema-unknown-type", "schema-unresolved", "unresolved-handler"]


@pytest.mark.parametrize("code, ref, file, line, text", [
    ("schema-unknown-type", "catalog-svc/query/get-product", "catalog-svc/src/catalog/main.clj", 22, "`:stirng`"),
    ("schema-invalid-bounds", "customers-svc/command/register-customer", "customers-svc/src/customers/main.clj",
     29, "`:min 3` greater than `:max 1`"),
    ("schema-invalid-regex", "customers-svc/command/register-customer", "customers-svc/src/customers/main.clj",
     31, "unterminated character set"),
    ("schema-invalid-default", "notifications-svc/command/send-email", "notifications-svc/src/notifications/main.clj",
     22, 'is string "high", expected integer'),
    ("schema-unresolved", "shipping-svc/command/dispatch-shipment", "shipping-svc/src/shipping/main.clj", 52,
     "`s/TrackingNumber` is not defined"),
    ("payload-invalid", "notifications-svc/command/send-email", "shipping-svc/src/shipping/main.clj", 29,
     'attrs.template: is string "shipment-created", expected keyword'),
    ("payload-invalid", "orders-svc/command/place-order", "storefront/src/storefront/api.clj", 21,
     "attrs.shipping-address: required key is missing; attrs.lines: has 0 items, expected at least 1"),
])
def test_schema_and_payload_defects_are_reported(model, code, ref, file, line, text):
    hits = [f for f in model.findings if f.code == code and str(f.ref) == ref and text in f.message]
    assert len(hits) == 1, [f.message for f in model.findings if f.code == code]
    assert (hits[0].severity, hits[0].file, hits[0].line) == ("error", file, line)


def test_valid_payloads_and_schemas_have_no_findings(model):
    # inventory-svc sends a complete payment payload; place-order! in storefront is valid.
    flagged = {f.file for f in model.findings if f.code == "payload-invalid"}
    assert flagged == {"shipping-svc/src/shipping/main.clj", "storefront/src/storefront/api.clj"}
    schema_refs = {str(f.ref) for f in model.findings if f.code.startswith("schema-") and f.severity == "error"}
    assert "orders-svc/command/place-order" not in schema_refs


def test_test_files_are_excluded_unless_asked(model):
    assert all("main_test" not in (e.file or "") for e in model.elements.values())
    res = analyze(FIXTURE, plugins=[EddCorePlugin(include_tests=True)])
    _finding(res.model, "missing-command", "`not-a-real-command`")


def test_schemas_convert_with_edd_envelope(model):
    place = model.elements[cmd("orders-svc", "place-order")].schemas["consumes"]
    assert place["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert place["properties"]["cmd-id"] == {"const": "place-order"}
    assert place["properties"]["id"] == {"type": "string", "format": "uuid"}
    attrs = place["properties"]["attrs"]
    assert attrs["properties"]["lines"] == {"type": "array", "items": {"$ref": "#/$defs/shop.schemas.LineItem"},
                                            "minItems": 1}
    assert set(place["$defs"]) == {"shop.schemas.Address", "shop.schemas.LineItem", "shop.schemas.Money",
                                   "shop.schemas.Currency"}
    assert place["$defs"]["shop.schemas.Money"]["additionalProperties"] is False
    assert "coupon" not in attrs["required"]
    summary = model.elements[qry("orders-svc", "get-order-summary")].schemas
    assert summary["consumes"]["properties"]["query-id"] == {"const": "get-order-summary"}
    assert summary["produces"]["properties"]["result"] == {"$ref": "#/$defs/orders.queries.OrderSummary"}
    product = model.elements[qry("catalog-svc", "get-product")].schemas["produces"]
    assert product["properties"]["result"]["anyOf"][1] == {"type": "null"}
    assert model.services["customers-svc"]["schema"]["properties"]["tier"] == {"enum": ["basic", "gold"]}
    source = model.elements[cmd("orders-svc", "place-order")].sources["consumes"]
    assert source.startswith(";; orders.commands/PlaceOrder (orders-svc/src/orders/commands.clj:")


def test_report_and_files(model, tmp_path):
    written = write_outputs(model, tmp_path)
    md = written["markdown"].read_text(encoding="utf-8")
    for heading in ("## Findings", "## System overview", "## Services", "## Flows", "## Schemas",
                    "### Service orders-svc", "### storefront.api: code caller place-order!", "### Cycles"):
        assert heading in md, heading
    assert md.count("```mermaid") >= 15
    assert "sequenceDiagram" in md and "flowchart LR" in md
    assert "classDef missing" in md and "authorize-payment reads query get-credit-limit (MISSING)" in md
    assert "`orders-svc/command/retry-placement` → `orders-svc/event/order-rejected`" in md
    schema_file = tmp_path / "schemas/orders-svc/command.place-order.consumes.json"
    assert json.loads(schema_file.read_text())["title"] == "place-order consumes"
    data = json.loads(written["json"].read_text())
    assert data["plugins"] == ["edd-core"]
    assert {"source", "target", "relation", "required"} <= set(data["relations"][0])
    # A second run replaces the schema files instead of keeping stale ones.
    stale = tmp_path / "schemas/old/stale.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    write_outputs(model, tmp_path)
    assert not stale.exists()


def _graphify(*args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, "-m", "graphify", *args], capture_output=True, text=True,
                          cwd=cwd, env=full_env, timeout=300)


def test_domain_cli_json_and_strict(tmp_path):
    r = _graphify("domain", str(FIXTURE), "--out", str(tmp_path / "out"), "--json", "--strict")
    assert r.returncode == 1, r.stderr  # the fixture has errors
    data = json.loads(r.stdout)
    assert data["plugins"] == ["edd-core"]
    assert data["finding_counts"]["error"] == 12
    assert Path(data["markdown"]).is_file()
    r = _graphify("domain", str(FIXTURE), "--out", str(tmp_path / "out2"))
    assert r.returncode == 0, r.stderr
    assert "edd-core: 8 services" in r.stdout and "missing-command" in r.stdout
    r = _graphify("domain", str(FIXTURE), "--list-plugins")
    assert "edd-core" in r.stdout
    r = _graphify("domain", str(FIXTURE), "--plugin", "no-such-plugin")
    assert r.returncode == 2 and "cannot import" in r.stderr


def test_update_merges_domain_nodes_into_graph(tmp_path):
    project = tmp_path / "shop"
    shutil.copytree(FIXTURE, project)
    r = _graphify("update", str(project), "--no-cluster")
    assert r.returncode == 0, r.stderr
    assert "[graphify domain] edd-core" in r.stderr

    def load():
        graph = json.loads((project / "graphify-out" / "graph.json").read_text())
        return graph["nodes"], graph.get("links", graph.get("edges", []))

    nodes, edges = load()
    by_id = {n["id"]: n for n in nodes}
    place = by_id["domain:command:orders-svc:place-order"]
    assert place["file_type"] == "concept" and place["_origin"] == "domain"
    assert place["source_file"] == "orders-svc/src/orders/main.clj"
    handled = [e for e in edges if e["source"] == place["id"] and e["relation"] == "handled_by"]
    assert handled and by_id[handled[0]["target"]]["label"] == "place-order()"
    missing = by_id["domain:command:inventory-svc:release-stok"]
    assert missing["domain_missing"] is True
    assert any(e["relation"] == "triggers" and e["target"] == missing["id"] for e in edges)
    assert (project / "graphify-out" / "domain" / "DOMAIN.md").is_file()
    count = sum(1 for n in nodes if n.get("_origin") == "domain")

    r = _graphify("update", str(project), "--no-cluster")
    assert r.returncode == 0, r.stderr
    nodes, _ = load()
    assert sum(1 for n in nodes if n.get("_origin") == "domain") == count

    r = _graphify("update", str(project), "--no-cluster", "--force", env={"GRAPHIFY_DOMAIN": "0"})
    assert r.returncode == 0, r.stderr
    nodes, _ = load()
    assert not [n for n in nodes if n.get("_origin") == "domain"]
