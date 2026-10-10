(ns orders.commands
  (:require [shop.schemas :as s]))

(def PlaceOrder
  [:map
   [:attrs
    [:map
     [:customer-id :uuid]
     [:lines [:vector {:min 1} s/LineItem]]
     [:shipping-address s/Address]
     [:coupon {:optional true} [:maybe :string]]]]])

(def CancelOrder
  [:map [:attrs [:map [:reason [:enum :customer :payment :stock]]]]])

(defn- order-total [lines]
  (reduce + (map #(* (:quantity %) (get-in % [:unit-price :amount])) lines)))

(defn place-order
  [{:keys [product]} {:keys [id attrs]}]
  (if product
    {:event-id :order-placed
     :id id
     :attrs (assoc attrs :total (order-total (:lines attrs)))}
    {:event-id :order-rejected
     :id id
     :attrs {:reason :unknown-product}}))

(defn confirm-order
  [_deps {:keys [id]}]
  {:event-id :order-confirmed
   :id id})

(defn cancel-order
  [_deps {:keys [id attrs]}]
  [{:identity id}
   {:event-id :order-cancelled
    :id id
    :attrs attrs}])

(defn mark-shipped
  [_deps {:keys [id attrs]}]
  {:event-id :order-shipped
   :id id
   :attrs attrs})

(defn retry-placement
  [{:keys [product]} {:keys [id]}]
  (if product
    {:event-id :order-placed :id id :attrs {:retry true}}
    {:event-id :order-rejected :id id :attrs {:reason :still-unknown}}))

(defn product-dep
  [_deps cmd]
  {:service :catalog-svc
   :query {:query-id :get-product
           :sku (get-in cmd [:attrs :lines 0 :sku])}})
