# edd_shop test corpus

A multi-service edd-core shop for the domain extraction tests
(`tests/test_domain_*.py`). It is never run; graphify only reads it.

Happy path: storefront `place-order` → orders `:order-placed` → inventory
`:reserve-stock` → payments `:authorize-payment` → orders `:confirm-order` →
shipping `:create-shipment` → notifications `:send-email`.

Deliberate defects, one for each check:

| Defect | Where | Expected finding |
|---|---|---|
| `:release-stok` typo | orders effect on `:order-cancelled` | `missing-command` |
| `:get-credit-limit` is not registered | payments `:deps` | `missing-query` |
| `:search-products` is not registered | storefront code | `missing-query` |
| `:billing-svc` is not in the corpus | shipping effect | `unknown-service` |
| `:stock-adjusted` has no `reg-event` | inventory | `event-not-applied` |
| `:email-bounced` is never emitted | notifications | `event-never-emitted` |
| `:update-address` registered twice | customers | `duplicate-command` |
| `archive-order-handler` is not defined | orders | `unresolved-handler` |
| `:cmd-id` read from event data | shipping `next-step` | `dynamic-reference` |
| `book-inpost` and `book-dhl` computed from a `case` | shipping `carrier-command` | resolved; `unknown-service` for `carriers-gateway` |
| ids in `def` vars and `let` bindings | orders `ids`, inventory, notifications, storefront | resolved like literals |
| `:order-rejected` starts `:retry-placement`, which can emit `:order-rejected` again | orders | cycle |
