from __future__ import annotations

import json
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class PluginPortabilityTests(unittest.TestCase):
    def test_mcp_server_uses_plugin_relative_paths(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
        server = manifest["mcpServers"]["feishu-pr-review"]

        self.assertEqual(server["cwd"], ".")
        self.assertEqual(server["args"], ["./server/mcp_server.py"])
        self.assertNotIn("env", server)

    def test_runtime_files_do_not_hardcode_original_user_home(self) -> None:
        runtime_files = [
            PLUGIN_ROOT / ".mcp.json",
            PLUGIN_ROOT / "config.example.json",
            PLUGIN_ROOT / "server" / "config.py",
            PLUGIN_ROOT / "server" / "gateway.py",
            PLUGIN_ROOT / "server" / "long_connection.py",
            PLUGIN_ROOT / "scripts" / "configure.py",
        ]

        for path in runtime_files:
            with self.subTest(path=path.name):
                self.assertNotIn("/Users/local-developer", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
