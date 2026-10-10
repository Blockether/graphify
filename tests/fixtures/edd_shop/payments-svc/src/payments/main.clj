(ns payments.main
  (:require [edd.core :as edd]
            [shop.schemas :as s]))

(def AuthorizePayment
  [:map [:attrs [:map [:amount s/Money] [:method [:enum :card :blik :transfer]]]]])

(defn authorize-payment
  [{:keys [customer limit]} {:keys [id attrs]}]
  (cond
    (nil? customer) {:event-id :payment-declined :id id :attrs {:reason :unknown-customer}}
    (> (get-in attrs [:amount :amount]) (:limit limit))
    {:event-id :payment-declined :id id :attrs {:reason :limit}}
    :else {:event-id :payment-authorized :id id :attrs attrs}))

(defn refund-payment
  [_deps {:keys [id]}]
  {:event-id :payment-refunded :id id})

(defn on-authorized [_ctx event]
  {:service :orders-svc
   :commands [{:cmd-id :confirm-order :id (:id event)}]})

(defn on-declined [_ctx event]
  {:service :orders-svc
   :commands [{:cmd-id :cancel-order :id (:id event) :attrs {:reason :payment}}]})

(defn audit-fx
  "Runs after every command of this service."
  [_ctx _events]
  {:service :notifications-svc
   :commands [{:cmd-id :log-audit :id (random-uuid)}]})

(defn register [ctx]
  (-> ctx
      (assoc :service-name :payments-svc)
      (edd/reg-cmd :authorize-payment authorize-payment
                   :consumes AuthorizePayment
                   :deps {:customer {:service :customers-svc
                                     :query (fn [cmd] {:query-id :get-customer :id (:customer-id cmd)})}
                          ;; Deliberate defect: customers-svc has no :get-credit-limit query.
                          :limit {:service :customers-svc
                                  :query (fn [cmd] {:query-id :get-credit-limit :id (:customer-id cmd)})}}
                   :auth {:role :non-interactive})
      (edd/reg-cmd :refund-payment refund-payment
                   :auth {:role [:support]})
      (edd/reg-event :payment-authorized (fn [agg _] (assoc agg :status :authorized)))
      (edd/reg-event :payment-declined (fn [agg _] (assoc agg :status :declined)))
      (edd/reg-event :payment-refunded (fn [agg _] (assoc agg :status :refunded)))
      (edd/reg-event-fx :payment-authorized on-authorized)
      (edd/reg-event-fx :payment-declined on-declined)
      (edd/reg-fx audit-fx)))
