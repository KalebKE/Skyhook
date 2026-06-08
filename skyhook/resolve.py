"""Call-name resolution (approximate).

Tree-sitter yields call *sites by name*, not which definition they bind to.
This fills ``calls.resolved_symbol_id`` with a pragmatic, scope-ranked guess and
records ambiguous candidates in ``call_candidates``. Resolution is APPROXIMATE
(name-based) and every consumer surfaces that; precise binding (stack-graphs /
scope analysis) is deferred and the schema is forward-compatible.

Ranking, best-first:
  0. same file as the call site
  1. repo-wide (any file)
A unique best-rank candidate sets ``resolved_symbol_id``; otherwise the call is
left unresolved with its candidates recorded.
"""

from __future__ import annotations

from typing import Dict, List

# structural kinds a call can bind to (functions/methods + constructor-style class calls).
_CALLABLE = ("function", "method", "class", "struct", "constant", "module")


def resolve_calls(store) -> Dict[str, int]:
    """Resolve calls in a :class:`graphstore.GraphStore`. Returns counts."""
    conn = store.conn

    # Build name -> [(symbol_id, file_id)] index over callable symbols.
    index: Dict[str, List[tuple]] = {}
    for row in conn.execute(
        "SELECT id, name, file_id, structural_kind FROM symbols"
    ):
        if row["structural_kind"] in _CALLABLE:
            index.setdefault(row["name"], []).append((row["id"], row["file_id"]))

    conn.execute("DELETE FROM call_candidates")
    conn.execute("UPDATE calls SET resolved_symbol_id=NULL")

    resolved = 0
    ambiguous = 0
    external = 0
    for call in conn.execute("SELECT id, src_file_id, callee_name FROM calls").fetchall():
        candidates = index.get(call["callee_name"])
        if not candidates:
            external += 1
            continue
        same_file = [c for c in candidates if c[1] == call["src_file_id"]]
        pool = same_file if same_file else candidates
        rank = 0 if same_file else 1
        if len(pool) == 1:
            conn.execute(
                "UPDATE calls SET resolved_symbol_id=? WHERE id=?", (pool[0][0], call["id"])
            )
            resolved += 1
        else:
            ambiguous += 1
            conn.executemany(
                "INSERT INTO call_candidates(call_id, symbol_id, rank) VALUES(?,?,?)",
                [(call["id"], sid, rank) for sid, _ in pool],
            )

    conn.commit()
    return {"resolved": resolved, "ambiguous": ambiguous, "external": external}
