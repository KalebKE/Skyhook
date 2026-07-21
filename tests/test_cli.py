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

    def test_graph_query_self_bootstraps_without_dirtying_tree(self):
        # Pipeline scenario: an agent worktree that has NOT adopted Skyhook
        # (no committed .skyhook/). `graph query` must build the graph on demand
        # AND leave the git tree clean — an untracked .skyhook/ would trip the
        # pipeline's clean-tree exit gate.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("def go():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
                cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.assertFalse((root / ".skyhook").exists())

            real = io.StringIO()
            with redirect_stdout(real):
                self.assertEqual(main(["graph", "query", "--repo", str(root), "exists", "src/main.py"]), 0)
            self.assertIn("True", real.getvalue())

            fake = io.StringIO()
            with redirect_stdout(fake):
                self.assertEqual(main(["graph", "query", "--repo", str(root), "exists", "src/nope.py"]), 0)
            self.assertIn("False", fake.getvalue())

            self.assertTrue((root / ".skyhook" / "graph.db").exists())
            self.assertFalse((root / ".skyhook" / "graph.json").exists())  # transient: no commit-artifact
            porcelain = subprocess.run(
                ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
            ).stdout
            self.assertEqual(porcelain.strip(), "", f"tree not clean: {porcelain!r}")

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

    def test_graph_build_forwards_full_flag(self):
        # `skyhook graph build --full` must actually reach build_graph — it was
        # silently hard-coded to full=False (declared flag, no effect).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "main.py").write_text("def top():\n    return 1\n", encoding="utf-8")

            calls = []
            import skyhook.graphstore as graphstore_mod

            real_build_graph = graphstore_mod.build_graph

            def spy(scan, db_path, full=False, **kwargs):
                calls.append(full)
                return real_build_graph(scan, db_path, full=full, **kwargs)

            with patch.object(graphstore_mod, "build_graph", side_effect=spy):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["graph", "build", "--repo", str(root), "--full"]), 0)
            self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
