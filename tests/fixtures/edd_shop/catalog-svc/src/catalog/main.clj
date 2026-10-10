(ns catalog.main
  (:require [edd.core :as edd]
            [shop.schemas :as s]))

(defn get-product [_deps query]
  {:sku (:sku query)})

(defn list-products [_deps _query]
  [])

(defn upsert-product [_deps {:keys [id attrs]}]
  {:event-id :product-upserted :id id :attrs attrs})

(defn register [ctx]
  (-> ctx
      (assoc :service-name :catalog-svc)
      (edd/reg-cmd :upsert-product upsert-product
                   :consumes [:map [:attrs s/ProductView]]
                   :auth {:role :merchant})
      (edd/reg-event :product-upserted (fn [agg e] (merge agg (:attrs e))))
      (edd/reg-query :get-product get-product
                     :consumes [:map [:sku :stirng]]
                     :produces [:maybe s/ProductView])
      (edd/reg-query :list-products list-products
                     :produces [:vector s/ProductView])))
