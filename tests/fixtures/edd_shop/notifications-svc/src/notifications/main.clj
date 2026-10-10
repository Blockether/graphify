(ns notifications.main
  (:require [edd.core :as edd :refer [reg-cmd reg-event reg-event-fx]]
            [shop.schemas :as s]))

(defn send-email [_deps {:keys [id attrs]}]
  {:event-id :email-sent :id id :attrs attrs})

(defn log-audit [_deps {:keys [id]}]
  {:event-id :audit-logged :id id})

(defn on-bounced [_ctx event]
  (let [retry :send-email]
    {:commands [{:cmd-id retry :id (:id event) :attrs {:template :bounce-retry}}]}))

(defn register [ctx]
  (-> ctx
      (assoc :service-name :notifications-svc)
      (reg-cmd :send-email send-email
               :consumes [:map [:attrs [:map [:template :keyword] [:to {:optional true} s/Email]]]])
      (reg-cmd :log-audit log-audit)
      (reg-event :email-sent (fn [agg _] (update agg :sent (fnil inc 0))))
      (reg-event :audit-logged (fn [agg _] agg))
      ;; Deliberate defect: no command emits :email-bounced.
      (reg-event-fx :email-bounced on-bounced)))
