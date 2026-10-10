(ns shop.schemas
  "Malli schemas that every shop service shares.")

(def Currency [:enum :EUR :USD :PLN])

(def Money
  [:map {:closed true}
   [:amount [:int {:min 0}]]
   [:currency Currency]])

(def Address
  [:map
   [:street [:string {:min 1 :max 120}]]
   [:city :string]
   [:zip [:re #"^[0-9]{2}-[0-9]{3}$"]]
   [:country {:optional true} [:enum "PL" "DE" "US"]]])

(def LineItem
  [:map
   [:sku :string]
   [:quantity pos-int?]
   [:unit-price Money]])

(def Email [:re #"^[^@]+@[^@]+$"])

(def ProductView
  [:map
   [:sku :string]
   [:name :string]
   [:price Money]
   [:tags [:set :keyword]]
   [:attributes [:map-of :keyword :string]]])
