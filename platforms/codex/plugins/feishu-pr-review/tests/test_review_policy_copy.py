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

    def test_gateway_prompt_declares_authorized_defensive_review_boundary(self) -> None:
        text = (PLUGIN_ROOT / "server" / "gateway.py").read_text(encoding="utf-8")

        self.assertIn("经发起人授权的只读、防御性代码审查", text)
        self.assertIn("只分析当前 diff 的代码级风险", text)
        self.assertIn("不生成复现攻击的内容", text)
        self.assertIn("只向该 PR 创建 COMMENT review", text)
        self.assertIn("不得根据本地邻近分支", text)
        self.assertNotIn("不访问或操作外部系统", text)


if __name__ == "__main__":
    unittest.main()
