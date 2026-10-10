(ns orders.queries)

(def OrderSummary
  [:map
   [:id :uuid]
   [:status [:enum :placed :confirmed :cancelled :shipped :rejected]]
   [:total :int]])

(defn order-summary [_deps query]
  {:id (:id query)})

(defn orders-by-customer [_deps query]
  [(:customer-id query)])
