import subprocess
import tempfile
import unittest
from pathlib import Path

from skyhook.config import default_config
from skyhook.scanner import classify_doc, extract_imports, extract_symbols, scan_repo


class ScannerTests(unittest.TestCase):
    def test_scans_git_repo_and_excludes_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "README.md").write_text("# Example\n\nStart here.", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("def main(): pass\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "ignored.js").write_text("ignored\n", encoding="utf-8")

            scan = scan_repo(root, default_config())

            paths = {record.path for record in scan.files}
            self.assertIn("README.md", paths)
            self.assertIn("pyproject.toml", paths)
            self.assertIn("src/app.py", paths)
            self.assertNotIn("node_modules/ignored.js", paths)
            self.assertIn("Python", scan.language_counts)
            self.assertIn("Python", scan.frameworks)
            self.assertTrue(scan.digest)

    def test_extracts_symbols_imports_and_test_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "src").mkdir()
            (root / "src" / "sync_service.py").write_text(
                "import json\nfrom pathlib import Path\n\nclass SyncService:\n    def retry(self): pass\n",
                encoding="utf-8",
            )
            (root / "tests").mkdir()
            (root / "tests" / "test_sync_service.py").write_text("def test_retry(): pass\n", encoding="utf-8")

            scan = scan_repo(root, default_config())
            source = next(record for record in scan.files if record.path == "src/sync_service.py")
            test = next(record for record in scan.files if record.path == "tests/test_sync_service.py")

            self.assertIn({"name": "SyncService", "kind": "service"}, source.symbols)
            self.assertIn("json", source.imports)
            self.assertIn("pathlib", source.imports)
            self.assertTrue(test.is_test)
            self.assertIn({"name": "test_retry", "kind": "test"}, test.symbols)

    def test_language_specific_extractors(self):
        self.assertIn({"name": "VehicleRepository", "kind": "repository"}, extract_symbols("app/VehicleRepository.kt", "Kotlin", "class VehicleRepository"))
        self.assertIn({"name": "TelemetryView", "kind": "model"}, extract_symbols("ui/TelemetryView.swift", "Swift", "struct TelemetryView {}"))
        self.assertIn("react", extract_imports("TypeScript", "import React from 'react'\nconst x = require('zod')\n"))
        self.assertIn("zod", extract_imports("TypeScript", "import React from 'react'\nconst x = require('zod')\n"))

    def test_classifies_architecture_docs(self):
        self.assertEqual(classify_doc("docs/ADR-001.md", "# Decision"), "adr")
        self.assertEqual(classify_doc("docs/c4-context.md", "# C4"), "c4")
        self.assertEqual(classify_doc("docs/architecture.md", "# Architecture"), "architecture")
        self.assertEqual(classify_doc(".claude/CODE_MAP.md", "# CODE_MAP"), "architecture")
        self.assertEqual(classify_doc("CLAUDE.md", "# Agent Instructions"), "readme")
        self.assertEqual(classify_doc(".claude/context/setup.md", "# Build Commands"), "runbook")
        self.assertEqual(classify_doc(".claude/docs/PRE_SUBMIT_CHECKLIST.md", "# Checklist"), "runbook")


if __name__ == "__main__":
    unittest.main()
