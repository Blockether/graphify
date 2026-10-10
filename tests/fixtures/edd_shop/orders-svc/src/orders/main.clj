(ns orders.main
  (:require [edd.core :as edd]
            [edd.common :as common]
            [orders.commands :as cmd]
            [orders.events :as evt]
            [orders.fx :as fx]
            [orders.ids :as ids]
            [orders.queries :as q]))

(defn register
  [ctx]
  (-> ctx
      (assoc :service-name :orders-svc)
      (edd/reg-cmd :place-order cmd/place-order
                   :consumes cmd/PlaceOrder
                   :deps [:product cmd/product-dep]
                   :auth {:role :customer})
      (edd/reg-cmd :confirm-order cmd/confirm-order
                   :auth {:role :non-interactive})
      (edd/reg-cmd :cancel-order cmd/cancel-order
                   :consumes cmd/CancelOrder
                   :auth {:role [:customer :support]})
      (edd/reg-cmd :mark-shipped cmd/mark-shipped)
      (edd/reg-cmd :retry-placement cmd/retry-placement)
      ;; Deliberate defect: the handler is not defined anywhere.
      (edd/reg-cmd :archive-order archive-order-handler)
      (edd/reg-event :order-placed evt/order-placed)
      (edd/reg-event :order-confirmed evt/order-confirmed)
      (edd/reg-event :order-cancelled evt/order-cancelled)
      (edd/reg-event :order-shipped evt/order-shipped)
      (edd/reg-event :order-rejected evt/order-rejected)
      (edd/reg-event-fx :order-placed fx/on-order-placed)
      (edd/reg-event-fx :order-cancelled fx/on-order-cancelled)
      (edd/reg-event-fx :order-rejected fx/on-order-rejected)
      (edd/reg-event-fx :order-confirmed fx/on-order-confirmed)
      (edd/reg-query :get-by-id common/get-by-id)
      (edd/reg-query ids/order-summary-query q/order-summary
                     :consumes [:map [:id :uuid]]
                     :produces q/OrderSummary)
      (edd/reg-query ids/customer-orders-query q/orders-by-customer
                     :consumes [:map [:customer-id :uuid]]
                     :produces [:vector :uuid])))
