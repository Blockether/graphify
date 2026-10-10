(ns shipping.main
  (:require [edd.core :as edd]
            [shop.schemas :as s]))

(def CreateShipment
  [:map [:attrs [:map [:order-id :uuid] [:address {:optional true} s/Address]]]])

(defn create-shipment [_deps {:keys [id attrs]}]
  {:event-id :shipment-created :id id :attrs attrs})

(defn dispatch-shipment [_deps {:keys [id attrs]}]
  {:event-id :shipment-dispatched :id id :attrs attrs})

(defn- carrier-for [country]
  (case country
    "PL" :inpost
    :dhl))

(defn- carrier-command
  "The command id is computed from the carrier name."
  [carrier event]
  {:service :carriers-gateway
   :commands [{:cmd-id (keyword (str "book-" (name carrier)))
               :id (:id event)}]})

(defn on-created [_ctx event]
  [{:service :notifications-svc
    ;; Deliberate defect: :template is a keyword in the schema, not a string.
    :commands [{:cmd-id :send-email :id (:id event) :attrs {:template "shipment-created"}}]}
   (carrier-command (carrier-for (get-in event [:attrs :country])) event)])

(defn- next-step
  "Deliberate dynamic reference: the command id comes from event data."
  [event]
  {:service :orders-svc
   :commands [{:cmd-id (get-in event [:attrs :next-step])
               :id (:id event)}]})

(defn on-dispatched [_ctx event]
  [{:service :orders-svc
    :commands [{:cmd-id :mark-shipped :id (get-in event [:attrs :order-id])}]}
   (next-step event)
   ;; Deliberate defect: billing-svc is not part of this corpus.
   {:service :billing-svc
    :commands [{:cmd-id :create-invoice :id (get-in event [:attrs :order-id])}]}])

(defn register [ctx]
  (-> ctx
      (assoc :service-name :shipping-svc)
      (edd/reg-cmd :create-shipment create-shipment :consumes CreateShipment)
      (edd/reg-cmd :dispatch-shipment dispatch-shipment
                   :consumes [:map [:attrs [:map [:carrier [:enum :dhl :ups]] [:tracking s/TrackingNumber]]]])
      (edd/reg-event :shipment-created (fn [agg e] (merge agg (:attrs e))))
      (edd/reg-event :shipment-dispatched (fn [agg _] (assoc agg :status :dispatched)))
      (edd/reg-event-fx :shipment-created on-created)
      (edd/reg-event-fx :shipment-dispatched on-dispatched)))
