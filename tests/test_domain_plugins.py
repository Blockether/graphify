"""Tests for the domain plugin contract and loader (graphify/domain/plugin.py).

A small custom plugin shows that the model, the checks and the report are
generic: nothing in them depends on edd-core.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_clojure")

from graphify.domain import analyze, write_outputs
from graphify.domain import plugin as plugin_mod
from graphify.domain.plugin import PluginError, discover, load_spec

ROUTES_PLUGIN = textwrap.dedent('''
    from graphify.domain import edn
    from graphify.domain.model import Element, KindSpec, Ref, Relation
    from graphify.domain.plugin import DomainPlugin


    class RoutesPlugin(DomainPlugin):
        """Reads (defroute :name handler :calls [:other ...])."""

        name = "routes"
        description = "toy routes"
        kinds = {"route": KindSpec("Route", "Routes", "stadium", entry=True)}

        def detect(self, corpus):
            return any("defroute" in f.path.read_text() for f in corpus.files)

        def extract(self, corpus, model):
            for sf in corpus.files:
                for top in sf.forms:
                    if top.kind == edn.LIST and top.head is not None and top.head.is_sym("defroute"):
                        name = top.items[1].name
                        ref = Ref("route", "web", name)
                        model.add(Element(ref, sf.rel, top.line, plugin=self.name))
                        calls = top.items[top.items.index(next(i for i in top.items if i.is_kw("calls"))) + 1]
                        for target in calls.items:
                            model.relate(Relation(ref, Ref("route", "web", target.name), "calls",
                                                  sf.rel, target.line, True, {}, self.name))


    plugin = RoutesPlugin
''')


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/web.clj").write_text(
        "(ns web)\n(defroute :home h :calls [:about])\n(defroute :about h :calls [:missing])\n", encoding="utf-8")
    return tmp_path


def test_file_spec_runs_a_custom_plugin_end_to_end(tmp_path):
    root = _project(tmp_path)
    (tmp_path / "routes_plugin.py").write_text(ROUTES_PLUGIN, encoding="utf-8")
    res = analyze(root, specs=[str(tmp_path / "routes_plugin.py")])
    model = res.model
    assert model.plugins == ["routes"]
    assert model.counts() == {"route": 2}
    (finding,) = model.findings
    assert finding.code == "missing-route" and finding.severity == "error"
    assert "missing" in finding.message
    written = write_outputs(model, tmp_path / "out")
    md = written["markdown"].read_text()
    assert "#### Routes" in md and "```mermaid" in md and "missing-route" in md
    assert json.loads(written["json"].read_text())["counts"] == {"route": 2}


def test_project_plugin_dir_is_discovered_and_detected(tmp_path):
    root = _project(tmp_path)
    plugin_dir = root / ".graphify" / "plugins"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "routes.py").write_text(ROUTES_PLUGIN, encoding="utf-8")
    names = [p.name for p in discover(root)]
    assert "routes" in names and "edd-core" in names
    res = analyze(root)
    assert [p.name for p in res.plugins] == ["routes"]  # edd-core does not detect edd.core here


def test_named_spec_finds_project_and_env_plugins(tmp_path, monkeypatch):
    # `--plugin <name>` must also reach plugins from .graphify/plugins/ and GRAPHIFY_PLUGINS.
    root = _project(tmp_path)
    plugin_dir = root / ".graphify" / "plugins"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "routes.py").write_text(ROUTES_PLUGIN, encoding="utf-8")
    assert [p.name for p in discover(root, ["routes"])] == ["routes"]
    assert analyze(root, specs=["routes"]).model.counts() == {"route": 2}
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "web_routes.py").write_text(ROUTES_PLUGIN.replace('name = "routes"', 'name = "web"'), encoding="utf-8")
    monkeypatch.setenv(plugin_mod.ENV_VAR, str(other))
    assert [p.name for p in discover(root, ["web"])] == ["web"]
    with pytest.raises(PluginError, match="no-such-plugin"):
        discover(root, ["no-such-plugin"])

def test_env_var_and_module_specs(tmp_path, monkeypatch):
    (tmp_path / "my_routes.py").write_text(ROUTES_PLUGIN, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert [p.name for p in load_spec("my_routes:RoutesPlugin")] == ["routes"]
    assert [p.name for p in load_spec("my_routes")] == ["routes"]
    monkeypatch.setenv(plugin_mod.ENV_VAR, str(tmp_path / "my_routes.py"))
    assert "routes" in [p.name for p in discover(tmp_path)]


def test_entry_point_plugins(monkeypatch):
    class FakeEntryPoint:
        name = "fake"

        def load(self):
            from graphify.plugins.edd_core import EddCorePlugin

            class Fake(EddCorePlugin):
                name = "fake"
            return Fake

    monkeypatch.setattr(plugin_mod, "_entry_points", lambda: [FakeEntryPoint()])
    assert [p.name for p in load_spec("fake")] == ["fake"]
    assert "fake" in [p.name for p in discover(None)]


def test_bad_specs_fail_clearly(tmp_path):
    with pytest.raises(PluginError, match="file not found"):
        load_spec(str(tmp_path / "nope.py"))
    (tmp_path / "empty.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(PluginError, match="define `plugin`"):
        load_spec(str(tmp_path / "empty.py"))
    (tmp_path / "broken.py").write_text("plugin = object()\n", encoding="utf-8")
    with pytest.raises(PluginError, match="needs a `name`"):
        load_spec(str(tmp_path / "broken.py"))
    errors: list[str] = []
    monkey_env = {plugin_mod.ENV_VAR: str(tmp_path / "empty.py")}
    discover(tmp_path, env=monkey_env, errors=errors)
    assert errors and "define `plugin`" in errors[0]
