"""The domain corpus reads forms only when a plugin needs them."""

from graphify.domain import analyze
from graphify.domain.corpus import Corpus


def _project(tmp_path):
    src = tmp_path / "src" / "app"
    src.mkdir(parents=True)
    (src / "core.clj").write_text(
        "(ns app.core (:require [app.db :as db]))\n(defn run [] (db/query))\n"
    )
    (src / "db.clj").write_text("(ns app.db)\n(defn query [] :ok)\n")
    return tmp_path


def test_unused_corpus_skips_the_form_read(tmp_path):
    result = analyze(_project(tmp_path))
    assert result.empty
    assert all(sf._pending is not None for sf in result.corpus.files)


def test_lazy_read_keeps_namespaces_aliases_and_definitions(tmp_path):
    corpus = Corpus.load(_project(tmp_path))
    core = corpus.by_ns["app.core"]
    assert core.aliases == {"db": "app.db"} and core.requires("app.db")
    assert sorted(corpus.definitions) == ["app.core/run", "app.db/query"]
    assert corpus.resolve("db/query", core) == "app.db/query"
    assert len(core.forms) == 2
