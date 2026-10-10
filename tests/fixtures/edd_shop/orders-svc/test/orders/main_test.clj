(ns orders.main-test
  (:require [edd.core :as edd]))

;; Test code is not part of the domain model by default.
(def sample {:cmd-id :not-a-real-command :id #uuid "00000000-0000-0000-0000-000000000000"})
