"""Move a top-level definition to another file, or move a file and rename its module.

A move plan holds the old and new bytes of every file it changes, so the caller
can preview the diff. ``apply_move`` writes a conflict-free plan and refuses a
file that changed after the plan. Each language family has its own module:
``clojure_moves``, ``python_moves``, ``script_moves``, ``go_moves``, ``rust_moves``,
``named_moves``, ``path_moves``, ``block_moves`` and ``unit_moves``.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from graphify.refactor import CLOJURE_SUFFIXES, Mention, StaleFile, Unsupported, digest

__all__ = ["MovePlan", "StaleFile", "Unsupported", "apply_move", "plan_move", "plan_move_file"]

_SKIP_DIRS = frozenset({".git", "node_modules", "graphify-out", "target", ".cpcache", "__pycache__", ".venv", "venv", "dist", "build", ".clj-kondo", ".lsp", ".shadow-cljs"})


@dataclass(frozen=True)
class MovePlan:
    """The file contents of one move, and why it is not safe when it is not.

    ``contents`` maps an absolute path to ``(old, new)`` bytes. ``old`` is None for a
    created file and ``new`` is None for a deleted file.
    """

    subject: str
    source: str
    target: str
    contents: dict = field(default_factory=dict)
    conflicts: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    mentions: tuple[Mention, ...] = ()
    renames: tuple[tuple[str, str], ...] = ()

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(sorted(self.contents))

    @property
    def created(self) -> tuple[str, ...]:
        return tuple(sorted(p for p, (old, _) in self.contents.items() if old is None))

    @property
    def deleted(self) -> tuple[str, ...]:
        return tuple(sorted(p for p, (_, new) in self.contents.items() if new is None))


def apply_move(plan: MovePlan) -> list[str]:
    """Write a conflict-free plan. Refuse a file that changed, or a new file that exists, since the plan."""
    if plan.conflicts:
        raise ValueError("The move plan has conflicts: " + " ".join(plan.conflicts))
    for path, (old, _) in plan.contents.items():
        p = Path(path)
        if old is None:
            if p.exists():
                raise StaleFile(f"{path} exists now. Plan the move again.")
        elif not p.is_file() or digest(p.read_bytes()) != digest(old):
            raise StaleFile(f"{path} changed after the move plan. Plan the move again.")
    for path, (_, new) in sorted(plan.contents.items()):
        if new is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(new)
    for path in plan.deleted:
        Path(path).unlink()
    return sorted(plan.contents)


def replace_ranges(data: bytes, edits) -> bytes:
    """Apply ``(start, end, text)`` edits that do not overlap. Raise on an overlap."""
    out = data
    last = None
    for start, end, text in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        if last is not None and end > last:
            raise ValueError(f"Overlapping edits at byte {start}.")
        out = out[:start] + (text.encode("utf-8") if isinstance(text, str) else text) + out[end:]
        last = start
    return out


def project_files(root: Path, graph: dict, suffixes) -> list[Path]:
    """Files of the project with these suffixes: the graph's files and the files git tracks."""
    suffixes = tuple(s.lower() for s in suffixes)
    found: set[Path] = set()
    for n in graph.get("nodes", ()):
        f = str(n.get("source_file") or "")
        if f.lower().endswith(suffixes):
            p = Path(f) if Path(f).is_absolute() else root / f
            if p.is_file():
                found.add(p.resolve())
    try:
        done = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root, capture_output=True, text=True, timeout=60)
        listed = done.stdout.splitlines() if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        listed = None
    if listed is None:
        listed = [str(p.relative_to(root)) for p in root.rglob("*") if not (_SKIP_DIRS & set(p.relative_to(root).parts))]
    for rel in listed:
        if rel.lower().endswith(suffixes) and not (_SKIP_DIRS & set(Path(rel).parts)):
            p = (root / rel).resolve()
            if p.is_file():
                found.add(p)
    return sorted(found)


def resolve_path(root: Path, path) -> Path:
    p = Path(path).expanduser()
    return (p if p.is_absolute() else root / p).resolve()


def _family(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in CLOJURE_SUFFIXES:
        return "clojure"
    if suffix in (".py", ".pyi"):
        return "python"
    from graphify import script_moves

    if suffix in script_moves.SUFFIXES:
        return "script"
    if suffix == ".go":
        return "go"
    if suffix == ".rs":
        return "rust"
    from graphify import named_moves

    if suffix in named_moves.LANGS:
        return "named"
    from graphify import path_moves

    if suffix in path_moves.LANGS:
        return "path"
    from graphify import block_moves

    if suffix in block_moves.LANGS:
        return "block"
    from graphify import unit_moves

    if suffix in unit_moves.LANGS:
        return "unit"
    return suffix or path.name


def _adapter(family: str):
    if family == "clojure":
        from graphify import clojure_moves

        return clojure_moves
    if family == "python":
        from graphify import python_moves

        return python_moves
    if family == "script":
        from graphify import script_moves

        return script_moves
    if family == "go":
        from graphify import go_moves

        return go_moves
    if family == "rust":
        from graphify import rust_moves

        return rust_moves
    if family == "named":
        from graphify import named_moves

        return named_moves
    if family == "path":
        from graphify import path_moves

        return path_moves
    if family == "block":
        from graphify import block_moves

        return block_moves
    if family == "unit":
        from graphify import unit_moves

        return unit_moves
    raise Unsupported(f"Moves do not support {family} files yet.")


def plan_move_file(graph: dict, path, new_path, root, tests: bool = True) -> MovePlan:
    """Plan the move of the file ``path`` to ``new_path`` and the rename of its module everywhere.

    For Clojure, ``new_path`` can also be the new namespace name. With ``tests``, a
    Clojure namespace ``x-test`` of the moved namespace ``x`` moves along with it.
    """
    root = Path(root).resolve()
    old = resolve_path(root, path)
    if not old.is_file():
        raise FileNotFoundError(f"{path} is not a file of the project.")
    family = _family(old)
    return _adapter(family).plan_move_file(graph, old, str(new_path), root, tests=tests)


def plan_move(graph: dict, node_id: str, to, root) -> MovePlan:
    """Plan the move of one top-level definition to the file ``to``, which can be new.

    For Clojure, ``to`` can also be a namespace name. Every reference, import and
    require follows the definition. The definition takes the imports it needs.
    ``node_id`` can also be ``path::Name`` for a declaration that the graph does not hold.
    """
    from graphify.refactor import _node, _resolve

    root = Path(root).resolve()
    try:
        node = _node(graph, node_id)
    except Exception:
        if "::" not in str(node_id):
            raise
        file, name = str(node_id).rsplit("::", 1)
        node = {"id": str(node_id), "label": name, "source_file": file}
    file = str(node.get("source_file") or "")
    if not file:
        raise Unsupported(f"{node.get('label', node_id)} has no source file.")
    source = _resolve(root, file).resolve()
    return _adapter(_family(source)).plan_move(graph, node, source, str(to), root)
