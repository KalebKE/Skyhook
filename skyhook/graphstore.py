"""SQLite-backed symbol + call graph (``.skyhook/graph.db``).

Stores AST-derived symbols, call sites, and imports for fast structural queries
(callers, callees, blast-radius, symbol lookup) that let agents skip
grep-exploration. Built from :class:`scanner.RepoScan` records that carry an
``astextract.FileAST`` and a ``content_hash`` (incremental rebuilds skip
unchanged files).

Pure stdlib ``sqlite3``. The binary ``graph.db`` is regenerable and meant to be
gitignored; the diffable JSON export (:func:`export_json`) and ``map.json`` are
what gets committed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    language TEXT,
    content_hash TEXT,
    is_test INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    symbol_uid TEXT UNIQUE NOT NULL,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT,
    structural_kind TEXT,
    scope TEXT,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    src_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    src_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    callee_name TEXT NOT NULL,
    resolved_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
    line INTEGER
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    line INTEGER
);
CREATE TABLE IF NOT EXISTS call_candidates (
    call_id INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    rank INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_name);
CREATE INDEX IF NOT EXISTS idx_calls_resolved ON calls(resolved_symbol_id);
CREATE INDEX IF NOT EXISTS idx_calls_src ON calls(src_symbol_id);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_id);
"""


def _symbol_uid(path: str, structural_kind: str, scope: Optional[str], name: str, occ: int) -> str:
    raw = f"{path}\0{structural_kind}\0{scope or ''}\0{name}\0{occ}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


class GraphStore:
    """Owns a connection to a graph database (file or ``:memory:``)."""

    def __init__(self, db_path: str = ":memory:", read_only: bool = False):
        self.db_path = db_path
        if read_only and db_path != ":memory:":
            uri = f"file:{Path(db_path).as_posix()}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True)
        else:
            self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if not read_only:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    # ------------------------------------------------------------------ build

    def _meta_get(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def build(self, scan, full: bool = False) -> Dict[str, int]:
        """(Re)build the graph from a scan. Incremental unless ``full``.

        Returns a summary dict ``{files, symbols, calls, imports, skipped}``.
        Call :meth:`resolve_calls` afterwards to populate ``resolved_symbol_id``.
        """
        summary = {"files": 0, "symbols": 0, "calls": 0, "imports": 0, "skipped": 0}
        existing: Dict[str, str] = {}
        if not full:
            for row in self.conn.execute("SELECT path, content_hash FROM files"):
                existing[row["path"]] = row["content_hash"]

        seen_paths = set()
        for record in scan.sources:
            seen_paths.add(record.path)
            fa = record.file_ast
            if fa is None:
                continue
            if not full and existing.get(record.path) == record.content_hash:
                summary["skipped"] += 1
                continue

            # Replace this file's rows (cascade clears its symbols/calls/imports).
            self.conn.execute("DELETE FROM files WHERE path=?", (record.path,))
            cur = self.conn.execute(
                "INSERT INTO files(path, language, content_hash, is_test) VALUES(?,?,?,?)",
                (record.path, record.language, record.content_hash, 1 if record.is_test else 0),
            )
            file_id = cur.lastrowid
            summary["files"] += 1

            # Symbols (stable uid; occurrence index disambiguates collisions).
            name_to_id: Dict[str, int] = {}
            occ: Dict[tuple, int] = {}
            for d in fa.defs:
                key = (d.structural_kind, d.scope, d.name)
                occ[key] = occ.get(key, 0)
                uid = _symbol_uid(record.path, d.structural_kind, d.scope, d.name, occ[key])
                occ[key] += 1
                kind = self._record_symbol_kind(record, d.name)
                scur = self.conn.execute(
                    "INSERT OR IGNORE INTO symbols"
                    "(symbol_uid, file_id, name, kind, structural_kind, scope, start_line, end_line, signature)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (uid, file_id, d.name, kind, d.structural_kind, d.scope,
                     d.start_line, d.end_line, d.signature),
                )
                sid = scur.lastrowid or self.conn.execute(
                    "SELECT id FROM symbols WHERE symbol_uid=?", (uid,)
                ).fetchone()["id"]
                name_to_id.setdefault(d.name, sid)
                summary["symbols"] += 1

            # Imports.
            for imp in fa.imports:
                self.conn.execute(
                    "INSERT INTO imports(file_id, target, line) VALUES(?,?,?)",
                    (file_id, imp.target, imp.line),
                )
                summary["imports"] += 1

            # Calls (unresolved; resolve_calls fills resolved_symbol_id later).
            for c in fa.calls:
                src_id = name_to_id.get(c.enclosing) if c.enclosing else None
                self.conn.execute(
                    "INSERT INTO calls(src_symbol_id, src_file_id, callee_name, line) VALUES(?,?,?,?)",
                    (src_id, file_id, c.callee_name, c.line),
                )
                summary["calls"] += 1

        # Drop files no longer present (deletions) on a full or incremental pass.
        placeholders = ",".join("?" for _ in seen_paths) or "''"
        self.conn.execute(
            f"DELETE FROM files WHERE path NOT IN ({placeholders})", tuple(seen_paths)
        )

        self._meta_set("schemaVersion", str(SCHEMA_VERSION))
        self._meta_set("scanDigest", getattr(scan, "digest", "") or "")
        self.conn.commit()
        return summary

    @staticmethod
    def _record_symbol_kind(record, name: str) -> str:
        # The scanner already computed the human kind onto record.symbols; reuse it.
        for s in record.symbols:
            if s.get("name") == name:
                return str(s.get("kind") or "")
        return ""

    # --------------------------------------------------------------- queries

    def file_exists(self, path: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM files WHERE path=? LIMIT 1", (path,)).fetchone()
            is not None
        )

    def symbols_in_file(self, path: str) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT s.* FROM symbols s JOIN files f ON s.file_id=f.id "
            "WHERE f.path=? ORDER BY s.start_line",
            (path,),
        ).fetchall()
        return [self._symbol_row(r) for r in rows]

    def find_symbol(self, name: str, limit: int = 50) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT s.*, f.path AS path FROM symbols s JOIN files f ON s.file_id=f.id "
            "WHERE s.name=? ORDER BY f.path LIMIT ?",
            (name, limit),
        ).fetchall()
        return [self._symbol_row(r) for r in rows]

    def search(self, query: str, limit: int = 50) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT s.*, f.path AS path FROM symbols s JOIN files f ON s.file_id=f.id "
            "WHERE s.name LIKE ? ORDER BY length(s.name), f.path LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [self._symbol_row(r) for r in rows]

    def stats(self) -> Dict[str, int]:
        def count(table: str) -> int:
            return self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

        resolved = self.conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE resolved_symbol_id IS NOT NULL"
        ).fetchone()["n"]
        calls = count("calls")
        return {
            "files": count("files"),
            "symbols": count("symbols"),
            "calls": calls,
            "imports": count("imports"),
            "resolved_calls": resolved,
            "resolved_pct": round(100 * resolved / calls, 1) if calls else 0,
        }

    def _symbol_ids_for(self, target: str) -> List[int]:
        """Symbol ids for a target: every symbol in a file path, or all named `target`."""
        if self.file_exists(target) or "/" in target or target.endswith(
            (".py", ".swift", ".kt", ".java", ".js", ".ts", ".go", ".ex")
        ):
            rows = self.conn.execute(
                "SELECT s.id FROM symbols s JOIN files f ON s.file_id=f.id WHERE f.path=?",
                (target,),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT id FROM symbols WHERE name=?", (target,)).fetchall()
        return [r["id"] for r in rows]

    def callers_of(self, name: str, strict: bool = False) -> List[Dict]:
        """Symbols that call `name`. Approximate unless ``strict`` (resolved-only)."""
        target_ids = [r["id"] for r in self.conn.execute("SELECT id FROM symbols WHERE name=?", (name,))]
        if not target_ids:
            return []
        ph = ",".join("?" * len(target_ids))
        out: Dict[tuple, Dict] = {}
        for r in self.conn.execute(
            f"SELECT DISTINCT s.name AS name, f.path AS path, c.line AS line "
            f"FROM calls c JOIN symbols s ON c.src_symbol_id=s.id JOIN files f ON s.file_id=f.id "
            f"WHERE c.resolved_symbol_id IN ({ph})",
            target_ids,
        ):
            out[(r["name"], r["path"])] = {"name": r["name"], "path": r["path"], "line": r["line"], "approximate": True}
        if not strict:
            for r in self.conn.execute(
                f"SELECT DISTINCT s.name AS name, f.path AS path, c.line AS line "
                f"FROM call_candidates cc JOIN calls c ON cc.call_id=c.id "
                f"JOIN symbols s ON c.src_symbol_id=s.id JOIN files f ON s.file_id=f.id "
                f"WHERE cc.symbol_id IN ({ph})",
                target_ids,
            ):
                out.setdefault((r["name"], r["path"]), {"name": r["name"], "path": r["path"], "line": r["line"], "approximate": True})
        return list(out.values())

    def callees_of(self, name: str) -> List[Dict]:
        """Symbols/names that `name` calls (resolved targets + unresolved names)."""
        src_ids = [r["id"] for r in self.conn.execute("SELECT id FROM symbols WHERE name=?", (name,))]
        if not src_ids:
            return []
        ph = ",".join("?" * len(src_ids))
        out: Dict[str, Dict] = {}
        for r in self.conn.execute(
            f"SELECT DISTINCT t.name AS name, f.path AS path FROM calls c "
            f"JOIN symbols t ON c.resolved_symbol_id=t.id JOIN files f ON t.file_id=f.id "
            f"WHERE c.src_symbol_id IN ({ph})",
            src_ids,
        ):
            out[r["name"]] = {"name": r["name"], "path": r["path"], "resolved": True, "approximate": True}
        for r in self.conn.execute(
            f"SELECT DISTINCT callee_name AS name FROM calls "
            f"WHERE src_symbol_id IN ({ph}) AND resolved_symbol_id IS NULL",
            src_ids,
        ):
            out.setdefault(r["name"], {"name": r["name"], "path": None, "resolved": False, "approximate": True})
        return list(out.values())

    def blast_radius(self, target: str, depth: int = 3) -> Dict:
        """Transitive reverse-call closure: who is (in)directly impacted by `target`."""
        seed = set(self._symbol_ids_for(target))
        if not seed:
            return {"target": target, "approximate": True, "impacted": []}
        impacted: Dict[int, int] = {}  # symbol_id -> distance
        frontier = set(seed)
        for dist in range(1, max(1, depth) + 1):
            if not frontier:
                break
            ph = ",".join("?" * len(frontier))
            callers = set()
            for r in self.conn.execute(
                f"SELECT DISTINCT c.src_symbol_id AS sid FROM calls c "
                f"WHERE c.resolved_symbol_id IN ({ph}) AND c.src_symbol_id IS NOT NULL",
                tuple(frontier),
            ):
                callers.add(r["sid"])
            for r in self.conn.execute(
                f"SELECT DISTINCT c.src_symbol_id AS sid FROM call_candidates cc "
                f"JOIN calls c ON cc.call_id=c.id "
                f"WHERE cc.symbol_id IN ({ph}) AND c.src_symbol_id IS NOT NULL",
                tuple(frontier),
            ):
                callers.add(r["sid"])
            new = {sid for sid in callers if sid not in impacted and sid not in seed}
            for sid in new:
                impacted[sid] = dist
            frontier = new
        rows = []
        if impacted:
            ph = ",".join("?" * len(impacted))
            for r in self.conn.execute(
                f"SELECT s.id AS id, s.name AS name, f.path AS path FROM symbols s "
                f"JOIN files f ON s.file_id=f.id WHERE s.id IN ({ph})",
                tuple(impacted),
            ):
                rows.append({"name": r["name"], "path": r["path"], "distance": impacted[r["id"]]})
        rows.sort(key=lambda x: (x["distance"], x["path"], x["name"]))
        files = sorted({r["path"] for r in rows})
        return {"target": target, "approximate": True, "impacted": rows, "impactedFiles": files}

    @staticmethod
    def _symbol_row(row: sqlite3.Row) -> Dict:
        data = {
            "name": row["name"],
            "kind": row["kind"],
            "structuralKind": row["structural_kind"],
            "scope": row["scope"],
            "startLine": row["start_line"],
            "endLine": row["end_line"],
            "signature": row["signature"],
            "symbolUid": row["symbol_uid"],
        }
        if "path" in row.keys():
            data["path"] = row["path"]
        return data

    # ---------------------------------------------------------------- export

    def export_dict(self) -> Dict:
        """Deterministic, diffable snapshot (no timestamps) for committing."""
        files = self.conn.execute(
            "SELECT path, language, is_test FROM files ORDER BY path"
        ).fetchall()
        symbols = self.conn.execute(
            "SELECT s.symbol_uid, f.path AS path, s.name, s.kind, s.structural_kind, "
            "s.scope, s.start_line, s.end_line FROM symbols s JOIN files f ON s.file_id=f.id "
            "ORDER BY f.path, s.start_line, s.name"
        ).fetchall()
        imports = self.conn.execute(
            "SELECT f.path AS path, i.target, i.line FROM imports i JOIN files f ON i.file_id=f.id "
            "ORDER BY f.path, i.line, i.target"
        ).fetchall()
        calls = self.conn.execute(
            "SELECT f.path AS path, c.callee_name, c.line FROM calls c "
            "JOIN files f ON c.src_file_id=f.id ORDER BY f.path, c.line, c.callee_name"
        ).fetchall()
        return {
            "schemaVersion": SCHEMA_VERSION,
            "scanDigest": self._meta_get("scanDigest") or "",
            "files": [dict(r) for r in files],
            "symbols": [dict(r) for r in symbols],
            "imports": [dict(r) for r in imports],
            "calls": [dict(r) for r in calls],
        }

    def export_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.export_dict(), indent=2, sort_keys=False) + "\n")

    def digest(self) -> str:
        payload = self.export_dict()
        payload.pop("scanDigest", None)  # digest the structure, not the upstream digest
        blob = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha1(blob).hexdigest()

    def close(self) -> None:
        self.conn.close()


def build_graph(scan, db_path: Path, full: bool = False, resolve: bool = True) -> Dict[str, int]:
    """Convenience: open/build/(resolve)/export at ``db_path`` + sibling ``graph.json``."""
    store = GraphStore(str(db_path))
    summary = store.build(scan, full=full)
    if resolve:
        try:
            from .resolve import resolve_calls  # Stage 3 (optional until present)

            resolve_calls(store)
        except Exception:
            pass
    store.export_json(db_path.with_suffix(".json"))
    store.close()
    return summary
