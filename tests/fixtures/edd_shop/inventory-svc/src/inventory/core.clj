(ns inventory.core
  (:require [edd.core :as edd]
            [edd.common :as common]))

(def ReserveStock
  [:map
   [:attrs [:map
            [:order-id :uuid]
            [:lines [:vector [:map [:sku :string] [:quantity pos-int?]]]]]]])

(def adjust-stock-id :adjust-stock)

(defn reserve-stock
  [{:keys [stock]} {:keys [id attrs]}]
  (if (every? pos? (vals stock))
    {:event-id :stock-reserved :id id :attrs attrs}
    {:event-id :stock-shortage :id id :attrs attrs}))

(defn release-stock
  [_deps {:keys [id]}]
  {:event-id :stock-released :id id})

(defn adjust-stock
  [_deps {:keys [id attrs]}]
  {:event-id :stock-adjusted :id id :attrs attrs})

(defn stock-dep
  [_deps cmd]
  {:query-id :get-stock-levels
   :skus (map :sku (get-in cmd [:attrs :lines]))})

(defn apply-reserved [agg _] (update agg :reserved (fnil inc 0)))
(defn apply-shortage [agg _] agg)
(defn apply-released [agg _] (update agg :reserved dec))

(defn after-reserved
  [_ctx event]
  {:service :payments-svc
   :commands [{:cmd-id :authorize-payment
               :id (get-in event [:attrs :order-id])
               :attrs {:amount (get-in event [:attrs :total])
                       :method :card}}]})

(defn after-shortage
  [_ctx event]
  {:service :orders-svc
   :commands [{:cmd-id :cancel-order
               :id (get-in event [:attrs :order-id])
               :attrs {:reason :stock}}]})

(defn stock-levels [_deps query]
  (zipmap (:skus query) (repeat 0)))

(defn init
  [ctx]
  (let [ctx (assoc ctx :service-name :inventory-svc)
        release :release-stock]
    (edd/reg-cmd ctx :reserve-stock reserve-stock
                 :consumes ReserveStock
                 :deps [:stock stock-dep])
    (edd/reg-cmd ctx release release-stock)
    (edd/reg-cmd ctx adjust-stock-id adjust-stock
                 :consumes [:map [:attrs [:map [:sku :string] [:delta :int]]]]
                 :auth {:role :warehouse})
    (edd/reg-event ctx :stock-reserved apply-reserved)
    (edd/reg-event ctx :stock-shortage apply-shortage)
    (edd/reg-event ctx :stock-released apply-released)
    (edd/reg-event-fx ctx :stock-reserved after-reserved)
    (edd/reg-event-fx ctx :stock-shortage after-shortage)
    (edd/reg-query ctx :get-by-id common/get-by-id)
    (edd/reg-query ctx :get-stock-levels stock-levels
                   :consumes [:map [:skus [:sequential :string]]]
                   :produces [:map-of :string :int])))
