"""Calls through a TypeScript namespace import: `import * as ns from "./m"; ns.fn()`.

Before the fix, `ns.fn()` gave no `calls` edge. The member-call resolver only knows
typed receivers, and the bare name `fn` can collide across the corpus. Change
impact and references then stopped at the module boundary. The import edge now
carries the local name `ns`, and the call goes to the one `fn` in module `m`.
"""
from __future__ import annotations

from pathlib import Path

from graphify.extract import extract

ROWS, RUN, OTHER = "src_repo_rows", "src_db_run", "src_db_other"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _calls(result: dict) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in result["edges"] if e["relation"] == "calls"}


def _db(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "src/db.ts",
        "export function run(q: string): string { return q; }\n"
        "export function other(q: string): string { return q; }\n",
    )


def test_namespace_import_call_resolves_to_the_module_function(tmp_path: Path):
    db = _db(tmp_path)
    caller = _write(
        tmp_path / "src/repo.ts",
        'import * as db from "./db";\nexport function rows(k: string) { return db.run(k); }\n',
    )

    calls = _calls(extract([db, caller], cache_root=tmp_path))

    assert (ROWS, RUN) in calls
    assert (ROWS, OTHER) not in calls


def test_import_require_call_resolves_like_a_namespace_import(tmp_path: Path):
    db = _db(tmp_path)
    caller = _write(
        tmp_path / "src/repo.ts",
        'import db = require("./db");\nexport function rows(k: string) { return db.run(k); }\n',
    )

    calls = _calls(extract([db, caller], cache_root=tmp_path))

    assert (ROWS, RUN) in calls


def test_a_receiver_that_no_import_binds_gets_no_edge(tmp_path: Path):
    db = _db(tmp_path)
    caller = _write(
        tmp_path / "src/repo.ts",
        "const db = { run: (k: string) => k };\nexport function rows(k: string) { return db.run(k); }\n",
    )

    calls = _calls(extract([db, caller], cache_root=tmp_path))

    assert (ROWS, RUN) not in calls
