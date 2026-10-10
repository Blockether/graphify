(ns orders.events)

(defn order-placed [agg {:keys [attrs]}]
  (merge agg attrs {:status :placed}))

(defn order-confirmed [agg _event]
  (assoc agg :status :confirmed))

(defn order-cancelled [agg {:keys [attrs]}]
  (assoc agg :status :cancelled :reason (:reason attrs)))

(defn order-shipped [agg _event]
  (assoc agg :status :shipped))

(defn order-rejected [agg _event]
  (assoc agg :status :rejected))
