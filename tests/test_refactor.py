"""Language-general references and renames (graphify.refactor)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from graphify.refactor import StaleFile, apply_rename, load_graph, plan_rename, references, rewrite, symbol_name


def _project(root: Path, files: dict[str, str]) -> dict:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    subprocess.run([sys.executable, "-m", "graphify", "update", "."], cwd=root, capture_output=True, check=True)
    return load_graph(root)


def _node(graph: dict, label: str, file: str) -> str:
    found = [n["id"] for n in graph["nodes"] if n.get("label") == label and n.get("source_file", "").endswith(file)]
    assert len(found) == 1, (label, file, [n.get("label") for n in graph["nodes"]])
    return found[0]


def _renamed(root: Path, graph: dict, label: str, file: str, new: str) -> dict[str, str]:
    plan = plan_rename(graph, _node(graph, label, file), new, root)
    assert not plan.conflicts, plan.conflicts
    apply_rename(plan)
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file() and "graphify-out" not in p.parts}


PYTHON = {
    "app/util.py": "def helper(x):\n    return x\n",
    "app/main.py": "from app.util import helper\nimport app.util as u\n\n\ndef run():\n    # helper is the identity\n    return helper(1) + u.helper(2)\n",
    "app/other.py": "def helper(x):\n    return -x\n\n\ndef go():\n    return helper(3), 'helper'\n",
    "app/extension.py": "class Setting:\n    def value(self):\n        return 1\n",
    "app/test_ext.py": "import app.extension as vis\n\n\ndef extension(fn):\n    return fn\n\n\ndef test_value():\n    setting = vis.Setting()\n    assert setting.value() == 1\n    return extension(test_value)\n",
}


def test_python_rename_follows_imports_and_skips_rivals(tmp_path):
    graph = _project(tmp_path, PYTHON)
    out = _renamed(tmp_path, graph, "helper()", "app/util.py", "assist")
    assert out["app/util.py"].startswith("def assist(x):")
    assert "from app.util import assist" in out["app/main.py"]
    assert "assist(1) + u.assist(2)" in out["app/main.py"]
    assert "# helper is the identity" in out["app/main.py"]  # a comment is a mention, not an edit
    assert out["app/other.py"] == PYTHON["app/other.py"]  # a rival definition keeps its name


def test_python_module_path_is_not_a_symbol(tmp_path):
    graph = _project(tmp_path, PYTHON)
    out = _renamed(tmp_path, graph, "extension()", "app/test_ext.py", "wrap")
    assert "import app.extension as vis" in out["app/test_ext.py"]
    assert "return wrap(test_value)" in out["app/test_ext.py"]


def test_python_method_follows_a_qualified_constructor(tmp_path):
    graph = _project(tmp_path, PYTHON)
    out = _renamed(tmp_path, graph, ".value()", "app/extension.py", "current")
    assert "def current(self):" in out["app/extension.py"]
    assert "setting.current() == 1" in out["app/test_ext.py"]


def test_plan_reports_mentions_and_refuses_stale_files(tmp_path):
    graph = _project(tmp_path, PYTHON)
    plan = plan_rename(graph, _node(graph, "helper()", "app/util.py"), "assist", tmp_path)
    assert any("helper is the identity" in m.text for m in plan.mentions)
    assert {Path(f).resolve().relative_to(tmp_path.resolve()).as_posix() for f in plan.files} == {"app/util.py", "app/main.py"}
    (tmp_path / "app/main.py").write_text("changed = 1\n", encoding="utf-8")
    with pytest.raises(StaleFile):
        rewrite(plan)


def test_invalid_name_is_a_conflict(tmp_path):
    graph = _project(tmp_path, PYTHON)
    plan = plan_rename(graph, _node(graph, "helper()", "app/util.py"), "not valid", tmp_path)
    assert plan.conflicts
    with pytest.raises(ValueError):
        apply_rename(plan)


TYPESCRIPT = {
    "src/util.ts": "export function formatName(n: string): string {\n  return n.trim();\n}\n",
    "src/main.ts": "import { formatName } from './util';\nimport * as u from './util';\n\nexport const a = formatName(' x ') + u.formatName(' y ');\n",
    "src/local.ts": "function formatName(n: string): string {\n  return n;\n}\nexport const b = formatName('z');\n",
}


def test_typescript_rename_covers_named_and_namespace_imports(tmp_path):
    graph = _project(tmp_path, TYPESCRIPT)
    out = _renamed(tmp_path, graph, "formatName()", "src/util.ts", "displayName")
    assert "export function displayName(" in out["src/util.ts"]
    assert "import { displayName } from './util';" in out["src/main.ts"]
    assert "displayName(' x ') + u.displayName(' y ')" in out["src/main.ts"]
    assert out["src/local.ts"] == TYPESCRIPT["src/local.ts"]


RUST = {
    "src/lib.rs": "pub mod util;\nuse crate::util::scale;\n\npub fn run() -> i32 {\n    scale(2) + util::scale(3)\n}\n",
    "src/util.rs": "pub fn scale(x: i32) -> i32 {\n    x * 2\n}\n\npub struct Guard;\n\nimpl Drop for Guard {\n    fn drop(&mut self) {}\n}\n",
}


def test_rust_rename_covers_use_and_paths(tmp_path):
    graph = _project(tmp_path, RUST)
    out = _renamed(tmp_path, graph, "scale()", "src/util.rs", "grow")
    assert "pub fn grow(x: i32)" in out["src/util.rs"]
    assert "use crate::util::grow;" in out["src/lib.rs"]
    assert "grow(2) + util::grow(3)" in out["src/lib.rs"]


def test_rust_rename_covers_full_paths_and_keeps_methods(tmp_path):
    graph = _project(tmp_path, {
        "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n',
        "src/util.rs": "pub fn sum(x: u32) -> u32 {\n    x\n}\n",
        "src/packet.rs": "pub struct Packet;\n\nimpl Packet {\n    pub fn sum(&self) -> u32 {\n        crate::util::sum(1)\n    }\n}\n",
        "src/main.rs": "mod packet;\nmod util;\n\nfn main() {\n    let p = packet::Packet;\n    println!(\"{}\", p.sum() + crate::util::sum(2));\n}\n",
    })
    out = _renamed(tmp_path, graph, "sum()", "src/util.rs", "total")
    assert "crate::util::total(1)" in out["src/packet.rs"] and "pub fn sum(&self)" in out["src/packet.rs"]
    assert "p.sum() + crate::util::total(2)" in out["src/main.rs"]


def test_rust_trait_method_is_a_conflict(tmp_path):
    graph = _project(tmp_path, RUST)
    plan = plan_rename(graph, _node(graph, ".drop()", "src/util.rs"), "release", tmp_path)
    assert any("Drop" in c for c in plan.conflicts)


GO = {
    "go.mod": "module example.com/app\n\ngo 1.22\n",
    "shop/cart.go": "package shop\n\ntype Cart struct{ n int }\n\nfunc NewCart() *Cart { return &Cart{} }\n\nfunc (c *Cart) Total() int { return c.n }\n",
    "main.go": "package main\n\nimport \"example.com/app/shop\"\n\nfunc main() {\n\tc := shop.NewCart()\n\t_ = c.Total()\n}\n",
}


def test_go_rename_covers_package_selectors_and_methods(tmp_path):
    graph = _project(tmp_path, GO)
    out = _renamed(tmp_path, graph, ".Total()", "shop/cart.go", "Sum")
    assert "func (c *Cart) Sum() int" in out["shop/cart.go"]
    assert "_ = c.Sum()" in out["main.go"]
    graph = _project(tmp_path, {k: out[k] for k in ("shop/cart.go", "main.go")})
    out = _renamed(tmp_path, graph, "NewCart()", "shop/cart.go", "MakeCart")
    assert "func MakeCart() *Cart" in out["shop/cart.go"]
    assert "c := shop.MakeCart()" in out["main.go"]


def test_go_type_rename_covers_its_package_and_skips_rivals(tmp_path):
    graph = _project(tmp_path, {
        "go.mod": "module example.com/app\n\ngo 1.22\n",
        "shop/order.go": "package shop\n\ntype Order struct{ ID string }\n",
        "shop/store.go": "package shop\n\nvar cache = map[string]*Order{}\n\nfunc Find(id string) Order { return *cache[id] }\n",
        "audit/audit.go": "package audit\n\ntype Order struct{ Ref string }\n",
        "bill/bill.go": (
            "package bill\n\nimport (\n\t\"example.com/app/audit\"\n\t\"example.com/app/shop\"\n)\n\n"
            "type Receipt struct{ Order shop.Order }\n\n"
            "func Make() (Receipt, []shop.Order, audit.Order) {\n"
            "\treturn Receipt{Order: shop.Order{ID: \"a\"}}, []shop.Order{{ID: \"b\"}}, audit.Order{Ref: \"c\"}\n}\n"
        ),
    })
    out = _renamed(tmp_path, graph, "Order", "shop/order.go", "Purchase")
    assert "map[string]*Purchase{}" in out["shop/store.go"] and "func Find(id string) Purchase" in out["shop/store.go"]
    bill = out["bill/bill.go"]
    assert "type Receipt struct{ Order shop.Purchase }" in bill  # the field keeps its name
    assert 'Receipt{Order: shop.Purchase{ID: "a"}}, []shop.Purchase{{ID: "b"}}, audit.Order{Ref: "c"}' in bill
    assert out["audit/audit.go"] == "package audit\n\ntype Order struct{ Ref string }\n"


JAVA = {
    "src/app/Greeter.java": "package app;\n\npublic class Greeter {\n    public String greetUser(String n) { return \"hi \" + n; }\n}\n",
    "src/app/Main.java": "package app;\n\npublic class Main {\n    public static void main(String[] a) {\n        Greeter g = new Greeter();\n        System.out.println(g.greetUser(\"x\"));\n    }\n}\n",
}


def test_java_rename_covers_typed_receivers(tmp_path):
    graph = _project(tmp_path, JAVA)
    out = _renamed(tmp_path, graph, ".greetUser()", "src/app/Greeter.java", "welcome")
    assert "public String welcome(String n)" in out["src/app/Greeter.java"]
    assert "g.welcome(\"x\")" in out["src/app/Main.java"]


def test_references_list_every_site(tmp_path):
    graph = _project(tmp_path, PYTHON)
    occs = references(graph, _node(graph, "helper()", "app/util.py"), tmp_path)
    assert sorted((Path(o.file).name, o.role) for o in occs) == [
        ("main.py", "import"), ("main.py", "member"), ("main.py", "use"), ("util.py", "definition")]


def test_symbol_name():
    assert symbol_name(".save()") == "save"
    assert symbol_name("Foo.bar()") == "bar"
    assert symbol_name("Cart") == "Cart"
