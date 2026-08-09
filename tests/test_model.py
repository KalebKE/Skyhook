import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skyhook.config import default_config
from skyhook.model import (
    ModelError,
    ModelHTTPError,
    OpenAICompatibleOrienter,
    StaticOrienter,
    _messages_size,
    chat_json,
    resolve_provider,
    scan_facts,
)
from skyhook.scanner import FileRecord, RepoScan, scan_repo
from skyhook.schema import empty_map


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

    def test_http_context_error_keeps_structured_error_code(self):
        import urllib.error

        detail = b'{"error":{"code":"context_length_exceeded","message":"too long"}}'
        error = urllib.error.HTTPError(
            "https://example.test", 400, "Bad Request", {}, io.BytesIO(detail)
        )
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            orienter = OpenAICompatibleOrienter(default_config())
            with mock.patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(ModelHTTPError) as raised:
                    orienter._chat([{"role": "user", "content": "hi"}])
        self.assertEqual(raised.exception.code, "context_length_exceeded")
        self.assertTrue(raised.exception.is_context_length_error)

    def test_chat_json_uses_configurable_timeout(self):
        body = json.dumps(
            {"choices": [{"message": {"content": json.dumps({"rules": ["a"]})}}]}
        ).encode("utf-8")

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        cfg = default_config()
        cfg.model.timeout_seconds = 180
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with mock.patch("urllib.request.urlopen", return_value=FakeResponse(body)) as urlopen:
                chat_json(cfg, [{"role": "user", "content": "hi"}])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 180)


class ChunkedOrientationTests(unittest.TestCase):
    def _scan(self, docs=0, sources=0):
        records = [
            FileRecord(
                path=f"docs/guide-{index:03d}.md",
                kind="doc",
                title=f"Guide {index}",
                doc_kind="design",
                snippet=(f"documentation-{index}-" * 160),
            )
            for index in range(docs)
        ]
        records.extend(
            FileRecord(
                path=f"src/module-{index:03d}.py",
                kind="source",
                language="Python",
                symbols=[{"name": f"function_{index}", "kind": "other", "line": index + 1}],
                imports=["json"],
            )
            for index in range(sources)
        )
        return RepoScan(
            root=Path("/tmp/demo"),
            repo_name="demo",
            files=records,
            language_counts={"Python": sources},
            top_dirs={"docs": docs, "src": sources},
            digest="digest",
        )

    def _partial(self):
        return {
            "orientation": {
                "summaryHints": ["A bounded repository segment."],
                "agentStartHere": [],
                "knownGotchas": [],
            },
            "codeAreas": [],
            "docs": [],
            "architecture": [],
        }

    def _map(self):
        return empty_map("demo", "/tmp/demo")

    def test_large_scan_is_chunked_under_byte_budget(self):
        cfg = default_config()
        cfg.model.max_prompt_bytes = 7000
        scan = self._scan(docs=12, sources=5)
        calls = []

        def respond(messages):
            calls.append(messages)
            prompt = messages[-1]["content"]
            if "Synthesize one Skyhook" in prompt:
                return json.dumps(self._map())
            return json.dumps(self._partial())

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            orienter = OpenAICompatibleOrienter(cfg)
            with mock.patch.object(orienter, "_chat", side_effect=respond):
                data = orienter.orient(scan)

        mapper_calls = [call for call in calls if "Extract orientation evidence" in call[-1]["content"]]
        self.assertGreater(len(mapper_calls), 1)
        self.assertTrue(all(_messages_size(call) <= cfg.model.max_prompt_bytes for call in calls))
        mapper_text = "\n".join(call[-1]["content"] for call in mapper_calls)
        for record in scan.files:
            self.assertIn(record.path, mapper_text)
        self.assertEqual(data["scan"]["fileCount"], len(scan.files))
        self.assertEqual(data["scan"]["digest"], "digest")

    def test_context_error_bisects_only_the_affected_fact_chunk(self):
        cfg = default_config()
        cfg.model.max_prompt_bytes = 9000
        scan = self._scan(docs=4)
        partial_attempts = 0

        def respond(messages):
            nonlocal partial_attempts
            prompt = messages[-1]["content"]
            if "Synthesize one Skyhook" in prompt:
                return json.dumps(self._map())
            partial_attempts += 1
            if partial_attempts == 1:
                raise ModelHTTPError(
                    400,
                    '{"error":{"code":"context_length_exceeded"}}',
                    "context_length_exceeded",
                )
            return json.dumps(self._partial())

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            orienter = OpenAICompatibleOrienter(cfg)
            with mock.patch.object(orienter, "_chat", side_effect=respond):
                orienter.orient(scan)

        self.assertGreaterEqual(partial_attempts, 3)

    def test_large_partial_results_are_reduced_under_the_same_budget(self):
        cfg = default_config()
        cfg.model.max_prompt_bytes = 7000
        scan = self._scan(docs=12)
        calls = []

        def respond(messages):
            calls.append(messages)
            prompt = messages[-1]["content"]
            if "Synthesize one Skyhook" in prompt:
                return json.dumps(self._map())
            partial = self._partial()
            if "Merge these partial" not in prompt:
                partial["orientation"]["summaryHints"] = ["x" * 2400]
            return json.dumps(partial)

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            orienter = OpenAICompatibleOrienter(cfg)
            with mock.patch.object(orienter, "_chat", side_effect=respond):
                orienter.orient(scan)

        self.assertTrue(any("Merge these partial" in call[-1]["content"] for call in calls))
        self.assertTrue(all(_messages_size(call) <= cfg.model.max_prompt_bytes for call in calls))

    def test_invalid_json_is_repaired_without_replaying_scan_facts(self):
        cfg = default_config()
        scan = self._scan(docs=1)
        calls = []

        def respond(messages):
            calls.append(messages)
            if len(calls) == 1:
                return "not-json"
            return json.dumps(self._map())

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            orienter = OpenAICompatibleOrienter(cfg)
            with mock.patch.object(orienter, "_chat", side_effect=respond):
                orienter.orient(scan)

        self.assertEqual(len(calls), 2)
        repair_prompt = calls[1][-1]["content"]
        self.assertIn("JSON to repair", repair_prompt)
        self.assertNotIn("docs/guide-000.md", repair_prompt)

    def test_scan_facts_honors_source_sample_limit(self):
        facts = scan_facts(self._scan(sources=5), max_source_samples=2)
        self.assertEqual(
            [source["path"] for source in facts["sourceSamples"]],
            ["src/module-000.py", "src/module-001.py"],
        )
        self.assertNotIn("schema", facts)


if __name__ == "__main__":
    unittest.main()
