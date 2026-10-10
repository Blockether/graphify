(ns storefront.api
  "Storefront backend: calls the shop services over HTTP."
  (:require [edd.client.core :as client]))

(def orders-service :orders-svc)

(def place-order-cmd :place-order)

(defn place-order! [ctx customer-id lines address]
  (client/call-service ctx {:service orders-service
                            :commands [{:cmd-id place-order-cmd
                                        :id (random-uuid)
                                        :attrs {:customer-id customer-id
                                                :lines lines
                                                :shipping-address address}}]}))

(defn browse [ctx]
  (client/query-service ctx {:service :catalog-svc
                             :query {:query-id :list-products}}))

(defn search [ctx text]
  ;; Deliberate defect: catalog-svc has no :search-products query.
  (client/query-service ctx {:service :catalog-svc
                             :query {:query-id :search-products :text text}}))

(defn my-orders [ctx customer-id]
  (client/query-service ctx {:service :orders-svc
                             :query {:query-id :list-customer-orders
                                     :customer-id customer-id}}))

(defn sign-up [ctx attrs]
  (client/call-service ctx {:service :customers-svc
                            :commands [{:cmd-id :register-customer :id (random-uuid) :attrs attrs}]}))
