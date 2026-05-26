import subprocess
import tempfile
import unittest
from pathlib import Path

from skyhook.config import default_config
from skyhook.model import StaticOrienter
from skyhook.scanner import scan_repo


class StaticModelTests(unittest.TestCase):
    def test_static_orientation_prioritizes_real_context_over_agent_stubs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "CLAUDE.md").write_text("# Agent Instructions\n", encoding="utf-8")
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "ARCHITECTURE.md").write_text("# Architecture\n", encoding="utf-8")
            (root / ".claude" / "agents").mkdir(parents=True)
            (root / ".claude" / "agents" / "android-developer.md").write_text(
                "# Android Developer\n\narchitecture implementation notes",
                encoding="utf-8",
            )
            (root / ".claude" / "CODE_MAP.md").write_text("# CODE_MAP\n", encoding="utf-8")
            (root / "app" / "src" / "main" / "java" / "com" / "example").mkdir(parents=True)
            (root / "app" / "src" / "androidTest" / "java" / "com" / "example").mkdir(parents=True)
            (root / "app" / "build.gradle.kts").write_text("plugins { id(\"com.android.application\") }\n", encoding="utf-8")
            (root / "app" / "src" / "main" / "java" / "com" / "example" / "MainActivity.kt").write_text(
                "class MainActivity\n",
                encoding="utf-8",
            )
            (root / "app" / "src" / "androidTest" / "java" / "com" / "example" / "MainActivityTest.kt").write_text(
                "class MainActivityTest\n",
                encoding="utf-8",
            )

            data = StaticOrienter().orient(scan_repo(root, default_config()))

            self.assertIn("docs/ARCHITECTURE.md", data["orientation"]["agentStartHere"])
            self.assertNotIn(".claude/agents/android-developer.md", data["orientation"]["agentStartHere"])
            architecture_paths = {path for item in data["architecture"] for path in item["paths"]}
            self.assertIn(".claude/CODE_MAP.md", architecture_paths)
            self.assertIn("docs/ARCHITECTURE.md", architecture_paths)
            self.assertNotIn(".claude/agents/android-developer.md", architecture_paths)
            app_area = next(area for area in data["codeAreas"] if area["name"] == "app")
            self.assertLess(
                app_area["entrypoints"].index("app/src/main/java/com/example/MainActivity.kt"),
                app_area["entrypoints"].index("app/src/androidTest/java/com/example/MainActivityTest.kt"),
            )


if __name__ == "__main__":
    unittest.main()
