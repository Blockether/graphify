(ns orders.fx)

(defn- reserve-command [event]
  {:cmd-id :reserve-stock
   :id (:id event)
   :attrs {:order-id (:id event)
           :lines (get-in event [:attrs :lines])}})

(defn on-order-placed
  [_ctx event]
  {:service :inventory-svc
   :commands [(reserve-command event)]})

(defn on-order-cancelled
  [_ctx event]
  ;; Deliberate defect: typo in the command id.
  {:service :inventory-svc
   :commands [{:cmd-id :release-stok
               :id (:id event)}]})

(defn on-order-rejected
  [_ctx event]
  {:commands [{:cmd-id :retry-placement
               :id (:id event)}]})

(defn on-order-confirmed
  [_ctx event]
  [{:service :shipping-svc
    :commands [{:cmd-id :create-shipment
                :id (:id event)
                :attrs {:order-id (:id event)}}]}
   {:service :notifications-svc
    :commands [{:cmd-id :send-email
                :id (:id event)
                :attrs {:template :order-confirmed}}]}])
