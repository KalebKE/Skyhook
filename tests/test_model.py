import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skyhook.config import default_config
from skyhook.model import ModelError, StaticOrienter, chat_json, resolve_provider
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


class ChatJsonTests(unittest.TestCase):
    def test_resolve_provider_auto_without_key_is_static(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_provider(default_config()), "static")

    def test_resolve_provider_override_wins(self):
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            self.assertEqual(resolve_provider(default_config(), "static"), "static")
            self.assertEqual(resolve_provider(default_config()), "openai")

    def test_chat_json_returns_none_for_static_provider(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            result = chat_json(default_config(), [{"role": "user", "content": "hi"}])
        self.assertIsNone(result)

    def test_chat_json_parses_openai_response(self):
        body = json.dumps(
            {"choices": [{"message": {"content": json.dumps({"rules": ["a"]})}}]}
        ).encode("utf-8")

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with mock.patch("urllib.request.urlopen", return_value=FakeResponse(body)):
                result = chat_json(default_config(), [{"role": "user", "content": "hi"}])
        self.assertEqual(result, {"rules": ["a"]})

    def test_chat_json_raises_model_error_on_http_failure(self):
        import urllib.error

        error = urllib.error.URLError("connection refused")
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with mock.patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(ModelError):
                    chat_json(default_config(), [{"role": "user", "content": "hi"}])


if __name__ == "__main__":
    unittest.main()
