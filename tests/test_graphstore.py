import subprocess
import tempfile
import unittest
from pathlib import Path

from skyhook import grammars
from skyhook.config import default_config
from skyhook.graphstore import GraphStore
from skyhook.resolve import resolve_calls
from skyhook.scanner import scan_repo


def _has_python():
    return grammars.get_language("Python", "") is not None


def _make_repo(tmp: str) -> Path:
    root = Path(tmp)
    subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text(
        "def helper(x):\n    return x + 1\n\ndef foo(x):\n    return helper(x)\n", encoding="utf-8"
    )
    (root / "pkg" / "b.py").write_text(
        "from pkg.a import foo\n\ndef run():\n    return foo(2)\n", encoding="utf-8"
    )
    return root


@unittest.skipUnless(_has_python(), "python grammar not installed")
class GraphStoreTests(unittest.TestCase):
    def _scan(self, root):
        return scan_repo(root, default_config())

    def test_build_query_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            scan = self._scan(root)
            store = GraphStore(":memory:")
            summary = store.build(scan)
            self.assertGreaterEqual(summary["symbols"], 3)
            self.assertTrue(store.file_exists("pkg/a.py"))
            self.assertFalse(store.file_exists("pkg/nope.py"))
            self.assertTrue(any(s["name"] == "helper" for s in store.find_symbol("helper")))
            self.assertEqual(
                {s["name"] for s in store.symbols_in_file("pkg/a.py")}, {"helper", "foo"}
            )

    def test_build_creates_missing_parent_dir(self):
        # Fresh worktree scenario: .skyhook/ does not exist yet. build_graph
        # must create it rather than crash on sqlite3.connect (the shell/Codex
        # `skyhook graph build` path, which — unlike `skyhook mcp` — never
        # mkdir'd the output dir before opening the db).
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            scan = self._scan(root)
            from skyhook.graphstore import build_graph

            db = root / ".skyhook" / "graph.db"
            self.assertFalse(db.parent.exists())
            build_graph(scan, db)  # must not raise
            self.assertTrue(db.exists())
            self.assertTrue(db.with_suffix(".json").exists())

    def test_build_can_skip_json_export(self):
        # Transient builds (mcp / query self-bootstrap) only need graph.db to
        # serve queries; graph.json is a commit-artifact and must NOT be written
        # into a worktree that has not adopted Skyhook (would dirty the tree).
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            scan = self._scan(root)
            from skyhook.graphstore import build_graph

            db = root / ".skyhook" / "graph.db"
            build_graph(scan, db, export_json=False)
            self.assertTrue(db.exists())
            self.assertFalse(db.with_suffix(".json").exists())

    def test_incremental_skips_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            scan = self._scan(root)
            store = GraphStore(":memory:")
            store.build(scan)
            second = store.build(self._scan(root))
            self.assertEqual(second["files"], 0)
            self.assertGreater(second["skipped"], 0)

    def test_export_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            store = GraphStore(":memory:")
            store.build(self._scan(root))
            d1 = store.digest()
            store2 = GraphStore(":memory:")
            store2.build(self._scan(root))
            self.assertEqual(d1, store2.digest())

    def test_resolution_and_callers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            store = GraphStore(":memory:")
            store.build(self._scan(root))
            res = resolve_calls(store)
            self.assertGreaterEqual(res["resolved"], 2)  # helper<-foo, foo<-run
            callers = {c["name"] for c in store.callers_of("helper")}
            self.assertIn("foo", callers)
            callees = {c["name"] for c in store.callees_of("foo")}
            self.assertIn("helper", callees)
            blast = store.blast_radius("pkg/a.py", depth=3)
            self.assertTrue(blast["approximate"])
            impacted = {i["name"] for i in blast["impacted"]}
            self.assertIn("run", impacted)  # run -> foo -> helper (transitive)


if __name__ == "__main__":
    unittest.main()
