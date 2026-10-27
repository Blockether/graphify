"""The absolute Python module map serves only Python imports.

An incremental update that re-extracts no Python file must not probe every
Python file of the resolution context: on a large repository that probe cost
more than the extraction itself.
"""
from __future__ import annotations

import graphify.extract as extract_mod
from graphify.extract import extract


def _project(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "b.py").write_text("from pkg.a import alpha\n\ndef beta():\n    return alpha()\n")
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")
    return [{"id": "pkg_a", "source_file": "pkg/a.py"}, {"id": "b", "source_file": "b.py"}]


def _counting(monkeypatch):
    calls: list[int] = []
    real = extract_mod._python_absolute_import_alias_files
    monkeypatch.setattr(
        extract_mod,
        "_python_absolute_import_alias_files",
        lambda paths, root: calls.append(len(paths)) or real(paths, root),
    )
    return calls


def test_a_non_python_update_skips_the_python_module_map(tmp_path, monkeypatch):
    context = _project(tmp_path)
    calls = _counting(monkeypatch)
    result = extract([tmp_path / "main.go"], root=tmp_path, parallel=False, resolution_context_nodes=context)
    assert calls == []
    assert any(n.get("label") == "main()" for n in result["nodes"])


def test_a_python_update_still_resolves_absolute_imports(tmp_path, monkeypatch):
    context = _project(tmp_path)
    calls = _counting(monkeypatch)
    result = extract(
        [tmp_path / "b.py", tmp_path / "pkg" / "a.py"], root=tmp_path, parallel=False, resolution_context_nodes=context
    )
    assert calls
    ids = {n["id"]: n for n in result["nodes"]}
    alpha = next(i for i, n in ids.items() if n.get("label") == "alpha()")
    beta = next(i for i, n in ids.items() if n.get("label") == "beta()")
    assert any(e["source"] == beta and e["target"] == alpha and e["relation"] == "calls" for e in result["edges"])
