from __future__ import annotations

import unittest

from server.feishu import build_ack_card


class AckCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pr_url = "https://github.com/tech-innovation-group/echomem/pull/340"

    @staticmethod
    def card_text(card: dict[str, object]) -> str:
        return str(card)

    def test_new_job_card_is_compact_and_actionable(self) -> None:
        card = build_ack_card(
            pr_url=self.pr_url,
            job_id="2a727c4e12345678",
            status="pending",
            pr_label="EchoMem#340",
        )

        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual(card["header"]["title"]["content"], "PR 检视已受理")
        content = self.card_text(card)
        self.assertIn("EchoMem#340", content)
        self.assertIn("2a727c4e", content)
        self.assertIn("等待执行", content)
        self.assertIn("Leader + A/B 独立复核", content)
        self.assertIn(self.pr_url, content)

    def test_deduplicated_card_explains_request_merge(self) -> None:
        card = build_ack_card(
            pr_url=self.pr_url,
            job_id="2a727c4e12345678",
            status="running",
            pr_label="EchoMem#340",
            deduplicated=True,
        )

        self.assertEqual(card["header"]["title"]["content"], "PR 检视已在进行")
        content = self.card_text(card)
        self.assertIn("正在检视", content)
        self.assertIn("不会重复创建 Codex 会话", content)

    def test_missing_repo_mapping_uses_warning_card(self) -> None:
        card = build_ack_card(
            pr_url=self.pr_url,
            job_id="2a727c4e12345678",
            status="pending",
            repo_mapping_missing=True,
            repo_key="tech-innovation-group/echomem",
        )

        self.assertEqual(card["header"]["template"], "orange")
        self.assertEqual(card["header"]["title"]["content"], "PR 检视未启动")
        content = self.card_text(card)
        self.assertIn("需要配置", content)
        self.assertIn("任务不会启动", content)


if __name__ == "__main__":
    unittest.main()
