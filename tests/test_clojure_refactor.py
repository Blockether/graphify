"""Exact references and renames of Clojure vars (graphify.clojure_refactor)."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from graphify.clojure_refactor import StaleFile, apply_rename, plan_rename, references, rewrite
from graphify.extractors.clojure import extract_clojure

CORE = """\
(ns app.core
  (:require [app.util :as u :refer [helper]]
            [app.proto :refer [Shape area]]
            [app.records :as rec]))

(declare later)

(defn run [helper x]
  (helper x))

(defn go [x]
  (let [y (helper x)]
    (u/helper y (later y))))

(defn later [y] #'u/helper)

(defmethod u/handle :a [m] (u/helper m))

(defn sizes [] [(area (rec/->Sq 3)) :helper "helper"])

(comment
  (helper 1))
"""

UTIL = """\
(ns app.util)

(defmulti handle :type)

(defn helper
  "Return x. The helper is the identity."
  [x]
  x)

(defmacro with-helper [& body]
  `(helper (do ~@body)))

(defn uses-redefs [] (with-redefs [helper identity] (helper :helper)))

(def resolved (resolve 'app.util/helper))
"""

OTHER = """\
(ns app.other
  (:require [app.util :refer [helper] :rename {helper h}]))

(defn f [] (h 1))
"""

PROTO = """\
(ns app.proto)

(defprotocol Shape
  (area [s]))
"""

RECORDS = """\
(ns app.records
  (:require [app.proto :as p]))

(defrecord Sq [n]
  p/Shape
  (area [_] (* n n)))

(defn mk [] (->Sq 2))

(defn mk2 [] (map->Sq {:n 1}))
"""

FILES = {"src/app/core.clj": CORE, "src/app/util.clj": UTIL, "src/app/other.clj": OTHER,
         "src/app/proto.clj": PROTO, "src/app/records.clj": RECORDS}


@pytest.fixture
def corpus(tmp_path: Path) -> dict[str, Path]:
    out = {}
    for name, body in FILES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        out[Path(name).stem] = path
    return out


def _roles(occs, stem: str) -> Counter:
    return Counter(o.role for o in occs if Path(o.file).stem == stem)


def test_references_follow_resolution_and_skip_locals(corpus):
    occs = references("app.util", "helper", corpus.values())
    assert _roles(occs, "core") == Counter({"use": 4, "refer": 1, "comment": 1})
    assert _roles(occs, "util") == Counter({"definition": 1, "use": 4})
    assert _roles(occs, "other") == Counter({"refer": 2, "use": 1})
    # `run` binds a parameter named helper: its call is the local, not the var.
    run_line = CORE.splitlines().index("  (helper x))") + 1
    assert not [o for o in occs if Path(o.file).stem == "core" and o.line == run_line]
    assert {o.text for o in occs} == {"helper", "h"}


def test_rename_rewrites_every_site_and_keeps_data(corpus):
    plan = plan_rename("app.util", "helper", "assist", corpus.values())
    assert plan.conflicts == ()
    preview = rewrite(plan)
    assert set(preview) == set(plan.files)
    assert all(old == Path(path).read_bytes() for path, (old, _) in preview.items())  # no writes yet
    assert {Path(f).stem for f in plan.files} == {"core", "util", "other"}
    assert apply_rename(plan) == list(plan.files)
    core = corpus["core"].read_text()
    assert "[app.util :as u :refer [assist]]" in core
    assert "(defn run [helper x]\n  (helper x))" in core  # the local stays
    assert "(let [y (assist x)]" in core and "(u/assist y (later y))" in core
    assert "#'u/assist" in core and "(u/assist m)" in core and "(comment\n  (assist 1))" in core
    assert ':helper "helper"' in core  # keyword and string are data
    util = corpus["util"].read_text()
    assert "(defn assist\n" in util and "`(assist (do ~@body))" in util
    assert "(with-redefs [assist identity] (assist :helper))" in util
    assert "(resolve 'app.util/assist)" in util
    other = corpus["other"].read_text()
    assert ":refer [assist] :rename {assist h}" in other and "(h 1)" in other
    mentioned = {(Path(m.file).stem, m.text) for m in plan.mentions}
    assert ("core", ":helper") in mentioned and ("util", "helper") in mentioned  # the docstring


def test_rename_reports_conflicts(corpus):
    assert any("already defines `handle`" in c
               for c in plan_rename("app.util", "helper", "handle", corpus.values()).conflicts)
    assert any("already uses `later`" in c
               for c in plan_rename("app.util", "helper", "later", corpus.values()).conflicts)
    assert any("local `x`" in c
               for c in plan_rename("app.util", "helper", "x", corpus.values()).conflicts)
    assert any("not a valid new name" in c
               for c in plan_rename("app.util", "helper", "a/b", corpus.values()).conflicts)
    assert plan_rename("app.util", "nothing", "else", corpus.values()).edits == ()
    with pytest.raises(ValueError):
        apply_rename(plan_rename("app.util", "helper", "x", corpus.values()))


def test_rename_record_renames_its_factories(corpus):
    plan = plan_rename("app.records", "Sq", "Square", corpus.values())
    assert plan.conflicts == ()
    apply_rename(plan)
    records = corpus["records"].read_text()
    assert "(defrecord Square [n]" in records
    assert "(->Square 2)" in records and "(map->Square {:n 1})" in records
    assert "(rec/->Square 3)" in corpus["core"].read_text()


def test_rename_protocol_method_renames_implementations(corpus):
    plan = plan_rename("app.proto", "area", "surface", corpus.values())
    assert plan.conflicts == ()
    apply_rename(plan)
    assert "(surface [s])" in corpus["proto"].read_text()
    assert "(surface [_] (* n n))" in corpus["records"].read_text()
    core = corpus["core"].read_text()
    assert ":refer [Shape surface]" in core and "(surface (rec/->Sq 3))" in core


def test_apply_refuses_a_file_changed_after_the_plan(corpus):
    plan = plan_rename("app.util", "helper", "assist", corpus.values())
    corpus["util"].write_text(UTIL + "\n(defn extra [] 1)\n", encoding="utf-8")
    with pytest.raises(StaleFile):
        rewrite(plan)
    with pytest.raises(StaleFile):
        apply_rename(plan)
    assert "assist" not in corpus["core"].read_text()


def test_record_metadata_names_the_record_not_its_factory(corpus):
    nodes = extract_clojure(corpus["records"])["nodes"]
    names = {n["metadata"]["name"] for n in nodes if n.get("metadata", {}).get("kind") == "var"}
    assert "Sq" in names and "map->Sq" not in names


def test_renamed_refer_calls_the_original_var(corpus):
    refs = extract_clojure(corpus["other"])["clojure"]["refs"]
    assert {(r["name"], tuple(r["ns"])) for r in refs if r["relation"] == "calls"} == {("helper", ("app.util",))}
