"""Moves of top-level definitions and files (graphify.moves) in every supported language."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graphify.moves import apply_move, plan_move, plan_move_file
from graphify.refactor import Unsupported, load_graph


def _project(root: Path, files: dict[str, str]) -> dict:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    subprocess.run([sys.executable, "-m", "graphify", "update", "."], cwd=root, capture_output=True, check=True)
    return load_graph(root)


def _node(graph: dict, label: str, file: str) -> str:
    found = [n["id"] for n in graph["nodes"] if n.get("label") == label and n.get("source_file", "").endswith(file)]
    return found[0] if found else f"{file}::{label.removesuffix('()')}"


def _apply(plan) -> None:
    assert not plan.conflicts, plan.conflicts
    apply_move(plan)


def _read(root: Path) -> dict[str, str]:
    skip = {"graphify-out", "target", "node_modules"}
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file() and not skip & set(p.relative_to(root).parts)}


def _runs(*cmd: str) -> bool:
    if shutil.which(cmd[0]) is None:
        return False
    try:
        return subprocess.run(list(cmd), capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _check(root: Path, *cmd: str, **env) -> None:
    done = subprocess.run(list(cmd), cwd=root, capture_output=True, text=True, env={**os.environ, **env}, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "warning" not in done.stderr.replace("warning: unused manifest key", ""), done.stderr


PYTHON = {
    "app/__init__.py": "",
    "app/util.py": "import os\n\n\ndef helper(x):\n    return os.sep + str(x)\n\n\ndef other():\n    return helper(1)\n",
    "app/main.py": "from app.util import helper\n\n\ndef run():\n    return helper(2)\n",
    "tests/test_util.py": "from app import util\n\n\ndef test_helper():\n    assert util.helper(1)\n",
}


def test_python_move_function_takes_its_imports(tmp_path):
    graph = _project(tmp_path, PYTHON)
    _apply(plan_move(graph, _node(graph, "helper()", "app/util.py"), "app/tools.py", tmp_path))
    files = _read(tmp_path)
    assert files["app/tools.py"].startswith("import os\n")
    assert "def helper" not in files["app/util.py"] and "from app.tools import helper" in files["app/util.py"]
    assert "from app.tools import helper" in files["app/main.py"]
    assert "app.tools.helper(1)" in files["tests/test_util.py"]


def test_python_move_file_renames_the_module(tmp_path):
    graph = _project(tmp_path, PYTHON)
    _apply(plan_move_file(graph, "app/util.py", "app/core/text.py", tmp_path))
    files = _read(tmp_path)
    assert "app/util.py" not in files and "app/core/__init__.py" in files
    assert "from app.core.text import helper" in files["app/main.py"]
    assert "from app.core import text as util" in files["tests/test_util.py"]


CLOJURE = {
    "deps.edn": '{:paths ["src"] :aliases {:test {:extra-paths ["test"]}}}\n',
    "src/app/util.clj": '(ns app.util\n  (:require [clojure.string :as str]))\n\n(defn helper [x]\n  (str/upper-case x))\n\n(defn other []\n  (helper "a"))\n',
    "src/app/main.clj": '(ns app.main\n  (:require [app.util :as u]))\n\n(defn run []\n  (u/helper "b"))\n',
    "test/app/util_test.clj": '(ns app.util-test\n  (:require [clojure.test :refer [deftest is]]\n            [app.util :as u]))\n\n(deftest helper-test\n  (is (= "A" (u/helper "a"))))\n',
}


def test_clojure_move_namespace_moves_its_test_namespace(tmp_path):
    graph = _project(tmp_path, CLOJURE)
    _apply(plan_move_file(graph, "src/app/util.clj", "app.text.core", tmp_path))
    files = _read(tmp_path)
    assert files["src/app/text/core.clj"].startswith("(ns app.text.core")
    assert files["test/app/text/core_test.clj"].startswith("(ns app.text.core-test")
    assert "[app.text.core :as u]" in files["test/app/text/core_test.clj"]
    assert "[app.text.core :as u]" in files["src/app/main.clj"]
    assert "src/app/util.clj" not in files and "test/app/util_test.clj" not in files


def test_clojure_move_var_to_new_namespace(tmp_path):
    graph = _project(tmp_path, CLOJURE)
    _apply(plan_move(graph, _node(graph, "helper()", "src/app/util.clj"), "app.strings", tmp_path))
    files = _read(tmp_path)
    assert "[clojure.string :as str]" in files["src/app/strings.clj"] and "(defn helper" in files["src/app/strings.clj"]
    assert "[app.strings :refer [helper]]" in files["src/app/util.clj"] and "(defn helper" not in files["src/app/util.clj"]
    assert "(strings/helper \"a\")" in files["test/app/util_test.clj"]
    assert "app.util" not in files["test/app/util_test.clj"].split("(deftest")[0].replace("app.util-test", "")


SCRIPT = {
    "tsconfig.json": '{"compilerOptions": {"strict": true, "noEmit": true, "module": "esnext", "moduleResolution": "bundler", "paths": {"@/*": ["./src/*"]}}, "include": ["src"]}\n',
    "src/utils/math.ts": "export const PI = 3.14;\n\nexport function add(a: number, b: number): number {\n  return a + b;\n}\n\nexport function area(r: number): number {\n  return add(PI, r) * r;\n}\n",
    "src/main.ts": 'import { add, PI } from "./utils/math";\nimport * as m from "@/utils/math";\n\nexport const total = add(PI, 1) + m.area(2);\n',
    "src/utils/math.test.ts": 'import { add } from "./math";\n\nexport const check = add(1, 2) === 3;\n',
}


def test_script_move_function_repoints_named_and_namespace_imports(tmp_path):
    graph = _project(tmp_path, SCRIPT)
    _apply(plan_move(graph, _node(graph, "add()", "src/utils/math.ts"), "src/lib/add.ts", tmp_path))
    files = _read(tmp_path)
    assert "export function add" in files["src/lib/add.ts"]
    assert files["src/utils/math.ts"].replace("'", '"').startswith('import { add } from "../lib/add";')
    assert 'from "./lib/add"' in files["src/main.ts"]
    assert 'from "../lib/add"' in files["src/utils/math.test.ts"]
    if _runs("tsc", "--version"):
        _check(tmp_path, "tsc", "-p", ".")


def test_script_move_file_moves_its_test_file(tmp_path):
    graph = _project(tmp_path, SCRIPT)
    _apply(plan_move_file(graph, "src/utils/math.ts", "src/lib/arith.ts", tmp_path))
    files = _read(tmp_path)
    assert "src/lib/arith.ts" in files and "src/lib/arith.test.ts" in files
    assert 'from "./lib/arith"' in files["src/main.ts"] and 'from "@/lib/arith"' in files["src/main.ts"]
    assert 'from "./arith"' in files["src/lib/arith.test.ts"]
    if _runs("tsc", "--version"):
        _check(tmp_path, "tsc", "-p", ".")


GO = {
    "go.mod": "module example.com/p\n\ngo 1.21\n",
    "util/util.go": 'package util\n\nimport "strings"\n\n// Upper shouts.\nfunc Upper(s string) string {\n\treturn strings.ToUpper(s) + Suffix\n}\n\n// Suffix ends a shout.\nconst Suffix = "!"\n\nfunc Twice(s string) string {\n\treturn Upper(s) + Upper(s)\n}\n',
    "util/util_test.go": 'package util\n\nimport "testing"\n\nfunc TestUpper(t *testing.T) {\n\tif Upper("a") != "A!" {\n\t\tt.Fatal(Upper("a"))\n\t}\n}\n',
    "main.go": 'package main\n\nimport (\n\t"fmt"\n\n\t"example.com/p/util"\n)\n\nfunc main() {\n\tfmt.Println(util.Upper("x"), util.Twice("y"))\n}\n',
}


def test_go_move_function_to_other_package(tmp_path):
    graph = _project(tmp_path, GO)
    _apply(plan_move(graph, _node(graph, "Twice()", "util/util.go"), "text/text.go", tmp_path))
    files = _read(tmp_path)
    assert files["text/text.go"].startswith("package text")
    assert "util.Upper(s) + util.Upper(s)" in files["text/text.go"] and '"example.com/p/util"' in files["text/text.go"]
    assert "func Twice" not in files["util/util.go"]
    assert "text.Twice(" in files["main.go"] and '"example.com/p/text"' in files["main.go"]
    if _runs("go", "version"):
        _check(tmp_path, "go", "vet", "./...")


def test_go_move_file_to_other_package_moves_its_test(tmp_path):
    graph = _project(tmp_path, GO)
    _apply(plan_move_file(graph, "util/util.go", "core/core.go", tmp_path))
    files = _read(tmp_path)
    assert "util/util.go" not in files and files["core/core_test.go"].startswith("package core")
    assert "core.Upper(" in files["main.go"] and '"example.com/p/core"' in files["main.go"]
    if _runs("go", "version"):
        _check(tmp_path, "go", "test", "./...")


RUST = {
    "Cargo.toml": '[package]\nname = "shapes-lib"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": "pub mod geo;\npub mod util;\n\nuse crate::util::{clamp, Counter};\n\npub fn total(xs: &[i32]) -> i32 {\n    let mut c = Counter::new();\n    for x in xs {\n        c.add(clamp(*x));\n    }\n    c.value()\n}\n",
    "src/util.rs": (
        "use std::fmt::Write;\n\npub const LIMIT: i32 = 100;\n\n/// Clamp a value.\npub fn clamp(x: i32) -> i32 {\n    if x > LIMIT {\n        LIMIT\n    } else {\n        x\n    }\n}\n\n"
        "fn label(x: i32) -> String {\n    let mut s = String::new();\n    write!(s, \"v{}\", x).unwrap();\n    s\n}\n\n#[derive(Debug, Default)]\npub struct Counter {\n    total: i32,\n}\n\n"
        "impl Counter {\n    pub fn new() -> Self {\n        Counter { total: 0 }\n    }\n\n    pub fn add(&mut self, x: i32) {\n        self.total += x;\n    }\n\n    pub fn value(&self) -> i32 {\n        self.total\n    }\n\n    fn describe(&self) -> String {\n        label(self.total)\n    }\n}\n\n"
        "pub fn report(c: &Counter) -> String {\n    c.describe()\n}\n\n#[cfg(test)]\nmod tests {\n    use super::*;\n\n    #[test]\n    fn clamps() {\n        assert_eq!(clamp(500), LIMIT);\n        assert_eq!(super::clamp(3), 3);\n        let mut c = Counter::new();\n        c.add(2);\n        assert_eq!(report(&c), \"v2\");\n    }\n}\n"
    ),
    "src/geo.rs": "pub mod area;\n\npub use self::area::square;\n\npub fn double(x: i32) -> i32 {\n    crate::util::clamp(x * 2)\n}\n",
    "src/geo/area.rs": "use super::double;\nuse crate::util::LIMIT;\n\npub fn square(x: i32) -> i32 {\n    double(x) * x / 2 + LIMIT - LIMIT\n}\n\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn sq() {\n        assert_eq!(super::square(3), 9);\n        assert_eq!(crate::geo::area::square(2), 4);\n    }\n}\n",
    "src/main.rs": "use shapes_lib::geo::area::square;\nuse shapes_lib::util;\n\nfn main() {\n    println!(\"{} {}\", square(3), util::clamp(1000));\n    println!(\"{}\", shapes_lib::util::report(&shapes_lib::util::Counter::new()));\n}\n",
    "tests/integration.rs": "use shapes_lib::geo::{self, area};\nuse shapes_lib::util::*;\n\n#[test]\nfn works() {\n    assert_eq!(area::square(2), 4);\n    assert_eq!(geo::double(3), 6);\n    assert_eq!(clamp(1000), LIMIT);\n    let c = Counter::new();\n    assert_eq!(c.value(), 0);\n}\n",
}


def _cargo(root: Path) -> None:
    if _runs("cargo", "--version"):
        _check(root, "cargo", "test", "-q", CARGO_TERM_COLOR="never")


def test_rust_move_struct_takes_impls_and_opens_privacy(tmp_path):
    graph = _project(tmp_path, RUST)
    plan = plan_move(graph, _node(graph, "Counter", "src/util.rs"), "src/counter.rs", tmp_path)
    _apply(plan)
    files = _read(tmp_path)
    assert "pub mod counter;" in files["src/lib.rs"] and "use crate::counter::Counter;" in files["src/lib.rs"]
    assert "impl Counter" in files["src/counter.rs"] and "impl Counter" not in files["src/util.rs"]
    assert "use crate::util::label;" in files["src/counter.rs"] and "Write" not in files["src/counter.rs"]
    assert "pub(crate) fn label" in files["src/util.rs"] and "pub(crate) fn describe" in files["src/counter.rs"]
    assert "shapes_lib::counter::Counter::new()" in files["src/main.rs"]
    assert "use shapes_lib::counter::Counter;" in files["tests/integration.rs"]
    _cargo(tmp_path)


def test_rust_move_function_into_test_module_scope(tmp_path):
    graph = _project(tmp_path, RUST)
    _apply(plan_move(graph, _node(graph, "clamp()", "src/util.rs"), "src/geo.rs", tmp_path))
    files = _read(tmp_path)
    assert "use crate::util::LIMIT;" in files["src/geo.rs"]
    assert "use crate::geo::clamp;" not in files["src/util.rs"].split("mod tests")[0]
    assert "crate::geo::clamp(3)" in files["src/util.rs"]
    assert "use shapes_lib::util;" not in files["src/main.rs"]
    _cargo(tmp_path)


def test_rust_move_private_function_to_new_nested_module(tmp_path):
    graph = _project(tmp_path, RUST)
    _apply(plan_move(graph, _node(graph, "label()", "src/util.rs"), "src/text/fmt.rs", tmp_path))
    files = _read(tmp_path)
    assert files["src/text.rs"] == "pub(crate) mod fmt;\n" and "mod text;" in files["src/lib.rs"]
    assert files["src/text/fmt.rs"].startswith("use std::fmt::Write;") and "pub(crate) fn label" in files["src/text/fmt.rs"]
    assert "std::fmt::Write" not in files["src/util.rs"]
    _cargo(tmp_path)


def test_rust_move_file_renames_paths_and_local_names(tmp_path):
    graph = _project(tmp_path, RUST)
    _apply(plan_move_file(graph, "src/geo/area.rs", "src/geo/surface.rs", tmp_path))
    files = _read(tmp_path)
    assert "pub mod surface;" in files["src/geo.rs"] and "self::surface::square" in files["src/geo.rs"]
    assert "crate::geo::surface::square(2)" in files["src/geo/surface.rs"]
    assert "surface::square(2)" in files["tests/integration.rs"] and " as area" not in files["tests/integration.rs"]
    _cargo(tmp_path)


def test_rust_move_module_with_children_creates_parent(tmp_path):
    graph = _project(tmp_path, RUST)
    _apply(plan_move_file(graph, "src/geo.rs", "src/shapes/geometry.rs", tmp_path))
    files = _read(tmp_path)
    assert files["src/shapes.rs"] == "pub mod geometry;\n" and "pub mod shapes;" in files["src/lib.rs"]
    assert "src/shapes/geometry/area.rs" in files and "src/geo/area.rs" not in files
    assert "shapes_lib::shapes::geometry::area::square" in files["src/main.rs"]
    _cargo(tmp_path)


def test_rust_refuses_a_macro(tmp_path):
    graph = _project(tmp_path, {**RUST, "src/util.rs": RUST["src/util.rs"] + "\nmacro_rules! twice {\n    ($x:expr) => {\n        $x * 2\n    };\n}\n"})
    with pytest.raises(Unsupported):
        plan_move(graph, "src/util.rs::twice", "src/other.rs", tmp_path)
