"""graphify crap: CRAP scores for Clojure and Python functions."""

import json
import subprocess

import pytest

from graphify import crap


def _clj(source: str, rel: str = "src/app/core.clj") -> dict[str, int]:
    return {u.name: u.complexity for u in crap.clojure_units(source.encode(), rel)}


def _py(source: str) -> dict[str, int]:
    return {
        f"{u.namespace}.{u.name}": u.complexity
        for u in crap.python_units(source.encode(), "pkg/mod.py", "mod")
    }


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_crap_formula_matches_savoia_and_evans():
    assert crap.crap_score(1, 100.0) == 1.0
    assert crap.crap_score(5, 0.0) == 30.0
    assert crap.crap_score(10, 50.0) == 22.5
    assert crap.crap_score(3, None) is None


@pytest.mark.parametrize(
    ("body", "cc"),
    [
        ("(+ x 1)", 1),
        ("(if x 1 2)", 2),
        ("(when-let [y x] (if-not y 1 2))", 3),
        ("(and a b c)", 3),
        ("(or a)", 1),
        ("(cond (neg? x) :a (zero? x) :b :else :c)", 3),
        ("(cond (neg? x) :a (zero? x) :b)", 3),
        ("(case x 1 :a 2 :b :default)", 3),
        ("(case x 1 :a 2 :b)", 3),
        ("(condp = x 1 :a 2 :b :other)", 3),
        ("(condp some x #{1} :>> inc #{2} :b)", 3),
        ("(cond-> x a inc b dec)", 3),
        ("(some-> x :a :b)", 3),
        ("(doseq [y xs :when (odd? y) :while y] y)", 4),
        ("(for [y xs] y)", 2),
        ("(loop [i 0] (when (< i 3) (recur (inc i))))", 3),
        ("(try (f) (catch Exception _ 1) (catch Throwable _ 2))", 3),
        ("(map (fn ([a] a) ([a b] b)) xs)", 2),
        ("'(if a b c)", 1),
        ("`(if ~x 1 2)", 1),
        ("#_(if a b c)", 1),
        ("(comment (if a b c))", 1),
        ("(str/when x)", 1),
        ("(clojure.core/when x 1)", 2),
        ("(:if m)", 1),
    ],
)
def test_clojure_decisions(body, cc):
    assert _clj(f"(ns app.core)\n(defn f [x] {body})\n") == {"f": cc}


def test_clojure_multi_arity_counts_each_arity_after_the_first():
    assert _clj("(defn f ([x] (if x 1 2)) ([x y] y) ([x y z] z))") == {"f": 4}


def test_clojure_reader_conditionals_count_the_measured_platform():
    source = "(defn f [x] #?(:clj (if x 1 2) :cljs (when x (if x 1 2))))"
    assert _clj(source, "src/app/core.cljc") == {"f": 2}
    assert _clj(source, "src/app/core.cljs") == {"f": 3}
    assert _clj("(defn f [x] #?(:cljs (if x 1 2) :default (+ x 1)))", "src/a.cljc") == {"f": 1}


def test_clojure_units_cover_methods_types_and_fn_defs():
    source = """(ns app.shapes)
(defmacro m [x] `(if ~x 1 2))
(defmulti area :shape)
(defmethod area :circle [{:keys [r]}] (if r (* r r) 0))
(def anon (fn [x] (when x x)))
(defrecord Square [s]
  Shape
  (area [_] (* s s))
  (perimeter [_] (if s (* 4 s) 0)))
(extend-protocol Show
  String (show [s] s)
  Long (show [n] (when (pos? n) n)))
(defn- private-fn [x] x)
"""
    units = {u.name: u for u in crap.clojure_units(source.encode(), "src/app/shapes.clj")}
    assert {name: u.complexity for name, u in units.items()} == {
        "area :circle": 2,
        "anon": 2,
        "Square.area": 1,
        "Square.perimeter": 2,
        "String.show": 1,
        "Long.show": 2,
        "private-fn": 1,
    }
    assert units["Square.perimeter"].child == 5
    assert units["Square.perimeter"].form_line == 6
    assert units["Long.show"].child == 5
    assert units["anon"].namespace == "app.shapes"


def test_clojure_namespace_falls_back_to_the_path():
    units = crap.clojure_units(b"(defn f [] 1)", "src/my_app/core.clj")
    assert units[0].namespace == "my-app.core"


PY_SOURCE = (
    "import os\n"
    "\n"
    "\n"
    "def busy(a, b, xs):\n"
    "    if a and b or not a:\n"
    "        pass\n"
    "    elif b:\n"
    "        pass\n"
    "    for x in xs:\n"
    "        while x:\n"
    "            x -= 1\n"
    "    try:\n"
    "        pass\n"
    "    except ValueError:\n"
    "        pass\n"
    "    except (KeyError, OSError):\n"
    "        pass\n"
    "    y = 1 if a else 2\n"
    "    zs = [z for z in xs if z]\n"
    "    match a:\n"
    "        case 1:\n"
    "            pass\n"
    "        case 2:\n"
    "            pass\n"
    "        case _:\n"
    "            pass\n"
    "    return y, zs\n"
    "\n"
    "\n"
    "class Box:\n"
    "    def get(self, k):\n"
    "        def inner(v):\n"
    "            return v if v else k\n"
    "        return inner(k)\n"
    "\n"
    "    async def put(self, k):\n"
    "        return k\n"
    "\n"
    "\n"
    "def plain():\n"
    "    return 1\n"
)


def test_python_decisions_and_units():
    assert _py(PY_SOURCE) == {"mod.busy": 14, "mod.Box.get": 2, "mod.Box.put": 1, "mod.plain": 1}
    units = {u.name: u for u in crap.python_units(PY_SOURCE.encode(), "pkg/mod.py", "mod")}
    assert (units["busy"].line, units["busy"].body_line, units["busy"].end_line) == (4, 5, 27)


def test_python_and_or_chains_count_each_operator():
    assert _py("def f(a, b, c):\n    return a and b and c\n") == {"mod.f": 3}


def _project(tmp_path):
    _write(tmp_path, "src/app/core.clj", "(ns app.core)\n(defn f [x]\n  (if x 1 2))\n"
           "(defn g [x] (when x (if x 1 2)))\n")
    _write(tmp_path, "src/app/other.clj", "(ns app.other)\n(defn h [x] (if x 1 2))\n")
    _write(tmp_path, "test/app/core_test.clj", "(ns app.core-test)\n(defn t [x] (if x 1 2))\n")
    _write(tmp_path, "pkg/mod.py", "def f(a):\n    if a:\n        return 1\n    return 2\n")
    _write(tmp_path, "tests/test_mod.py", "def test_f():\n    assert True\n")
    return tmp_path


def _clofidence(tmp_path, forms, prefixes=("app.",)):
    _write(tmp_path, "target/coverage/clofidence.json", json.dumps(
        {"format": "vis-coverage/1", "tool": "clofidence", "prefixes": list(prefixes), "forms": forms}))


def _by_name(report):
    return {(e.path, e.name): e for e in report.entries}


def test_source_files_skip_tests_unless_asked(tmp_path):
    root = _project(tmp_path)
    assert crap.source_files(root) == ["pkg/mod.py", "src/app/core.clj", "src/app/other.clj"]
    assert "test/app/core_test.clj" in crap.source_files(root, include_tests=True)
    assert crap.source_files(root, ["src/app/other.clj"]) == ["src/app/other.clj"]


def test_complexity_only_report_scores_by_complexity(tmp_path):
    report = crap.analyze(_project(tmp_path), coverage=False, use_cache=False, threshold=2)
    assert report.files == 3
    assert [(e.name, e.complexity, e.coverage) for e in report.entries][0] == ("g", 3, None)
    assert [e.name for e in report.over] == ["g"]
    assert report.coverage_reports == ()


def test_clofidence_report_joins_forms_and_marks_unloaded_namespaces(tmp_path):
    root = _project(tmp_path)
    _clofidence(root, [
        {"file": "app/core.clj", "ns": "app.core", "line": 2, "kind": "defn", "hittable": 4, "hit": 4},
        {"file": "app/core.clj", "ns": "app.core", "line": 4, "kind": "defn", "hittable": 4, "hit": 1},
    ])
    report = crap.analyze(root, use_cache=False)
    rows = _by_name(report)
    assert rows[("src/app/core.clj", "f")].coverage == 100.0
    assert rows[("src/app/core.clj", "f")].crap == 2.0
    assert rows[("src/app/core.clj", "g")].coverage == 25.0
    assert rows[("src/app/core.clj", "g")].crap == pytest.approx(9 * 0.75 ** 3 + 3, abs=1e-4)
    # app.other matches the instrumented prefix but no test loaded it.
    assert rows[("src/app/other.clj", "h")].coverage == 0.0
    assert rows[("src/app/other.clj", "h")].crap == 6.0
    assert rows[("pkg/mod.py", "f")].coverage is None
    assert report.coverage_reports == (str(root / "target/coverage/clofidence.json"),)


def test_clofidence_children_score_record_methods(tmp_path):
    _write(tmp_path, "src/app/shapes.clj", "(ns app.shapes)\n(defrecord Square [s]\n  Shape\n"
           "  (area [_] (* s s))\n  (perimeter [_] (if s (* 4 s) 0)))\n")
    _clofidence(tmp_path, [{"file": "app/shapes.clj", "ns": "app.shapes", "line": 2, "kind": "defrecord",
                            "hittable": 6, "hit": 3, "children": {"4": [2, 2], "5": [4, 1]}}])
    rows = {e.name: e for e in crap.analyze(tmp_path, use_cache=False).entries}
    assert rows["Square.area"].coverage == 100.0
    assert rows["Square.perimeter"].coverage == 25.0


def test_clofidence_rejects_other_formats(tmp_path):
    bad = _write(tmp_path, "bad.json", json.dumps({"format": "other"}))
    with pytest.raises(ValueError):
        crap.read_clofidence(bad, crap.Coverage())


def test_coverage_py_json_ignores_the_def_line(tmp_path):
    root = _project(tmp_path)
    _write(root, "coverage.json", json.dumps({"files": {str(root / "pkg/mod.py"): {
        "executed_lines": [1, 2, 3], "missing_lines": [4]}}}))
    rows = _by_name(crap.analyze(root, use_cache=False))
    assert rows[("pkg/mod.py", "f")].coverage == pytest.approx(200 / 3, rel=1e-3)
    assert rows[("src/app/core.clj", "f")].coverage is None


def test_lcov_reports_python_lines(tmp_path):
    root = _project(tmp_path)
    _write(root, "lcov.info", "TN:\nSF:pkg/mod.py\nDA:1,1\nDA:2,1\nDA:3,0\nDA:4,1\nend_of_record\n")
    rows = _by_name(crap.analyze(root, coverage=["lcov.info"], use_cache=False))
    assert rows[("pkg/mod.py", "f")].coverage == pytest.approx(200 / 3, rel=1e-3)


def test_missing_or_unknown_reports_fail(tmp_path):
    root = _project(tmp_path)
    with pytest.raises(FileNotFoundError):
        crap.analyze(root, coverage=["nope.json"], use_cache=False)
    _write(root, "report.txt", "hello")
    with pytest.raises(ValueError):
        crap.analyze(root, coverage=["report.txt"], use_cache=False)


def test_cache_reuses_unchanged_files_and_reparses_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_OUT", str(tmp_path / "out"))
    root = _project(tmp_path / "p")
    first = crap.analyze(root, coverage=False)
    assert first.cached_files == 0
    second = crap.analyze(root, coverage=False)
    assert second.cached_files == 3
    assert second.entries == first.entries
    _write(root, "src/app/other.clj", "(ns app.other)\n(defn h [x] (if x (if x 1 2) 3))\n")
    third = crap.analyze(root, coverage=False)
    assert third.cached_files == 2
    assert {e.name: e.complexity for e in third.entries}["h"] == 3


def test_parallel_scan_matches_serial(tmp_path):
    root = _project(tmp_path)
    serial = crap.analyze(root, coverage=False, use_cache=False)
    parallel = crap.analyze(root, coverage=False, use_cache=False, parallel=True)
    assert parallel.entries == serial.entries


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_changed_keeps_only_touched_functions(tmp_path):
    root = _project(tmp_path)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    assert crap.analyze(root, coverage=False, changed=True, use_cache=False).entries == ()
    _write(root, "src/app/core.clj", "(ns app.core)\n(defn f [x]\n  (if x 1 2))\n"
           "(defn g [x] (when x (if x 1 3)))\n")
    _write(root, "pkg/new.py", "def n():\n    return 1\n")
    names = {e.name for e in crap.analyze(root, coverage=False, changed=True, use_cache=False).entries}
    assert names == {"g", "n"}


def test_changed_outside_git_fails(tmp_path):
    with pytest.raises(RuntimeError):
        crap.analyze(_project(tmp_path), coverage=False, changed=True, use_cache=False)


def test_empty_project_gives_an_empty_report(tmp_path):
    report = crap.analyze(tmp_path, use_cache=False)
    assert (report.entries, report.files, report.over) == ((), 0, ())
    assert "0 functions in 0 files" in crap.format_table(report)


def test_table_counts_one_file_in_the_singular(tmp_path):
    (tmp_path / "one.py").write_text("def f():\n    return 1\n", "utf-8")
    table = crap.format_table(crap.analyze(tmp_path, coverage=False, use_cache=False))
    assert "1 function in 1 file " in table


def test_cli_exit_codes_json_and_edn(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_OUT", str(tmp_path / "out"))
    root = _project(tmp_path / "p")
    edn = tmp_path / "snap.edn"
    assert crap.main(["--root", str(root), "--no-coverage", "--json", "--edn", str(edn)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["over_threshold"] == 0
    assert {e["name"] for e in data["entries"]} == {"f", "g", "h"}
    assert ':name "g" :namespace "app.core" :complexity 3 :coverage nil :crap nil' in edn.read_text()
    assert crap.main(["--root", str(root), "--no-coverage", "--threshold", "2"]) == 2
    assert "g" in capsys.readouterr().out
    assert crap.main(["--root", str(root), "--coverage", "missing.json"]) == 1
    assert "coverage report not found" in capsys.readouterr().err
