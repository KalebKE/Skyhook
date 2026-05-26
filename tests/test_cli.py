import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from skyhook.cli import main


class CliTests(unittest.TestCase):
    def test_init_static_writes_artifacts_and_check_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "README.md").write_text("# Demo\n\nDemo repo.", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")

            self.assertEqual(main(["init", "--repo", str(root), "--provider", "static"]), 0)

            self.assertTrue((root / ".skyhook" / "map.json").exists())
            self.assertTrue((root / ".skyhook" / "map.md").exists())
            self.assertTrue((root / ".skyhook" / "INDEX.md").exists())
            self.assertTrue((root / ".skyhook" / "docs.md").exists())
            self.assertTrue((root / ".skyhook" / "architecture.md").exists())
            self.assertTrue((root / ".skyhook" / "tests.md").exists())
            self.assertTrue(any((root / ".skyhook" / "areas").glob("*.md")))
            self.assertEqual(main(["check", "--repo", str(root)]), 0)

    def test_check_fails_when_map_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")

            self.assertEqual(main(["init", "--repo", str(root), "--provider", "static"]), 0)
            (root / "new.py").write_text("print('new')\n", encoding="utf-8")

            self.assertEqual(main(["check", "--repo", str(root)]), 1)

    def test_route_prints_markdown_from_existing_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "architecture.md").write_text("# Architecture\n\nSync boundaries.", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "sync_service.py").write_text("class SyncService:\n    def retry(self): pass\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_sync_service.py").write_text("class SyncServiceTest: pass\n", encoding="utf-8")

            self.assertEqual(main(["init", "--repo", str(root), "--provider", "static"]), 0)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["route", "--repo", str(root), "--task", "add retry handling to SyncService"]), 0)

            text = output.getvalue()
            self.assertIn("# Skyhook Route", text)
            self.assertIn("src/sync_service.py", text)
            self.assertIn("tests/test_sync_service.py", text)

    def test_route_json_and_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "billing.py").write_text("class BillingService: pass\n", encoding="utf-8")

            self.assertEqual(main(["init", "--repo", str(root), "--provider", "static"]), 0)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(["route", "--repo", str(root), "--task", "change BillingService", "--format", "json", "--save"]),
                    0,
                )

            data = json.loads(output.getvalue())
            self.assertEqual(data["schemaVersion"], 1)
            self.assertEqual(data["profile"], "implementation")
            self.assertIn("src/billing.py", data["likelyEditTargets"])
            self.assertTrue((root / ".skyhook" / "routes" / f"{data['id']}.json").exists())
            self.assertTrue((root / ".skyhook" / "routes" / f"{data['id']}.md").exists())

    def test_route_profile_shapes_context_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "README.md").write_text("# Demo\n\nProduct workflow.", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "design.md").write_text("# Checkout Design\n\nUser checkout flow.", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "checkout.py").write_text("class CheckoutService: pass\n", encoding="utf-8")

            self.assertEqual(main(["init", "--repo", str(root), "--provider", "static"]), 0)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "route",
                            "--repo",
                            str(root),
                            "--profile",
                            "product_planning",
                            "--task",
                            "plan checkout user story",
                            "--format",
                            "json",
                        ]
                    ),
                    0,
                )

            data = json.loads(output.getvalue())
            self.assertEqual(data["profile"], "product_planning")
            self.assertEqual(data["likelyEditTargets"], [])
            self.assertIn("docs/design.md", data["readFirst"])

    def test_route_reads_task_file_and_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "vehicles.py").write_text("class VehicleRepository: pass\n", encoding="utf-8")
            task_file = root / "issue.md"
            task_file.write_text("Update VehicleRepository lookup behavior", encoding="utf-8")

            self.assertEqual(main(["init", "--repo", str(root), "--provider", "static"]), 0)

            file_output = io.StringIO()
            with redirect_stdout(file_output):
                self.assertEqual(main(["route", "--repo", str(root), "--task-file", str(task_file)]), 0)
            self.assertIn("src/vehicles.py", file_output.getvalue())

            stdin_output = io.StringIO()
            with patch("sys.stdin", io.StringIO("Update VehicleRepository from stdin")), redirect_stdout(stdin_output):
                self.assertEqual(main(["route", "--repo", str(root)]), 0)
            self.assertIn("src/vehicles.py", stdin_output.getvalue())


if __name__ == "__main__":
    unittest.main()
