from __future__ import annotations

import json
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class ReviewPolicyCopyTests(unittest.TestCase):
    def test_user_facing_copy_describes_consensus_or_agent_a_final_review(self) -> None:
        sources = [
            PLUGIN_ROOT / "README.md",
            PLUGIN_ROOT / "server" / "feishu.py",
            PLUGIN_ROOT / "server" / "gateway.py",
            PLUGIN_ROOT / "skills" / "feishu-pr-review" / "SKILL.md",
        ]

        for path in sources:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(PLUGIN_ROOT)):
                self.assertIn("共识或 A 终审确认", text)
                self.assertNotIn("共识后的可行动", text)

    def test_plugin_version_is_bumped_for_policy_release(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertTrue(manifest["version"].startswith("0.1.1+codex."))


if __name__ == "__main__":
    unittest.main()
