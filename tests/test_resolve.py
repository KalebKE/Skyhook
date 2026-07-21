import subprocess
import tempfile
import unittest
from pathlib import Path

from skyhook import grammars
from skyhook.config import default_config
from skyhook.graphstore import GraphStore
from skyhook.resolve import resolve_calls
from skyhook.scanner import scan_repo


def _has(lang):
    return grammars.get_language(lang, "") is not None


def _build(root: Path) -> GraphStore:
    store = GraphStore(":memory:")
    store.build(scan_repo(root, default_config()), full=True)
    return store


def _git_init(root: Path) -> None:
    subprocess.run(
        ["git", "init"], cwd=root, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _resolution_of(store: GraphStore, callee: str, src_path: str = None):
    q = (
        "SELECT c.resolution, s.name AS target, f.path AS target_path "
        "FROM calls c LEFT JOIN symbols s ON c.resolved_symbol_id=s.id "
        "LEFT JOIN files f ON s.file_id=f.id "
        "JOIN files sf ON c.src_file_id=sf.id WHERE c.callee_name=?"
    )
    args = [callee]
    if src_path:
        q += " AND sf.path=?"
        args.append(src_path)
    return store.conn.execute(q, args).fetchall()


@unittest.skipUnless(_has("Python"), "python grammar not installed")
class PythonResolveTests(unittest.TestCase):
    def test_same_file_wins_over_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            (root / "pkg").mkdir()
            (root / "pkg" / "a.py").write_text(
                "def helper():\n    return 1\n\ndef foo():\n    return helper()\n"
            )
            (root / "pkg" / "b.py").write_text("def helper():\n    return 2\n")
            store = _build(root)
            resolve_calls(store)
            rows = _resolution_of(store, "helper", "pkg/a.py")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["resolution"], "same_file")
            self.assertEqual(rows[0]["target_path"], "pkg/a.py")
            store.close()

    def test_import_narrows_between_same_named_defs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            (root / "alpha").mkdir()
            (root / "beta").mkdir()
            (root / "alpha" / "util.py").write_text("def helper():\n    return 1\n")
            (root / "beta" / "util.py").write_text("def helper():\n    return 2\n")
            (root / "main.py").write_text(
                "from alpha.util import helper\n\ndef run():\n    return helper()\n"
            )
            store = _build(root)
            resolve_calls(store)
            rows = _resolution_of(store, "helper", "main.py")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["resolution"], "imported")
            self.assertEqual(rows[0]["target_path"], "alpha/util.py")
            store.close()

    def test_ambiguous_records_graded_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            (root / "a.py").write_text("def helper():\n    return 1\n")
            (root / "b.py").write_text("def helper():\n    return 2\n")
            (root / "main.py").write_text("def run():\n    return helper()\n")
            store = _build(root)
            res = resolve_calls(store)
            self.assertGreaterEqual(res["ambiguous"], 1)
            rows = store.conn.execute(
                "SELECT cc.rank FROM call_candidates cc "
                "JOIN calls c ON cc.call_id=c.id WHERE c.callee_name='helper'"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            # global stage index is 4 — candidates carry a meaningful rank.
            self.assertTrue(all(r["rank"] == 4 for r in rows))
            store.close()

    def test_by_stage_counts_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            (root / "a.py").write_text(
                "def helper():\n    return 1\n\ndef foo():\n    return helper()\n"
            )
            store = _build(root)
            res = resolve_calls(store)
            self.assertIn("byStage", res)
            self.assertGreaterEqual(res["byStage"].get("same_file", 0), 1)
            store.close()


@unittest.skipUnless(_has("Kotlin"), "kotlin grammar not installed")
class KotlinResolveTests(unittest.TestCase):
    def _repo(self, tmp: str) -> Path:
        root = Path(tmp)
        _git_init(root)
        (root / "engine").mkdir()
        (root / "app").mkdir()
        (root / "engine" / "Engine.kt").write_text(
            "package com.acme.engine\n\n"
            "class Engine {\n"
            "    fun start() {}\n"
            "}\n"
        )
        (root / "app" / "Car.kt").write_text(
            "package com.acme.app\n\n"
            "import com.acme.engine.Engine\n\n"
            "class Car(private val engine: Engine) {\n"
            "    companion object {\n"
            "        fun create(): Car = Car(Engine())\n"
            "    }\n"
            "    fun drive() {\n"
            "        engine.start()\n"
            "        stop()\n"
            "    }\n"
            "    fun stop() {}\n"
            "}\n"
        )
        (root / "app" / "Garage.kt").write_text(
            "package com.acme.app\n\n"
            "class Garage {\n"
            "    fun open() {\n"
            "        val car = Car.create()\n"
            "        helper()\n"
            "    }\n"
            "}\n"
            "fun helper() {}\n"
        )
        return root

    def test_member_call_resolves_via_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _build(self._repo(tmp))
            resolve_calls(store)
            # engine.start() in Car.kt -> Engine.start in the imported file.
            rows = _resolution_of(store, "start", "app/Car.kt")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["target_path"], "engine/Engine.kt")
            self.assertIn(rows[0]["resolution"], ("qualified", "imported"))
            store.close()

    def test_qualified_companion_call_resolves_to_class_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _build(self._repo(tmp))
            resolve_calls(store)
            # Car.create() in Garage.kt -> create scoped under Car (companion).
            rows = _resolution_of(store, "create", "app/Garage.kt")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["resolution"], "qualified")
            self.assertEqual(rows[0]["target_path"], "app/Car.kt")
            store.close()

    def test_same_package_resolves_without_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _build(self._repo(tmp))
            resolve_calls(store)
            # helper() in Garage.kt is top-level in the same file -> same_file;
            # constructor Car() / Car.create() cross-file needs no import.
            rows = _resolution_of(store, "helper", "app/Garage.kt")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["resolution"], "same_file")
            store.close()

    def test_constructor_call_binds_to_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _build(self._repo(tmp))
            resolve_calls(store)
            # Car(Engine()) inside Car.kt: Car -> class Car (same file).
            rows = _resolution_of(store, "Car", "app/Car.kt")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["target"], "Car")
            self.assertEqual(rows[0]["resolution"], "same_file")
            store.close()

    def test_qualified_disambiguates_same_method_name_across_classes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            (root / "M.kt").write_text(
                "package a\n\n"
                "object Foo {\n    fun run() {}\n}\n"
                "object Bar {\n    fun run() {}\n}\n"
                "fun main() {\n    Foo.run()\n}\n"
            )
            store = _build(root)
            resolve_calls(store)
            rows = _resolution_of(store, "run", "M.kt")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["resolution"], "qualified")
            # Bind target is Foo.run, not Bar.run.
            sid_scope = store.conn.execute(
                "SELECT s.scope FROM calls c JOIN symbols s ON c.resolved_symbol_id=s.id "
                "WHERE c.callee_name='run'"
            ).fetchone()
            self.assertEqual(sid_scope["scope"], "Foo")
            store.close()


if __name__ == "__main__":
    unittest.main()
