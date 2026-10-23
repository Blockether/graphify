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


JAVA = {
    'src/main/java/a/Util.java': 'package a;\n\nimport java.util.List;\n\n/** Doubles. */\nclass Util {\n    static int twice(int x) { return Helper.k() * x + List.of(1).size(); }\n}\n',
    'src/main/java/a/Helper.java': 'package a;\n\nclass Helper { static int k() { return 2; } }\n',
    'src/main/java/a/Same.java': 'package a;\n\npublic class Same { int f() { return Util.twice(1); } }\n',
    'src/main/java/b/Main.java': 'package b;\n\nimport a.*;\n\npublic class Main { int run() { return new Same().hashCode(); } }\n',
    'src/test/java/a/UtilTest.java': 'package a;\n\nclass UtilTest { int t() { return Util.twice(3); } }\n',
}


KOTLIN = {
    'src/main/kotlin/a/Util.kt': 'package a\n\nimport kotlin.math.abs\n\n/** Doubles. */\nfun twice(x: Int) = abs(x) * factor()\n\nfun keep() = twice(1)\n',
    'src/main/kotlin/a/Factor.kt': 'package a\n\nfun factor() = 2\n',
    'src/main/kotlin/b/Main.kt': 'package b\n\nimport a.twice\nimport a.*\n\nfun run() = twice(2) + a.twice(3) + keep()\n',
}


CSHARP = {
    'App/A/Util.cs': 'using System;\n\nnamespace App.A;\n\n/// <summary>Doubles.</summary>\npublic static class Util\n{\n    public static int Twice(int x) => Math.Abs(x) * Factor.K;\n}\n',
    'App/A/Factor.cs': 'namespace App.A;\n\npublic static class Factor { public const int K = 2; }\n',
    'App/A/Same.cs': 'namespace App.A;\n\npublic class Same { int F() => Util.Twice(1); }\n',
    'App/B/Main.cs': 'using App.A;\n\nnamespace App.B\n{\n    public class Main { int Run() => Util.Twice(2) + App.A.Util.Twice(3); }\n}\n',
}


PHP = {
    'composer.json': '{"autoload": {"psr-4": {"App\\\\": "src/"}}}\n',
    'src/A/Util.php': '<?php\n\nnamespace App\\A;\n\nclass Util\n{\n    public static function twice(int $x): int\n    {\n        return Helper::factor() * $x;\n    }\n}\n\nclass Helper\n{\n    public static function factor(): int\n    {\n        return 2;\n    }\n}\n',
    'src/A/Same.php': '<?php\n\nnamespace App\\A;\n\nclass Same\n{\n    public function run(): int\n    {\n        return Util::twice(3);\n    }\n}\n',
    'src/B/Main.php': '<?php\n\nnamespace App\\B;\n\nuse App\\A\\Util;\n\nclass Main\n{\n    public function run(): int\n    {\n        return Util::twice(4) + \\App\\A\\Util::twice(1);\n    }\n}\n',
}


SWIFT = {
    'Package.swift': '// swift-tools-version:5.9\nimport PackageDescription\nlet package = Package(name: "P", targets: [.target(name: "Core"), .target(name: "Extra", dependencies: ["Core"]), .executableTarget(name: "App", dependencies: ["Core", "Extra"])])\n',
    'Sources/Core/Util.swift': 'struct Helper {\n    static func factor() -> Int { 2 }\n}\n\npublic func twice(_ x: Int) -> Int {\n    return Helper.factor() * x\n}\n',
    'Sources/Core/Same.swift': 'public func runSame() -> Int {\n    return twice(3)\n}\n',
    'Sources/Extra/Other.swift': 'import Core\n\npublic func other() -> Int { 1 }\n\npublic func keep() -> Int { other() + twice(1) }\n',
    'Sources/App/main.swift': 'import Core\nimport Extra\n\nprint(runSame() + other() + keep())\n',
}


SCALA = {
    'src/main/scala/a/Util.scala': 'package a\n\nimport scala.collection.mutable\n\nobject Util {\n  def twice(x: Int): Int = Helper.factor * x\n  def buf = mutable.ListBuffer(1)\n}\n\nobject Helper {\n  def factor: Int = 2\n}\n',
    'src/main/scala/a/Same.scala': 'package a\n\nobject Same {\n  def run: Int = Util.twice(3)\n}\n',
    'src/main/scala/b/Main.scala': 'package b\n\nimport a.Util\n\nobject Main {\n  def run: Int = Util.twice(4) + a.Util.twice(1)\n}\n',
}


C_PROJECT = {
    'CMakeLists.txt': 'add_executable(app src/main.c src/a.c)\n',
    'src/a.h': '#ifndef A_H\n#define A_H\n\nint helper(int x);\n\n/* Double a number. */\nint twice(int x);\n\nint quad(int x);\n\n#endif\n',
    'src/a.c': '#include <stdio.h>\n#include "a.h"\n\nint helper(int x) { return x + 0; }\n\n/* Double it. */\nint twice(int x) {\n    printf("t\\n");\n    return helper(x) * 2;\n}\n\nint quad(int x) { return twice(twice(x)); }\n',
    'src/main.c': '#include "a.h"\n\nint main(void) { return twice(1) + quad(1) - 6; }\n',
}


CPP = {
    'CMakeLists.txt': 'add_executable(app\n  src/main.cpp\n  src/a.cpp\n)\ntarget_include_directories(app PRIVATE include)\n',
    'include/proj/a.hpp': '#pragma once\n\nnamespace proj {\n\nint helper(int x);\nint twice(int x);\n\n}  // namespace proj\n',
    'src/a.cpp': '#include "proj/a.hpp"\n\n#include <vector>\n\nnamespace proj {\n\nint helper(int x) { return x; }\n\nint twice(int x) {\n    std::vector<int> v{helper(x)};\n    return v[0] * 2;\n}\n\n}  // namespace proj\n',
    'src/main.cpp': '#include "proj/a.hpp"\n\nint main() { return proj::twice(1) - 2; }\n',
}


OBJC = {
    'src/A.h': '#import <Foundation/Foundation.h>\n\nint twice(int x);\n\n@interface A : NSObject\n- (int)run;\n@end\n',
    'src/A.m': '#import "A.h"\n\nint twice(int x) { return x * 2; }\n\n@implementation A\n- (int)run { return twice(2); }\n@end\n',
    'src/main.m': '#import "A.h"\n\nint main(void) { @autoreleasepool { return [[A new] run] + twice(1) - 6; } }\n',
}


RUBY = {
    'lib/app.rb': "require_relative 'app/util'\nrequire_relative 'app/same'\n",
    'lib/app/util.rb': "require 'json'\n\nmodule App\n  FACTOR = 2\nend\n\n# Doubles.\ndef twice(x)\n  JSON.generate([x]).size * 0 + x * App::FACTOR\nend\n\ndef quad(x) = twice(twice(x))\n",
    'lib/app/same.rb': 'def run_same = quad(1)\n',
    'bin/main.rb': "require_relative '../lib/app'\n\nputs run_same + twice(1)\n",
    'test/app/util_test.rb': "require_relative '../../lib/app/util'\n\nraise 'bad' unless twice(2) == 4\n",
}


BASH = {
    'bin/main.sh': '#!/usr/bin/env bash\nset -euo pipefail\nsource "$(dirname "${BASH_SOURCE[0]}")/../lib/util.sh"\nsource "$(dirname "${BASH_SOURCE[0]}")/../lib/same.sh"\n\necho "$(twice 2) $(run_same)"\n',
    'lib/util.sh': '# Utilities.\n\nfactor() { echo 2; }\n\n# Double a number.\ntwice() {\n  echo $(( $1 * $(factor) ))\n}\n',
    'lib/same.sh': 'run_same() { twice 3; }\n',
}


POWERSHELL = {
    'main.ps1': '. "$PSScriptRoot/lib/util.ps1"\n\nWrite-Output (Get-Twice 2)\n',
    'lib/util.ps1': 'function Get-Factor { 2 }\n\n# Double a number.\nfunction Get-Twice($x) {\n    $x * (Get-Factor)\n}\n',
}


JULIA = {
    'src/App.jl': 'module App\n\ninclude("util.jl")\ninclude("same.jl")\n\nend\n',
    'src/util.jl': 'const FACTOR = 2\n\n"""Double a number."""\nfunction twice(x)\n    x * FACTOR\nend\n\nquad(x) = twice(twice(x))\n',
    'src/same.jl': 'run_same() = quad(1)\n',
}


VERILOG = {
    'rtl/top.sv': '`include "defs.svh"\n\nmodule top(input logic a, output logic y);\n  inv u(.a(a), .y(y));\nendmodule\n\nmodule inv(input logic a, output logic y);\n  assign y = ~a;\nendmodule\n',
    'rtl/defs.svh': '`define W 8\n',
    'files.f': 'rtl/top.sv\n',
}


LUA = {
    'main.lua': 'local util = require("app.util")\n\nprint(util.twice(2), util.quad(1))\n',
    'app/util.lua': 'local M = {}\n\nfunction M.factor()\n  return 2\nend\n\n-- Double a number.\nfunction M.twice(x)\n  return x * M.factor()\nend\n\nfunction M.quad(x)\n  return M.twice(M.twice(x))\nend\n\nreturn M\n',
}


LUA_FREE = {
    'main.lua': 'local util = require("app.util")\n\nprint(util.twice(2), util.quad(1))\n',
    'app/util.lua': 'local M = {}\n\n-- Double a number.\nfunction M.twice(x)\n  return x * 2\nend\n\nfunction M.quad(x)\n  return M.twice(M.twice(x))\nend\n\nreturn M\n',
}


ZIG = {
    'src/main.zig': 'const std = @import("std");\nconst util = @import("util.zig");\n\npub fn main() void {\n    std.debug.print("{d} {d}\\n", .{ util.twice(2), util.quad(1) });\n}\n',
    'src/util.zig': 'const std = @import("std");\n\nfn factor() i32 {\n    return 2;\n}\n\n/// Double a number.\npub fn twice(x: i32) i32 {\n    std.debug.assert(x >= 0);\n    return x * factor();\n}\n\npub fn quad(x: i32) i32 {\n    return twice(twice(x));\n}\n',
}


ELIXIR = {
    'lib/a/util.ex': 'defmodule A.Util do\n  alias B.Helper\n\n  @doc "Doubles."\n  def twice(x), do: Helper.factor() * x + offset()\n\n  def offset, do: 0\n\n  def quad(x), do: twice(twice(x))\nend\n',
    'lib/b/helper.ex': 'defmodule B.Helper do\n  def factor, do: 2\nend\n',
    'lib/c/main.ex': 'defmodule C.Main do\n  alias A.Util\n\n  def run, do: Util.twice(1) + A.Util.quad(1) + Enum.sum(Enum.map([1], &Util.twice/1))\nend\n',
    'test/a/util_test.exs': 'defmodule A.UtilTest do\n  use ExUnit.Case\n  alias A.Util\n\n  test "twice" do\n    assert Util.twice(2) == 4\n  end\nend\n',
}


FORTRAN = {
    'src/util.f90': 'module util\n  use iso_fortran_env, only: int32, real64\n  implicit none\n  private\n  public :: twice, quad\n  integer(int32), parameter :: factor = 2\ncontains\n  ! Double a number.\n  integer(int32) function twice(x)\n    integer(int32), intent(in) :: x\n    twice = x * factor\n  end function twice\n\n  integer(int32) function quad(x)\n    integer(int32), intent(in) :: x\n    quad = x * 4\n  end function quad\nend module util\n',
    'src/main.f90': 'program main\n  use util, only: twice, quad\n  implicit none\n  print *, twice(1), quad(1)\nend program main\n',
    'CMakeLists.txt': 'add_executable(app src/util.f90 src/main.f90)\n',
}


# Every other tree-sitter grammar: module-named (graphify.named_moves), path-loaded
# (graphify.path_moves) and module-block (graphify.block_moves) languages.


def _moved(tmp_path, files, file, name, to):
    graph = _project(tmp_path, files)
    _apply(plan_move(graph, _node(graph, name, file), to, tmp_path))
    return _read(tmp_path)


def _parses(root: Path) -> None:
    from graphify.move_kit import parse
    from graphify.refactor import GRAMMARS

    for p in root.rglob("*"):
        if p.is_file() and p.suffix in GRAMMARS and "graphify-out" not in p.parts and p.suffix != ".f":
            assert not parse(p, p.read_bytes()).root_node.has_error, p


def test_java_move_class_to_other_package(tmp_path):
    files = _moved(tmp_path, JAVA, "src/main/java/a/Util.java", "Util", "src/main/java/c/Util.java")
    assert files["src/main/java/c/Util.java"].startswith("package c;")
    assert "public class Util" in files["src/main/java/c/Util.java"]
    assert "import c.Util;" in files["src/main/java/a/Same.java"]
    assert "src/main/java/a/Util.java" not in files
    if _runs("javac", "-version"):
        _check(tmp_path, "javac", "-d", str(tmp_path / "out"), *[str(p) for p in tmp_path.rglob("*.java")])


def test_java_move_file_moves_its_test(tmp_path):
    graph = _project(tmp_path, JAVA)
    _apply(plan_move_file(graph, "src/main/java/a/Util.java", "src/main/java/c/Util.java", tmp_path))
    files = _read(tmp_path)
    assert files["src/test/java/c/UtilTest.java"].startswith("package c;")
    if _runs("javac", "-version"):
        _check(tmp_path, "javac", "-d", str(tmp_path / "out"), *[str(p) for p in tmp_path.rglob("*.java")])


def test_kotlin_move_function_rewrites_imports_and_qualified_calls(tmp_path):
    files = _moved(tmp_path, KOTLIN, "src/main/kotlin/a/Util.kt", "twice", "src/main/kotlin/c/Math.kt")
    main = files["src/main/kotlin/b/Main.kt"]
    assert "import c.twice" in main and "c.twice(3)" in main and "import a.twice" not in main
    assert "import a.factor" in files["src/main/kotlin/c/Math.kt"]
    assert "import c.twice" in files["src/main/kotlin/a/Util.kt"]


def test_csharp_move_class_to_other_namespace(tmp_path):
    files = _moved(tmp_path, CSHARP, "App/A/Util.cs", "Util", "App/C/Util.cs")
    assert "namespace App.C;" in files["App/C/Util.cs"] and "using App.A;" in files["App/C/Util.cs"]
    assert "using App.C;" in files["App/B/Main.cs"] and "App.C.Util" in files["App/B/Main.cs"]


def test_php_move_class_follows_psr4(tmp_path):
    files = _moved(tmp_path, PHP, "src/A/Util.php", "Util", "src/C/Util.php")
    assert "namespace App\\C;" in files["src/C/Util.php"] and "use App\\A\\Helper;" in files["src/C/Util.php"]
    assert "use App\\C\\Util;" in files["src/B/Main.php"] and "\\App\\C\\Util::twice(1)" in files["src/B/Main.php"]
    assert "use App\\C\\Util;" in files["src/A/Same.php"]


def test_swift_refuses_a_module_cycle(tmp_path):
    graph = _project(tmp_path, {**SWIFT, "Sources/Core/Util.swift": "func twice(_ x: Int) -> Int { x * 2 }\n",
                                "Sources/Core/Same.swift": "public func runSame() -> Int { twice(3) }\n"})
    plan = plan_move(graph, _node(graph, "twice", "Sources/Core/Util.swift"), "Sources/Extra/Math.swift", tmp_path)
    assert any("cycle" in c for c in plan.conflicts), plan.conflicts


def test_swift_move_function_to_a_dependency(tmp_path):
    files = _moved(tmp_path, SWIFT, "Sources/Extra/Other.swift", "other", "Sources/Core/Other.swift")
    assert files["Sources/Core/Other.swift"].startswith("public func other()")
    if _runs("swift", "--version") and sys.platform == "darwin":
        _check(tmp_path, "swift", "build")


def test_scala_move_object_to_other_package(tmp_path):
    files = _moved(tmp_path, SCALA, "src/main/scala/a/Util.scala", "Util", "src/main/scala/c/Util.scala")
    assert files["src/main/scala/c/Util.scala"].startswith("package c\n")
    assert "import a.Helper" in files["src/main/scala/c/Util.scala"]
    assert "c.Util.twice(1)" in files["src/main/scala/b/Main.scala"]
    _parses(tmp_path)


def test_c_move_function_takes_its_header_declaration(tmp_path):
    files = _moved(tmp_path, C_PROJECT, "src/a.c", "twice", "src/b.c")
    assert files["src/b.h"].startswith("#ifndef B_H\n#define B_H") and "int twice(int x);" in files["src/b.h"]
    assert "int twice" not in files["src/a.h"]
    assert files["src/b.c"].count('#include "b.h"') == 1 and '#include "a.h"' in files["src/b.c"]
    assert '#include "b.h"' in files["src/main.c"] and '#include "b.h"' in files["src/a.c"]
    assert "src/a.c src/b.c" in files["CMakeLists.txt"]
    if _runs("cc", "--version"):
        _check(tmp_path / "src", "cc", "-Wall", "-o", str(tmp_path / "app"), "main.c", "a.c", "b.c")


def test_cpp_move_keeps_namespace_and_include_root(tmp_path):
    files = _moved(tmp_path, CPP, "src/a.cpp", "twice", "src/b.cpp")
    assert "namespace proj {" in files["include/proj/b.hpp"] and files["include/proj/b.hpp"].startswith("#pragma once")
    assert '#include "proj/b.hpp"' in files["src/main.cpp"]
    assert files["src/b.cpp"].count('#include "proj/b.hpp"') == 1 and "namespace proj {" in files["src/b.cpp"]
    assert "  src/b.cpp\n" in files["CMakeLists.txt"]
    if _runs("c++", "--version"):
        _check(tmp_path, "c++", "-std=c++17", "-Wall", "-Iinclude", "-o", str(tmp_path / "app"), "src/main.cpp", "src/a.cpp", "src/b.cpp")


def test_cpp_move_header_rewrites_include_root_paths(tmp_path):
    graph = _project(tmp_path, CPP)
    _apply(plan_move_file(graph, "include/proj/a.hpp", "include/proj/core/a.hpp", tmp_path))
    files = _read(tmp_path)
    assert '#include "proj/core/a.hpp"' in files["src/main.cpp"] and '#include "proj/core/a.hpp"' in files["src/a.cpp"]


def test_objc_move_function(tmp_path):
    files = _moved(tmp_path, OBJC, "src/A.m", "twice", "src/B.m")
    assert '#import "B.h"' in files["src/B.m"] and '#import "B.h"' in files["src/main.m"]
    if sys.platform == "darwin" and _runs("clang", "--version"):
        _check(tmp_path, "clang", "-fobjc-arc", "-framework", "Foundation", "-o", str(tmp_path / "app"), "src/A.m", "src/B.m", "src/main.m")


def test_ruby_move_method_requires_the_new_file(tmp_path):
    files = _moved(tmp_path, RUBY, "lib/app/util.rb", "twice", "lib/app/math.rb")
    assert files["lib/app/math.rb"].startswith("require 'json'\nrequire_relative 'util'\n")
    assert "require_relative 'math'" in files["lib/app/util.rb"] and "require 'math'" not in files["lib/app/util.rb"]
    assert "math" not in files["bin/main.rb"]
    if _runs("ruby", "--version"):
        _check(tmp_path, "ruby", "bin/main.rb")


def test_ruby_move_file_moves_its_test(tmp_path):
    graph = _project(tmp_path, RUBY)
    _apply(plan_move_file(graph, "lib/app/util.rb", "lib/app/core/util.rb", tmp_path))
    files = _read(tmp_path)
    assert "require_relative 'app/core/util'" in files["lib/app.rb"]
    assert "require_relative '../../../lib/app/core/util'" in files["test/app/core/util_test.rb"]
    if _runs("ruby", "--version"):
        _check(tmp_path, "ruby", "test/app/core/util_test.rb")


def test_bash_move_function_sources_the_new_file(tmp_path):
    files = _moved(tmp_path, BASH, "lib/util.sh", "twice", "lib/math/twice.sh")
    assert 'source "$(dirname "${BASH_SOURCE[0]}")/../lib/math/twice.sh"' in files["bin/main.sh"]
    if _runs("bash", "--version"):
        _check(tmp_path, "bash", "bin/main.sh")


def test_powershell_move_function_dot_sources_the_new_file(tmp_path):
    files = _moved(tmp_path, POWERSHELL, "lib/util.ps1", "Get-Twice", "lib/math.ps1")
    assert '. "$PSScriptRoot/lib/math.ps1"' in files["main.ps1"]


def test_julia_move_function_includes_the_new_file(tmp_path):
    files = _moved(tmp_path, JULIA, "src/util.jl", "twice", "src/math.jl")
    assert 'include("util.jl")\ninclude("math.jl")' in files["src/App.jl"]
    assert files["src/math.jl"].startswith('"""Double a number."""')
    _parses(tmp_path)


def test_verilog_move_module_extends_the_file_list(tmp_path):
    files = _moved(tmp_path, VERILOG, "rtl/top.sv", "inv", "rtl/inv.sv")
    assert files["files.f"] == "rtl/top.sv\nrtl/inv.sv\n"
    assert "`include" not in files["rtl/top.sv"].replace('`include "defs.svh"', "")


def test_lua_move_function_rebinds_callers(tmp_path):
    files = _moved(tmp_path, LUA_FREE, "app/util.lua", "twice", "app/math.lua")
    assert 'local app_math = require("app.math")' in files["main.lua"] and "app_math.twice(2)" in files["main.lua"]
    assert files["app/math.lua"].endswith("return M\n")


def test_lua_refuses_a_require_cycle(tmp_path):
    graph = _project(tmp_path, LUA)
    plan = plan_move(graph, _node(graph, "twice", "app/util.lua"), "app/math.lua", tmp_path)
    assert any("cycle" in c for c in plan.conflicts), plan.conflicts


def test_zig_move_function_imports_and_publishes(tmp_path):
    files = _moved(tmp_path, ZIG, "src/util.zig", "twice", "src/math.zig")
    assert "util.factor()" in files["src/math.zig"] and "pub fn factor" in files["src/util.zig"]
    assert "math.twice(2)" in files["src/main.zig"] and "math.twice(math.twice(x))" in files["src/util.zig"]
    if _runs("zig", "version"):
        _check(tmp_path, "zig", "run", "src/main.zig")


def test_elixir_move_function_rewrites_aliases(tmp_path):
    files = _moved(tmp_path, ELIXIR, "lib/a/util.ex", "twice", "lib/a/math.ex")
    assert files["lib/a/math.ex"].startswith("defmodule A.Math do\n  alias B.Helper\n\n  @doc")
    assert "A.Util.offset()" in files["lib/a/math.ex"]
    assert "alias A.Math" in files["lib/c/main.ex"] and "&Math.twice/1" in files["lib/c/main.ex"]
    assert "A.Math.twice(A.Math.twice(x))" in files["lib/a/util.ex"]


def test_elixir_move_file_renames_the_module_and_its_test(tmp_path):
    graph = _project(tmp_path, ELIXIR)
    _apply(plan_move_file(graph, "lib/a/util.ex", "lib/core/util.ex", tmp_path))
    files = _read(tmp_path)
    assert files["lib/core/util.ex"].startswith("defmodule Core.Util do")
    assert files["test/core/util_test.exs"].startswith("defmodule Core.UtilTest do")
    assert "alias Core.Util" in files["lib/c/main.ex"]


def test_fortran_move_function_rewrites_use_lists(tmp_path):
    files = _moved(tmp_path, FORTRAN, "src/util.f90", "twice", "src/math.f90")
    assert "use util, only: quad\n  use math, only: twice" in files["src/main.f90"]
    assert "use util, only: factor" in files["src/math.f90"] and "public :: twice" in files["src/math.f90"]
    assert "public :: factor" in files["src/util.f90"]
    assert "src/util.f90 src/math.f90" in files["CMakeLists.txt"]
