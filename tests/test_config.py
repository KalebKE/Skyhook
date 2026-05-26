import tempfile
import unittest
from pathlib import Path

from skyhook.config import load_config


class ConfigTests(unittest.TestCase):
    def test_loads_minimal_yaml_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".skyhook"
            config_dir.mkdir()
            (config_dir / "config.yaml").write_text(
                """
version: 1
outputDir: out
model:
  provider: static
  model: test-model
scan:
  include:
    - src
  exclude:
    - build
    - node_modules
  maxFiles: 42
docs:
  extraGlobs:
    - "docs/**/*.md"
""",
                encoding="utf-8",
            )

            cfg = load_config(root)

            self.assertEqual(cfg.output_dir, "out")
            self.assertEqual(cfg.model.provider, "static")
            self.assertEqual(cfg.model.model, "test-model")
            self.assertEqual(cfg.scan.include, ["src"])
            self.assertEqual(cfg.scan.exclude, ["build", "node_modules"])
            self.assertEqual(cfg.scan.max_files, 42)
            self.assertEqual(cfg.docs.extra_globs, ["docs/**/*.md"])


if __name__ == "__main__":
    unittest.main()
