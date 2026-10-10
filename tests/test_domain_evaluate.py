"""Tests for constant resolution of computed names (graphify/domain/evaluate.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_clojure")

from graphify.domain.corpus import Corpus
from graphify.domain.evaluate import Const, Evaluator, binding_names


def _corpus(tmp_path: Path, body: str) -> tuple[Corpus, Evaluator]:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src/ids.clj").write_text("(ns app.ids)\n(def shared :shared-id)\n", encoding="utf-8")
    (tmp_path / "src/core.clj").write_text("(ns app.core (:require [app.ids :as ids]))\n" + body, encoding="utf-8")
    corpus = Corpus.load(tmp_path)
    return corpus, Evaluator(corpus)


def _values(corpus: Corpus, ev: Evaluator, var: str) -> list[str]:
    d = corpus.definitions[f"app.core/{var}"]
    return [v.value for v in ev.values(d.value, d.file)]


def test_vars_aliases_and_core_functions(tmp_path):
    corpus, ev = _corpus(tmp_path, '''
(def a :alpha)
(def b a)
(def c ids/shared)
(def d (keyword "x" "y"))
(def e (keyword (str "book-" (name :dhl))))
(def f (str :k "-" 1))
(def g (get {:pl :inpost :de :dhl} :de))
(def h (:pl {:pl :inpost}))
''')
    assert _values(corpus, ev, "a") == ["alpha"]
    assert _values(corpus, ev, "b") == ["alpha"]
    assert _values(corpus, ev, "c") == ["shared-id"]
    assert _values(corpus, ev, "d") == ["x/y"]
    assert _values(corpus, ev, "e") == ["book-dhl"]
    assert _values(corpus, ev, "f") == [":k-1"]
    assert _values(corpus, ev, "g") == ["dhl"]
    assert _values(corpus, ev, "h") == ["inpost"]


def test_branches_give_every_candidate(tmp_path):
    corpus, ev = _corpus(tmp_path, '''
(defn pick [x] (case x "PL" :inpost :dhl))
(def a (pick "PL"))
(def b (if (rand-int 2) :left :right))
(def c (cond (rand-int 2) :one :else :two))
(def d (or (System/getenv "X") :fallback))
''')
    assert _values(corpus, ev, "a") == ["inpost", "dhl"]
    assert _values(corpus, ev, "b") == ["left", "right"]
    assert _values(corpus, ev, "c") == ["one", "two"]
    assert _values(corpus, ev, "d") == []  # one branch is runtime data


def test_let_bindings_and_parameters(tmp_path):
    corpus, ev = _corpus(tmp_path, '''
(defn make [carrier] (keyword (str "book-" (name carrier))))
(def a (let [c :ups] (make c)))
(def b (let [m {:k :v}] (get m :k)))
(defn shadow [a] a)
(def c (shadow (rand)))
''')
    assert _values(corpus, ev, "a") == ["book-ups"]
    assert _values(corpus, ev, "b") == ["v"]
    assert _values(corpus, ev, "c") == []  # the parameter shadows the var `a`


def test_runtime_data_and_cycles_stay_unknown(tmp_path):
    corpus, ev = _corpus(tmp_path, '''
(def a (get-in event [:attrs :next]))
(def loop-a loop-b)
(def loop-b loop-a)
(def c (keyword (slurp "x")))
''')
    assert _values(corpus, ev, "a") == []
    assert _values(corpus, ev, "loop-a") == []
    assert _values(corpus, ev, "c") == []


def test_const_text_and_binding_names():
    assert Const("kw", "a/b").as_str() == ":a/b"
    assert Const("kw", "a/b").as_name() == "b"
    from graphify.domain import edn
    (target,) = edn.read_string("[x {:keys [y z] :as all} & more]")
    assert binding_names(target) == ["x", "y", "z", "all", "more"]
