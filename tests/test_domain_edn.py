"""Tests for the domain reader and corpus (graphify/domain/edn.py, corpus.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_clojure")

from graphify.domain import edn
from graphify.domain.corpus import Corpus


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_reader_keeps_values_and_lines():
    (form,) = edn.read_string('''{:a 1 :b/c "x\\ny" :d [sym ns/sym] :e #{:k} :f nil :g true}''')
    assert form.kind == edn.MAP
    assert form.get("a").value == "1"
    assert form.get("b/c").value == "x\ny"
    assert [s.value for s in form.get("d").items] == ["sym", "ns/sym"]
    assert form.get("d").items[1].name == "sym"
    assert form.get("d").items[1].namespace == "ns"
    assert form.get("e").kind == edn.SET
    assert form.get("f").kind == edn.NIL
    assert form.get("g").value is True


def test_reader_drops_noise_and_picks_clj_branch():
    forms = edn.read_string('''
;; comment
#_(ignored)
^:private (def x #?(:cljs :browser :clj :jvm))
(f #(inc %) @state #'var-ref 'quoted)
''')
    assert len(forms) == 2
    assert forms[0].items[2].value == "jvm"
    call = forms[1]
    assert [i.kind for i in call.items] == [edn.SYMBOL, edn.FN, edn.DEREF, edn.SYMBOL, edn.QUOTE]
    assert call.items[3].value == "var-ref"


def test_auto_resolved_keywords_use_namespace_and_aliases(tmp_path):
    _write(tmp_path, "src/app/core.clj", '''(ns app.core (:require [app.other :as o]))
(def m {::local 1 ::o/remote 2 :plain 3})
''')
    corpus = Corpus.load(tmp_path)
    m = corpus.definitions["app.core/m"].value
    assert [k.value for k, _ in m.pairs()] == ["app.core/local", "app.other/remote", "plain"]


def test_corpus_resolves_aliases_refers_and_definitions(tmp_path):
    _write(tmp_path, "src/a/lib.clj", "(ns a.lib)\n(defn helper [x] {:id x})\n(def Schema [:map])\n")
    _write(tmp_path, "src/a/main.clj", '''(ns a.main
  (:require [a.lib :as lib :refer [Schema]]
            [clojure.string :as str]))
(defn run [] (lib/helper 1) (str/join "," []))
''')
    _write(tmp_path, "test/a/main_test.clj", "(ns a.main-test)\n")
    corpus = Corpus.load(tmp_path)
    main = corpus.by_ns["a.main"]
    assert main.aliases == {"lib": "a.lib", "str": "clojure.string"}
    assert corpus.resolve("lib/helper", main) == "a.lib/helper"
    assert corpus.resolve("Schema", main) == "a.lib/Schema"
    assert corpus.resolve("run", main) == "a.main/run"
    assert corpus.definition("lib/helper", main).file.rel == "src/a/lib.clj"
    assert corpus.definitions["a.lib/Schema"].value.kind == edn.VECTOR
    calls = [t for t, _ in corpus.calls(main, {"a.lib/helper", "clojure.string/join"})]
    assert calls == ["a.lib/helper", "clojure.string/join"]
    assert corpus.by_ns["a.main-test"].is_test
    assert main.requires("a.lib") and not main.requires("edd.core")


def test_to_edn_round_trips_structure():
    (form,) = edn.read_string('[:map {:closed true} [:a [:enum "x" :y]] #{1}]')
    assert edn.to_edn(form) == '[:map {:closed true} [:a [:enum "x" :y]] #{1}]'
