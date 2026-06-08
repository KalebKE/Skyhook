import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from skyhook import grammars
from skyhook.config import default_config
from skyhook.graphstore import GraphStore
from skyhook.resolve import resolve_calls
from skyhook.scanner import scan_repo

try:
    import mcp  # noqa: F401

    _HAS_MCP = True
except Exception:
    _HAS_MCP = False


def _has_python():
    return grammars.get_language("Python", "") is not None


@unittest.skipUnless(_HAS_MCP and sys.version_info >= (3, 10), "mcp extra not installed / py<3.10")
@unittest.skipUnless(_has_python(), "python grammar not installed")
class McpServerTests(unittest.TestCase):
    def _store(self, tmp):
        root = Path(tmp)
        subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (root / "a.py").write_text("def helper(x):\n    return x\n\ndef foo(x):\n    return helper(x)\n", encoding="utf-8")
        store = GraphStore(":memory:")
        store.build(scan_repo(root, default_config()))
        resolve_calls(store)
        return store

    def test_tools_register_and_query(self):
        from skyhook.mcp_server import build_server

        with tempfile.TemporaryDirectory() as tmp:
            server = build_server(self._store(tmp))

            async def run():
                tools = {t.name for t in await server.list_tools()}
                self.assertIn("callers_of", tools)
                self.assertIn("blast_radius", tools)
                self.assertIn("file_exists", tools)
                res = await server.call_tool("callers_of", {"name": "helper"})
                structured = res[1] if isinstance(res, tuple) else res
                names = {c["name"] for c in structured["result"]}
                self.assertIn("foo", names)

            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
