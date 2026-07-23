import json
import tempfile
import unittest
from pathlib import Path

from skyhook import wire


class WireHelperTests(unittest.TestCase):
    def test_marked_block_create_update_idempotent_preserves_surroundings(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "CLAUDE.md"
            self.assertEqual(wire.upsert_marked_block(p, "hello"), "created")
            self.assertIn("skyhook:start", p.read_text())
            self.assertIn("hello", p.read_text())
            self.assertEqual(wire.upsert_marked_block(p, "hello"), "unchanged")

            # user content around the block must survive an update
            p.write_text("# My project\n\n" + p.read_text() + "\ntrailing note\n")
            self.assertEqual(wire.upsert_marked_block(p, "world"), "updated")
            txt = p.read_text()
            self.assertIn("# My project", txt)
            self.assertIn("trailing note", txt)
            self.assertIn("world", txt)
            self.assertNotIn("hello", txt)
            self.assertEqual(txt.count("skyhook:start"), 1)

    def test_toml_tagged_blocks_coexist_and_preserve_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            cfg.write_text('[mcp_servers.other]\ncommand = "x"\n')
            wire.upsert_marked_block(cfg, "[mcp_servers.skyhook-a]\n", "toml", tag="a")
            wire.upsert_marked_block(cfg, "[mcp_servers.skyhook-b]\n", "toml", tag="b")
            txt = cfg.read_text()
            self.assertIn("mcp_servers.other", txt)
            self.assertIn("skyhook-a", txt)
            self.assertIn("skyhook-b", txt)
            self.assertEqual(txt.count("# skyhook:start:a"), 1)
            self.assertEqual(txt.count("# skyhook:start:b"), 1)

    def test_merge_json_preserves_other_servers_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".mcp.json"
            p.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
            srv = {"command": "skyhook", "args": ["mcp"], "alwaysLoad": True}
            self.assertEqual(wire.merge_json_mcp(p, srv), "updated")
            data = json.loads(p.read_text())
            self.assertIn("other", data["mcpServers"])
            self.assertIs(data["mcpServers"]["skyhook"]["alwaysLoad"], True)
            self.assertEqual(wire.merge_json_mcp(p, srv), "unchanged")


class WirePlanTests(unittest.TestCase):
    def test_build_plan_writes_all_three_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "myrepo"
            repo.mkdir()
            home = root / "home"
            home.mkdir()
            codex = home / ".codex" / "config.toml"
            for action in wire.build_plan(repo, ["claude", "codex", "cursor"], home=home, codex_config=codex):
                action.run()

            # Claude Code
            mcp = json.loads((repo / ".mcp.json").read_text())
            self.assertEqual(mcp["mcpServers"]["skyhook"]["command"], "skyhook")
            self.assertIs(mcp["mcpServers"]["skyhook"]["alwaysLoad"], True)
            self.assertIn(str(repo.resolve()), mcp["mcpServers"]["skyhook"]["args"])
            self.assertIn("skyhook:start", (repo / "CLAUDE.md").read_text())

            # Cursor (plain server, no alwaysLoad; owns a rule file)
            cmcp = json.loads((repo / ".cursor" / "mcp.json").read_text())
            self.assertIn("skyhook", cmcp["mcpServers"])
            self.assertNotIn("alwaysLoad", cmcp["mcpServers"]["skyhook"])
            self.assertIn("alwaysApply: true", (repo / ".cursor" / "rules" / "skyhook.mdc").read_text())

            # Codex: per-repo-named server in the (global) config, protocol in project AGENTS.md
            ctxt = codex.read_text()
            self.assertIn("mcp_servers.skyhook-myrepo", ctxt)
            self.assertIn("skyhook:start:myrepo", ctxt)
            self.assertIn("skyhook:start", (repo / "AGENTS.md").read_text())

    def test_codex_per_repo_servers_coexist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            codex = home / ".codex" / "config.toml"
            for name in ("alpha", "beta"):
                r = root / name
                r.mkdir()
                for action in wire.build_plan(r, ["codex"], home=home, codex_config=codex):
                    action.run()
            ctxt = codex.read_text()
            self.assertIn("mcp_servers.skyhook-alpha", ctxt)
            self.assertIn("mcp_servers.skyhook-beta", ctxt)

    def test_cli_dry_run_writes_nothing(self):
        from skyhook.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertEqual(main(["wire", "--repo", str(repo), "--agent", "claude", "--dry-run"]), 0)
            self.assertFalse((repo / ".mcp.json").exists())
            self.assertFalse((repo / "CLAUDE.md").exists())


if __name__ == "__main__":
    unittest.main()
