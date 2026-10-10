(ns customers.main
  (:require [edd.core :as edd]
            [edd.common :as common]
            [shop.schemas :as s]))

(def Customer
  [:map
   [:id :uuid]
   [:name :string]
   [:email s/Email]
   [:addresses [:vector s/Address]]
   [:tier [:enum :basic :gold]]])

(defn register-customer [_deps {:keys [id attrs]}]
  {:event-id :customer-registered :id id :attrs attrs})

(defn update-address [_deps {:keys [id attrs]}]
  {:event-id :address-updated :id id :attrs attrs})

(defn get-customer [_deps query]
  {:id (:id query)})

(defn init [ctx]
  (-> ctx
      (assoc :service-name :customers-svc)
      (edd/reg-service-schema [:map [:tier [:enum :basic :gold]]])
      (edd/reg-cmd :register-customer register-customer
                   :consumes [:map [:attrs [:map
                                              [:name [:string {:min 3 :max 1}]]
                                              [:email s/Email]
                                              [:phone {:optional true} [:re "^[0-9+"]]]]])
      (edd/reg-cmd :update-address update-address
                   :consumes [:map [:attrs s/Address]])
      ;; Deliberate defect: the same command is registered twice.
      (edd/reg-cmd :update-address update-address)
      (edd/reg-event :customer-registered (fn [agg e] (merge agg (:attrs e))))
      (edd/reg-event :address-updated (fn [agg e] (update agg :addresses conj (:attrs e))))
      (edd/reg-query :get-by-id common/get-by-id)
      (edd/reg-query :get-customer get-customer
                     :consumes [:map [:id :uuid]]
                     :produces Customer)))
