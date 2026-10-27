"""Domain extraction: framework concepts on top of the code graph.

Plugins read the Clojure corpus and fill a generic :class:`DomainModel`
(services, commands, queries, events and their links). Graphify then checks
the links, converts Malli schemas to JSON Schema and writes a Markdown report
with Mermaid diagrams.

Use :func:`analyze` for the model and :func:`write_outputs` for the files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from graphify.domain.corpus import Corpus, read_file
from graphify.domain.model import DomainModel, Element, Finding, KindSpec, Ref, Relation
from graphify.domain.plugin import DomainPlugin, PluginError, discover

__all__ = [
    "DomainModel", "DomainPlugin", "DomainResult", "Element", "Finding", "KindSpec", "PluginError", "Ref",
    "Relation", "analyze", "write_outputs",
]


@dataclass
class DomainResult:
    model: DomainModel
    corpus: Corpus
    plugins: list = field(default_factory=list)
    plugin_errors: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.plugins


def analyze(root: str | Path, paths: Iterable[str | Path] | None = None, plugins: list | None = None,
            specs: Iterable[str] | None = None) -> DomainResult:
    """Run the domain plugins on a project.

    ``plugins`` are plugin objects. ``specs`` name plugins to load (see
    :mod:`graphify.domain.plugin`). Without both, every discovered plugin runs
    that detects its framework in the corpus.
    """
    root = Path(root)
    errors: list[str] = []
    explicit = plugins is not None or bool(specs)
    if plugins is None:
        plugins = discover(root, list(specs) if specs else None, errors=None if specs else errors)
    candidates = Corpus.candidates(root, paths)
    if not explicit and not _any_marker(candidates, plugins):
        # No detected plugin can apply: skip parsing every Clojure file.
        corpus, plugins = Corpus(root, []), []
    else:
        corpus = Corpus(root, [read_file(p, root) for p in candidates])
    model = DomainModel(root=str(root))
    active = []
    for plugin in plugins:
        if not explicit and not plugin.detect(corpus):
            continue
        active.append(plugin)
        model.plugins.append(plugin.name)
        model.kinds.update(getattr(plugin, "kinds", {}) or {})
        plugin.extract(corpus, model)
    model.check_references()
    for plugin in active:
        finalize = getattr(plugin, "finalize", None)
        if finalize is not None:
            finalize(corpus, model)
    return DomainResult(model, corpus, active, errors)


def _any_marker(candidates: list[Path], plugins: list) -> bool:
    """False only when every plugin has markers and no candidate file holds one."""
    if any(not getattr(p, "markers", ()) for p in plugins):
        return True
    markers = [re.compile(m) for p in plugins for m in p.markers]
    for path in candidates:
        try:
            source = path.read_bytes()
        except OSError:
            # read_file records the error on the parsed corpus.
            return True
        if any(m.search(source) for m in markers):
            return True
    return False


def write_outputs(model: DomainModel, out_dir: str | Path) -> dict[str, Path]:
    """Write ``DOMAIN.md``, ``domain.json`` and ``schemas/`` into ``out_dir``."""
    from graphify.domain.report import write_outputs as _write

    return _write(model, Path(out_dir))
