"""Tests for the Clojure extractor (graphify/extractors/clojure.py).

The grammar is Blockether/tree-sitter-clojure. The cases cover Clojure up to
1.12 (qualified methods, array classes, param tags) and the 1.13 alphas
(destructuring directives and unicode tagged literals).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_clojure")

from graphify.detect import FileType, classify_file
from graphify.extract import extract
from graphify.extractors.clojure import extract_clojure


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _labels(r) -> set[str]:
    return {n["label"] for n in r["nodes"]}


def _rel_pairs(r, relation: str) -> set[tuple[str, str]]:
    lab = {n["id"]: n["label"] for n in r["nodes"]}
    return {
        (lab.get(e["source"], e["source"]), lab.get(e["target"], e["target"]))
        for e in r["edges"]
        if e["relation"] == relation
    }


def _targets(r, source: str, relation: str) -> set[str]:
    return {t for s, t in _rel_pairs(r, relation) if s == source}


def _corpus(tmp_path: Path, files: dict[str, str]):
    paths = [_write(tmp_path, name, body) for name, body in files.items()]
    return extract(paths, cache_root=tmp_path / "cache", parallel=False)


# ── single file: definitions ────────────────────────────────────────────────

DEFS = """\
(ns demo.defs
  "Demo definitions."
  (:require [clojure.string :as str]))

(defn public-fn [x] (str/trim x))
(defn- private-fn [x] x)
(definline inl [x] x)
(defmacro my-macro [& body] `(do ~@body))
(def ^:const limit 10)
(defonce state (atom {}))
(defmulti render :kind)
(defmethod render :circle [m] (public-fn (:name m)))
(defprotocol Shape
  "A shape."
  (area [this])
  (perimeter [this]))
(defrecord Circle [r]
  Shape
  (area [_] (* Math/PI r r)))
(deftype Box [w])
(definterface Named (getName []))
(deftest-ish ignored)
"""


def test_namespace_and_file_nodes(tmp_path):
    r = extract_clojure(_write(tmp_path, "defs.clj", DEFS))
    assert "error" not in r or r["error"] is None
    labels = _labels(r)
    assert {"defs.clj", "demo.defs"} <= labels
    assert ("defs.clj", "demo.defs") in _rel_pairs(r, "contains")
    assert r["clojure"]["ns"] == "demo.defs"


def test_vars_functions_and_macros(tmp_path):
    r = extract_clojure(_write(tmp_path, "defs.clj", DEFS))
    contained = _targets(r, "demo.defs", "contains")
    for label in ("public-fn()", "private-fn()", "inl()", "my-macro (macro)", "limit",
                  "state", "render (multimethod)", "render :circle", "Shape", "Circle",
                  "Box", "Named"):
        assert label in contained, label


def test_multimethod_implementation(tmp_path):
    r = extract_clojure(_write(tmp_path, "defs.clj", DEFS))
    assert ("render :circle", "render (multimethod)") in _rel_pairs(r, "implements")
    assert ("render :circle", "public-fn()") in _rel_pairs(r, "calls")


def test_protocol_methods_and_record_implementation(tmp_path):
    r = extract_clojure(_write(tmp_path, "defs.clj", DEFS))
    assert {".area()", ".perimeter()"} <= _targets(r, "Shape", "method")
    assert ".area()" in _targets(r, "Circle", "method")
    assert ("Circle", "Shape") in _rel_pairs(r, "implements")


def test_record_constructors_resolve_to_record(tmp_path):
    src = DEFS + "(defn make [] [(->Circle 1) (map->Circle {:r 2}) (->Box 1)])\n"
    r = extract_clojure(_write(tmp_path, "defs.clj", src))
    assert {"Circle", "Box"} <= _targets(r, "make()", "calls")


def test_docstring_becomes_rationale(tmp_path):
    r = extract_clojure(_write(tmp_path, "defs.clj", DEFS))
    assert ("Demo definitions.", "demo.defs") in _rel_pairs(r, "rationale_for")


def test_specs_and_tests(tmp_path):
    src = """\
(ns demo.spec
  (:require [clojure.spec.alpha :as s]
            [clojure.test :refer [deftest is testing]]))
(defn run [x] x)
(s/def ::id uuid?)
(s/fdef run :args (s/cat :x int?))
(deftest run-test (testing "runs" (is (= 1 (run 1)))))
"""
    r = extract_clojure(_write(tmp_path, "spec.clj", src))
    contained = _targets(r, "demo.spec", "contains")
    assert {"::id (spec)", "run (spec)", "run-test (test)"} <= contained
    assert ("run (spec)", "run()") in _rel_pairs(r, "implements")
    assert ("run-test (test)", "run()") in _rel_pairs(r, "calls")


# ── single file: references and lexical scope ───────────────────────────────


def test_calls_and_forward_declarations(tmp_path):
    src = """\
(ns demo.calls)
(declare later)
(defn first-fn [x] (later x))
(defn later [x] (inc x))
(defn value-user [] (map later [1 2]) #'later)
"""
    r = extract_clojure(_write(tmp_path, "calls.clj", src))
    assert ("first-fn()", "later()") in _rel_pairs(r, "calls")
    assert ("value-user()", "later()") in _rel_pairs(r, "references")


def test_local_bindings_shadow_vars(tmp_path):
    src = """\
(ns demo.shadow)
(defn helper [] 1)
(defn by-let [] (let [helper 2] helper))
(defn by-param [helper] (helper))
(defn by-fn [] (fn [helper] (helper)))
(defn by-letfn [] (letfn [(helper [] 3)] (helper)))
(defn by-for [] (for [x [1] :let [helper x]] (helper)))
(defn by-loop [] (loop [helper 1] (helper)))
(defn by-vector [[a helper & more]] (helper))
(defn not-shadowed [] (let [x (helper)] x))
"""
    r = extract_clojure(_write(tmp_path, "shadow.clj", src))
    callers = {s for s, t in _rel_pairs(r, "calls") | _rel_pairs(r, "references")
               if t == "helper()"}
    assert callers == {"not-shadowed()"}


def test_comment_quote_and_discard_are_ignored(tmp_path):
    src = """\
(ns demo.ignore)
(defn target [] 1)
(comment (target))
(defn quoted [] '(target) (quote (target)) #_(target))
"""
    r = extract_clojure(_write(tmp_path, "ignore.clj", src))
    assert not any(t == "target()" for _, t in _rel_pairs(r, "calls"))


# ── namespace declarations ──────────────────────────────────────────────────


def test_ns_require_forms(tmp_path):
    src = """\
(ns demo.req
  (:require [app.a :as a]
            [app.b :refer [x y] :rename {x z}]
            [app.c :as-alias c]
            (app.prefix [d :as d] e)
            app.plain
            #?@(:clj [[app.jvm :as jvm]] :cljs [[app.web :as web]]))
  (:use [app.used])
  (:import (java.util UUID Date)
           [java.time Instant]
           java.io.File))
"""
    r = extract_clojure(_write(tmp_path, "req.clj", src))
    imported = _targets(r, "req.clj", "imports")
    for ns in ("app.a", "app.b", "app.prefix.d", "app.prefix.e", "app.plain", "app.jvm",
               "app.web", "app.used", "java.util.UUID", "java.util.Date",
               "java.time.Instant", "java.io.File"):
        assert ns in imported, ns


def test_toplevel_require_and_in_ns(tmp_path):
    src = """\
(in-ns 'demo.script)
(require '[app.tool :as tool])
(defn go [] (tool/run))
"""
    r = extract_clojure(_write(tmp_path, "script.clj", src))
    assert r["clojure"]["ns"] == "demo.script"
    assert "app.tool" in _targets(r, "script.clj", "imports")


# ── cross-file resolution ───────────────────────────────────────────────────


CROSS = {
    "src/app/util.clj": """\
(ns app.util)
(defn slug [s] s)
(defmacro with-thing [& body] `(do ~@body))
(defonce state (atom {}))
(defprotocol Shape (area [this]))
(defmulti render :kind)
""",
    "src/app/model.cljc": """\
(ns app.model)
(defn make-model [] #?(:clj (java.util.HashMap.) :cljs (js-obj)))
""",
    "src/app/core.clj": """\
(ns app.core
  (:require [app.util :as u :refer [slug]]
            [app.model :refer :all]))
(defrecord Circle [r] u/Shape (area [_] r))
(defn run [x]
  (u/with-thing (slug x) (make-model) @u/state))
(defmethod u/render :square [m] (run m))
(extend-protocol u/Shape String (area [s] (count s)))
""",
    "test/app/core_test.clj": """\
(ns app.core-test (:require [clojure.test :refer [deftest is]] [app.core :as c]))
(deftest run-test (is (= 1 (c/run 1))))
""",
}


def test_cross_file_alias_refer_and_refer_all(tmp_path):
    r = _corpus(tmp_path, CROSS)
    calls = _targets(r, "run()", "calls")
    assert {"slug()", "with-thing (macro)", "make-model()"} <= calls
    assert ("run()", "state") in _rel_pairs(r, "references")


def test_cross_file_protocol_multimethod_and_tests(tmp_path):
    r = _corpus(tmp_path, CROSS)
    assert ("Circle", "Shape") in _rel_pairs(r, "implements")
    assert ("render :square", "render (multimethod)") in _rel_pairs(r, "implements")
    assert ("run-test (test)", "run()") in _rel_pairs(r, "calls")


def test_cross_file_imports_point_at_namespace_nodes(tmp_path):
    r = _corpus(tmp_path, CROSS)
    assert {"app.util", "app.model"} <= _targets(r, "core.clj", "imports")
    assert "app.core" in _targets(r, "core_test.clj", "imports")


# ── Clojure 1.12 syntax ─────────────────────────────────────────────────────


def test_clojure_1_12_method_values_and_array_classes(tmp_path):
    src = """\
(ns demo.twelve (:import (java.util UUID)))
(defn length [] 0)
(defn new [] 0)
(defn uses [^[long] xs]
  (map String/.length ["a"])
  (map Integer/new [1])
  (into-array String/1 [])
  (UUID/randomUUID)
  (^[String] String/valueOf "x"))
"""
    r = extract_clojure(_write(tmp_path, "twelve.clj", src))
    assert "error" not in r or r["error"] is None
    out = _targets(r, "uses()", "calls") | _targets(r, "uses()", "references")
    assert "length()" not in out
    assert "new()" not in out
    assert not [ref for ref in r["clojure"]["refs"] if ref["name"] in (".length", "new", "1")]


# ── Clojure 1.13 syntax ─────────────────────────────────────────────────────


def test_clojure_1_13_required_key_directives(tmp_path):
    src = """\
(ns demo.thirteen)
(defn a [] 1)
(defn b [] 1)
(defn c [] 1)
(defn d [] 1)
(defn use-keys! [{:keys! [a] :syms! [b] :strs! [c] :demo/keys! [d]}]
  (a) (b) (c) (d))
"""
    r = extract_clojure(_write(tmp_path, "thirteen.clj", src))
    assert not _targets(r, "use-keys!()", "calls")


def test_clojure_1_13_select_and_status_directives(tmp_path):
    src = """\
(ns demo.select)
(defn sel [] 1)
(defn defs [] 1)
(defn everything [] 1)
(defn extra [] 1)
(defn gone [] 1)
(defn fallback [] 1)
(defn directives
  [{:keys [x] :select sel :defaults defs :all everything :excess extra :missing gone
    :or {x (fallback)}}]
  (sel) (defs) (everything) (extra) (gone) x)
"""
    r = extract_clojure(_write(tmp_path, "select.clj", src))
    assert not _targets(r, "directives()", "calls")


def test_clojure_1_13_key_binding_pairs(tmp_path):
    src = """\
(ns demo.pairs)
(defn local [] 1)
(defn key-fn [] 1)
(defn pairs [{local :key-fn} & {:keys [opt]}] (local) (key-fn) opt)
"""
    r = extract_clojure(_write(tmp_path, "pairs.clj", src))
    assert _targets(r, "pairs()", "calls") == {"key-fn()"}


def test_clojure_1_13_tagged_and_record_literals(tmp_path):
    src = """\
(ns demo.tags)
(defrecord Rec [a])
(defn helper [] 1)
(defn tags [] [#inst "2020-01-01" #uuid "00000000-0000-0000-0000-000000000000"
               #demo.tags.Rec{:a (helper)} #other.ns.Thing[1] #λ/tag 1])
"""
    r = extract_clojure(_write(tmp_path, "tags.clj", src))
    assert "Rec" in _targets(r, "tags()", "references")
    assert "helper()" in _targets(r, "tags()", "calls")
    names = {ref["name"] for ref in r["clojure"]["refs"]}
    assert "inst" not in names and "uuid" not in names
    assert {"ns": ["other.ns"], "name": "Thing"}.items() <= next(
        ref for ref in r["clojure"]["refs"] if ref["name"] == "Thing").items()


def test_cross_file_record_literal(tmp_path):
    r = _corpus(tmp_path, {
        "src/m/rec.clj": "(ns m.rec)\n(defrecord Point [x y])\n",
        "src/m/use.clj": "(ns m.use (:require [m.rec]))\n(def origin #m.rec.Point{:x 0 :y 0})\n",
    })
    assert ("origin", "Point") in _rel_pairs(r, "references")


# ── dialects, build files and detection ─────────────────────────────────────


def test_clojurescript_and_babashka_files(tmp_path):
    r = _corpus(tmp_path, {
        "src/web/app.cljs": "(ns web.app)\n(defn ^:export main [] (js/console.log 1))\n",
        "bin/task": "#!/usr/bin/env bb\n(ns task)\n(defn -main [] 1)\n",
    })
    labels = _labels(r)
    assert {"web.app", "main()", "task", "-main()"} <= labels


def test_deps_edn_and_project_clj_dependencies(tmp_path):
    r = _corpus(tmp_path, {
        "deps.edn": '{:deps {org.clojure/clojure {:mvn/version "1.12.0"}'
                    ' io.github.x/y {:git/sha "abc"}}'
                    ' :aliases {:test {:extra-deps {lambdaisland/kaocha {}}}}}\n',
        "project.clj": '(defproject demo "0.1.0" :dependencies [[ring/ring-core "1.12.0"]])\n',
    })
    deps = _targets(r, "deps.edn", "imports")
    assert {"org.clojure/clojure", "io.github.x/y", "lambdaisland/kaocha"} <= deps
    assert "ring/ring-core" in _targets(r, "project.clj", "imports")


@pytest.mark.parametrize("name", ["a.clj", "a.cljs", "a.cljc", "a.bb", "deps.edn"])
def test_clojure_extensions_are_code(tmp_path, name):
    assert classify_file(_write(tmp_path, name, "(ns a)\n")) == FileType.CODE


def test_parse_errors_do_not_raise(tmp_path):
    r = extract_clojure(_write(tmp_path, "broken.clj", "(ns broken)\n(defn f [x] (inc x\n"))
    assert "broken.clj" in _labels(r)


# ── pipeline integration: id remap and incremental rebuilds ─────────────────


def test_cross_file_calls_survive_root_relative_ids(tmp_path):
    # `graphify update` passes the project root as cache_root, so extract()
    # rewrites node ids relative to it after extraction. The resolver must see
    # the rewritten ids, or every cross-file edge is lost.
    paths = [_write(tmp_path, name, body) for name, body in CROSS.items()]
    r = extract(paths, cache_root=tmp_path, parallel=False)
    assert {"slug()", "with-thing (macro)", "make-model()"} <= _targets(r, "run()", "calls")
    assert "app.util" in _targets(r, "core.clj", "imports")
    ids = {n["id"] for n in r["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in r["edges"])


def test_definition_nodes_carry_resolver_metadata(tmp_path):
    r = extract_clojure(_write(tmp_path, "defs.clj", DEFS))
    meta = {n["label"]: n.get("metadata") for n in r["nodes"]}
    assert meta["demo.defs"] == {"language": "clojure", "kind": "namespace",
                                 "module": "demo.defs"}
    assert meta["public-fn()"] == {"language": "clojure", "kind": "var",
                                   "module": "demo.defs", "name": "public-fn"}


def test_incremental_rebuild_resolves_into_unchanged_files(tmp_path):
    paths = [_write(tmp_path, name, body) for name, body in CROSS.items()]
    full = extract(paths, cache_root=tmp_path, parallel=False)
    changed = tmp_path / "src/app/core.clj"
    # Mirror watch.py: context nodes keep id, label, source_file, types and metadata.
    context = [
        {k: n[k] for k in ("id", "label", "source_file", "file_type", "type", "metadata")
         if k in n}
        for n in full["nodes"]
        if n.get("source_file") and not n["source_file"].endswith("core.clj")
    ]
    r = extract([changed], cache_root=tmp_path, parallel=False,
                resolution_context_nodes=context)
    by_id = {n["id"]: n["label"] for n in full["nodes"]}
    calls = {by_id.get(e["target"]) for e in r["edges"]
             if e["relation"] == "calls" and by_id.get(e["source"]) == "run()"}
    assert {"slug()", "with-thing (macro)", "make-model()"} <= calls
    imports = {by_id.get(e["target"]) for e in r["edges"] if e["relation"] == "imports"}
    assert {"app.util", "app.model"} <= imports
