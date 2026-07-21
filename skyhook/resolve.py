"""Call-name resolution (staged, graded).

Tree-sitter yields call *sites* (callee name + optional receiver/qualifier),
not which definition they bind to. This binds ``calls.resolved_symbol_id``
through a ladder of narrowing stages and records which stage won in
``calls.resolution`` so consumers can grade confidence instead of treating
every edge as equally approximate. Ambiguous calls keep their candidates in
``call_candidates`` with the stage index as ``rank``.

The ladder, best-first (first stage whose pool is exactly one candidate wins):

  same_file     defined in the call's own file (same-class methods preferred)
  qualified     ``Foo.bar()`` -> symbols named ``bar`` scoped inside ``Foo``
  imported      defined in a file the caller imports
  same_package  defined in the caller's declared package/namespace
  global        unique repo-wide name match (heuristic)

Everything is resolved from in-memory dict indexes — no per-call SQL — so a
full pass over ~100k calls stays well under a second.
"""

from __future__ import annotations

import posixpath
from typing import Dict, List, Optional, Set, Tuple

# structural kinds a call can bind to (functions/methods + constructor-style class calls).
_CALLABLE = ("function", "method", "class", "struct", "constant", "module")

# Ladder order; index doubles as the candidate rank for ambiguous calls.
STAGES = ("same_file", "qualified", "imported", "same_package", "global")

_SELF_QUALIFIERS = {"this", "self", "super"}


class _Indexes:
    """All lookup structures for one resolution pass, built in a single sweep."""

    def __init__(self, conn):
        self.by_name: Dict[str, List[Tuple[int, int]]] = {}
        self.by_scope_name: Dict[Tuple[str, str], List[Tuple[int, int]]] = {}
        self.sym_scope: Dict[int, Optional[str]] = {}
        self.file_pkg: Dict[int, Optional[str]] = {}
        self.file_path: Dict[int, str] = {}
        self.file_lang: Dict[int, Optional[str]] = {}
        self.pkg_files: Dict[str, Set[int]] = {}
        self.imports_by_file: Dict[int, List[str]] = {}
        self._imported_files_cache: Dict[int, Set[int]] = {}

        # Object/companion parents, so `Car.create()` also matches a method
        # scoped to a named companion/object nested in class Car.
        object_parent: Dict[str, str] = {}

        for row in conn.execute("SELECT id, path, language, package FROM files"):
            self.file_pkg[row["id"]] = row["package"]
            self.file_path[row["id"]] = row["path"]
            self.file_lang[row["id"]] = row["language"]
            if row["package"]:
                self.pkg_files.setdefault(row["package"], set()).add(row["id"])

        rows = conn.execute(
            "SELECT id, name, file_id, structural_kind, scope FROM symbols"
        ).fetchall()
        for row in rows:
            if row["structural_kind"] == "object" and row["scope"]:
                object_parent[row["name"]] = row["scope"]
        for row in rows:
            self.sym_scope[row["id"]] = row["scope"]
            if row["structural_kind"] not in _CALLABLE:
                continue
            entry = (row["id"], row["file_id"])
            self.by_name.setdefault(row["name"], []).append(entry)
            scope = row["scope"]
            if scope:
                self.by_scope_name.setdefault((scope, row["name"]), []).append(entry)
                grandparent = object_parent.get(scope)
                if grandparent:
                    self.by_scope_name.setdefault((grandparent, row["name"]), []).append(entry)

        for row in conn.execute("SELECT file_id, target FROM imports"):
            self.imports_by_file.setdefault(row["file_id"], []).append(row["target"])

    def imported_files(self, file_id: int) -> Set[int]:
        """File ids this file's imports point at (package/module/path matching)."""
        cached = self._imported_files_cache.get(file_id)
        if cached is not None:
            return cached
        out: Set[int] = set()
        src_dir = posixpath.dirname(self.file_path.get(file_id, ""))
        for target in self.imports_by_file.get(file_id, ()):
            if target.startswith("."):
                # JS/TS relative import: resolve against the importing file's dir
                # and match the path-derived package (path sans extension).
                resolved = posixpath.normpath(posixpath.join(src_dir, target))
                out |= self.pkg_files.get(resolved, set())
                continue
            # Exact package/module match (python `import a.b`, kotlin star pkg).
            out |= self.pkg_files.get(target, set())
            # Exact-symbol import (kotlin/java `import com.acme.engine.Engine`,
            # python `from a.b import c` stores `a.b` already): drop the last
            # segment and match the remaining package.
            if "." in target:
                out |= self.pkg_files.get(target.rsplit(".", 1)[0], set())
        self._imported_files_cache[file_id] = out
        return out


def _unique(pool: List[Tuple[int, int]]) -> Optional[int]:
    return pool[0][0] if len(pool) == 1 else None


def resolve_calls(store) -> Dict[str, object]:
    """Resolve calls in a :class:`graphstore.GraphStore`. Returns counts."""
    conn = store.conn
    idx = _Indexes(conn)

    conn.execute("DELETE FROM call_candidates")
    conn.execute("UPDATE calls SET resolved_symbol_id=NULL, resolution=NULL")

    resolved_rows: List[Tuple[int, str, int]] = []  # (symbol_id, stage, call_id)
    candidate_rows: List[Tuple[int, int, int]] = []  # (call_id, symbol_id, rank)
    by_stage: Dict[str, int] = {}
    ambiguous = 0
    external = 0

    calls = conn.execute(
        "SELECT c.id, c.src_file_id, c.src_symbol_id, c.callee_name, c.qualifier FROM calls c"
    ).fetchall()
    for call in calls:
        name = call["callee_name"]
        candidates = idx.by_name.get(name)
        if not candidates:
            external += 1
            continue

        file_id = call["src_file_id"]
        qualifier = call["qualifier"]
        pools: List[Tuple[str, List[Tuple[int, int]]]] = []

        # 1. same_file — prefer methods of the caller's own class.
        same_file = [c for c in candidates if c[1] == file_id]
        if same_file:
            caller_scope = idx.sym_scope.get(call["src_symbol_id"])
            if caller_scope:
                same_class = [c for c in same_file if idx.sym_scope.get(c[0]) == caller_scope]
                if same_class:
                    same_file = same_class
        pools.append(("same_file", same_file))

        # 2. qualified — Foo.bar() binds to `bar` scoped inside Foo/object Foo.
        if qualifier and qualifier not in _SELF_QUALIFIERS:
            q_last = qualifier.rsplit(".", 1)[-1]
            qualified = idx.by_scope_name.get((q_last, name), [])
            if len(qualified) > 1:
                # Several classes named Foo: prefer ones the caller can see.
                visible = idx.imported_files(file_id) | idx.pkg_files.get(
                    idx.file_pkg.get(file_id) or "", set()
                )
                narrowed = [c for c in qualified if c[1] in visible]
                if narrowed:
                    qualified = narrowed
            pools.append(("qualified", qualified))

        # 3. imported — defined in a file the caller imports.
        imported = [c for c in candidates if c[1] in idx.imported_files(file_id)]
        pools.append(("imported", imported))

        # 4. same_package — same declared package/namespace (no import needed).
        pkg = idx.file_pkg.get(file_id)
        if pkg:
            pkg_fids = idx.pkg_files.get(pkg, set())
            same_pkg = [c for c in candidates if c[1] in pkg_fids and c[1] != file_id]
            pools.append(("same_package", same_pkg))

        # 5. global — unique repo-wide match (heuristic).
        pools.append(("global", candidates))

        resolved_here = False
        for stage, pool in pools:
            sid = _unique(pool)
            if sid is not None:
                resolved_rows.append((sid, stage, call["id"]))
                by_stage[stage] = by_stage.get(stage, 0) + 1
                resolved_here = True
                break
        if resolved_here:
            continue

        # Ambiguous: record the earliest non-empty pool at its stage rank.
        ambiguous += 1
        for stage, pool in pools:
            if pool:
                rank = STAGES.index(stage)
                candidate_rows.extend((call["id"], sid, rank) for sid, _fid in pool)
                break

    conn.executemany(
        "UPDATE calls SET resolved_symbol_id=?, resolution=? WHERE id=?", resolved_rows
    )
    conn.executemany(
        "INSERT INTO call_candidates(call_id, symbol_id, rank) VALUES(?,?,?)", candidate_rows
    )
    conn.commit()
    return {
        "resolved": len(resolved_rows),
        "ambiguous": ambiguous,
        "external": external,
        "byStage": by_stage,
    }
