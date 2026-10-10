"""The domain plugin contract and the plugin loader.

A domain plugin reads a :class:`~graphify.domain.corpus.Corpus` and adds
framework concepts to a :class:`~graphify.domain.model.DomainModel`. Graphify
finds plugins in these places, in this order:

1. built-in plugins (``graphify.plugins``),
2. installed packages that declare the ``graphify.domain_plugins`` entry point,
3. Python files in ``<project>/.graphify/plugins/``,
4. the specs in the ``GRAPHIFY_PLUGINS`` variable (separated by ``os.pathsep``).

A spec is a plugin name, ``module:attribute``, ``module`` or a path to a
``.py`` file. A later plugin with the same name replaces an earlier one.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable

from graphify.domain.model import DomainModel, KindSpec

ENTRY_POINT_GROUP = "graphify.domain_plugins"
ENV_VAR = "GRAPHIFY_PLUGINS"
PROJECT_PLUGIN_DIR = Path(".graphify") / "plugins"
BUILTIN_PLUGINS = {
    "edd-core": "graphify.plugins.edd_core:plugin",
}


class PluginError(Exception):
    """A plugin spec could not load or does not follow the contract."""


class DomainPlugin:
    """Base class for domain plugins.

    Subclasses set ``name``, ``description`` and ``kinds`` and override
    :meth:`extract`. :meth:`detect` decides if the plugin applies to a corpus
    when nobody names it explicitly.
    """

    name: str = "plugin"
    description: str = ""
    kinds: dict[str, KindSpec] = {}

    def detect(self, corpus) -> bool:  # noqa: ARG002 - part of the contract
        return True

    def extract(self, corpus, model: DomainModel) -> None:
        raise NotImplementedError

    def finalize(self, corpus, model: DomainModel) -> None:  # noqa: ARG002
        """Run after every plugin extracted and the references were checked."""


def _instantiate(obj: Any, origin: str) -> list[Any]:
    if isinstance(obj, (list, tuple)):
        return [p for o in obj for p in _instantiate(o, origin)]
    if isinstance(obj, type):
        obj = obj()
    elif callable(obj) and not hasattr(obj, "extract"):
        obj = obj()
    if not hasattr(obj, "extract") or not getattr(obj, "name", None):
        raise PluginError(f"{origin}: a plugin needs a `name` and an `extract(corpus, model)` method")
    return [obj]


def _from_module(module: Any, attr: str | None, origin: str) -> list[Any]:
    if attr:
        if not hasattr(module, attr):
            raise PluginError(f"{origin}: module has no attribute `{attr}`")
        return _instantiate(getattr(module, attr), origin)
    for candidate in ("plugins", "plugin", "PLUGIN", "get_plugin"):
        if hasattr(module, candidate):
            return _instantiate(getattr(module, candidate), origin)
    raise PluginError(f"{origin}: define `plugin`, `plugins` or `get_plugin()`")


def load_file(path: Path) -> list[Any]:
    """Import a plugin from a Python file."""
    path = path.resolve()
    digest = hashlib.sha1(str(path).encode()).hexdigest()[:10]
    mod_name = f"graphify_domain_plugin_{path.stem.replace('-', '_')}_{digest}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"{path}: cannot import")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(mod_name, None)
        raise PluginError(f"{path}: import failed: {e}") from e
    return _from_module(module, None, str(path))


def _entry_points() -> list[Any]:
    try:
        return list(metadata.entry_points(group=ENTRY_POINT_GROUP))
    except Exception:  # pragma: no cover - broken metadata
        return []


def load_spec(spec: str, project_root: Path | None = None) -> list[Any]:
    """Load the plugins that one spec names."""
    spec = spec.strip()
    if not spec:
        return []
    if spec in BUILTIN_PLUGINS:
        return load_spec(BUILTIN_PLUGINS[spec], project_root)
    for ep in _entry_points():
        if ep.name == spec:
            try:
                return _instantiate(ep.load(), f"entry point {spec}")
            except PluginError:
                raise
            except Exception as e:
                raise PluginError(f"entry point {spec}: {e}") from e
    path = Path(spec).expanduser()
    if not path.is_absolute() and project_root is not None and (project_root / path).exists():
        path = project_root / path
    if path.suffix == ".py" or path.exists():
        if path.is_dir():
            return [p for f in sorted(path.glob("*.py")) if not f.name.startswith("_") for p in load_file(f)]
        if not path.exists():
            raise PluginError(f"{spec}: file not found")
        return load_file(path)
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except Exception as e:
        raise PluginError(f"{spec}: cannot import module `{module_name}`: {e}") from e
    return _from_module(module, attr or None, spec)


def env_specs(env: dict | None = None) -> list[str]:
    raw = (env if env is not None else os.environ).get(ENV_VAR, "")
    return [s for s in raw.split(os.pathsep) if s.strip()]


def discover(project_root: str | Path | None = None, specs: Iterable[str] | None = None,
             env: dict | None = None, errors: list[str] | None = None) -> list[Any]:
    """Return the plugins to run.

    With ``specs``, load only those plugins. Without them, load every known
    plugin. A spec that fails to load raises :class:`PluginError` when it was
    named in ``specs``; otherwise the error goes to ``errors``.
    """
    root = Path(project_root) if project_root is not None else None
    found: dict[str, Any] = {}
    if specs:
        known: dict[str, Any] | None = None
        for spec in specs:
            try:
                loaded = load_spec(spec, root)
            except PluginError:
                # A bare name can also name a plugin from the project directory or the environment.
                if known is None:
                    known = {p.name: p for p in discover(root, None, env, [])}
                if spec.strip() not in known:
                    raise
                loaded = [known[spec.strip()]]
            for p in loaded:
                found[p.name] = p
        return list(found.values())
    sources: list[str] = list(BUILTIN_PLUGINS)
    sources += [ep.name for ep in _entry_points() if ep.name not in BUILTIN_PLUGINS]
    if root is not None and (root / PROJECT_PLUGIN_DIR).is_dir():
        sources += [str(f) for f in sorted((root / PROJECT_PLUGIN_DIR).glob("*.py")) if not f.name.startswith("_")]
    sources += env_specs(env)
    for spec in sources:
        try:
            for p in load_spec(spec, root):
                found[p.name] = p
        except PluginError as e:
            if errors is None:
                raise
            errors.append(str(e))
    return list(found.values())


def describe(project_root: str | Path | None = None, env: dict | None = None) -> list[dict[str, str]]:
    """List the plugins that :func:`discover` finds, for the CLI and tools."""
    errors: list[str] = []
    rows = [{"name": p.name, "description": getattr(p, "description", "") or "",
             "module": type(p).__module__} for p in discover(project_root, None, env, errors)]
    rows += [{"name": "<error>", "description": e, "module": ""} for e in errors]
    return rows
