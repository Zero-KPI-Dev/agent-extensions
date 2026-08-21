from __future__ import annotations

import unittest

from server.feishu import build_review_card


class ReviewCardTests(unittest.TestCase):
    pr_url = "https://github.com/tech-innovation-group/EchoMem/pull/365"

    @staticmethod
    def card_text(card: dict[str, object]) -> str:
        return str(card)

    def test_medium_findings_are_visible_at_a_glance_and_mode_is_inferred(self) -> None:
        report = """PR 检视完成

PR: https://github.com/tech-innovation-group/EchoMem/pull/365
Review ID: R-20260820-120746-930bcb359c01
结论为 `ACTION_REQUIRED`。
发现概览: Critical 0, High 0, Medium 2, Low 0
- F-001：Garden-only 重试存在 worker 交接竞态
- F-002：目标 Unit 退化时会搁置独立 pending projection
GitHub 发布: 已发布
"""

        card = build_review_card(report, pr_url=self.pr_url)

        self.assertEqual(card["header"]["template"], "yellow")
        self.assertEqual(card["header"]["title"]["content"], "🟡 发现 2 个问题（最高 Medium）")
        content = self.card_text(card)
        self.assertIn("INITIAL_REVIEW（初次检视）", content)
        self.assertNotIn("**模式** —", content)
        self.assertIn("共发现 2 个待处理问题", content)
        self.assertIn("🟡 **Medium** 2", content)
        self.assertIn("ACTION_REQUIRED", content)
        self.assertIn("F-001", content)

    def test_critical_findings_use_red_header(self) -> None:
        report = """PR 检视完成
模式: INITIAL_REVIEW
发现: Critical 1, High 1, Medium 0, Low 0
"""

        card = build_review_card(report, pr_url=self.pr_url)

        self.assertEqual(card["header"]["template"], "red")
        self.assertIn("含 Critical", card["header"]["title"]["content"])

    def test_incremental_mode_is_recognized(self) -> None:
        report = """PR 复检完成
模式: INCREMENTAL_REREVIEW
结论: FIX_VERIFIED
发现: Critical 0, High 0, Medium 0, Low 0
"""

        card = build_review_card(report, pr_url=self.pr_url)

        self.assertEqual(card["header"]["template"], "green")
        content = self.card_text(card)
        self.assertIn("INCREMENTAL_REREVIEW（增量复检）", content)
        self.assertIn("本轮无待处理意见", content)

    def test_fix_verified_history_is_not_treated_as_actionable(self) -> None:
        report = """- PR：https://github.com/tech-innovation-group/echomem/pull/365
- review_id：`R-20260820-144240-824903decaba`
- mode：`FIX_VERIFICATION`
- 结论：`FIX_VERIFIED`。未发现阻塞合入的问题。
- 发现数量：Critical 0 / High 0 / Medium 0 / Low 0 / Suggestion 0
- 主要发现摘要：
  - 历史 Medium `F-001`：`FIXED_VERIFIED`，Garden retry 的 worker 交接竞态已修复。
  - 历史 Medium `F-002`：`FIXED_VERIFIED`，target-unit fallback 已能提升独立 pending projection。
  - 两项均经 A/B 独立验证；未发现符合复检门禁的新增问题。
- GitHub 发布状态：`PUBLISHED`，以 `COMMENT` 发布；新增 inline 0。
- 未发布或阻塞原因：无。
"""

        card = build_review_card(report, pr_url=self.pr_url)

        self.assertEqual(card["header"]["template"], "green")
        self.assertEqual(card["header"]["title"]["content"], "✅ PR 复检通过")
        content = self.card_text(card)
        self.assertIn("历史问题已验证修复，本轮无待处理意见", content)
        self.assertIn("**已验证修复**", content)
        self.assertNotIn("检视发现待处理问题", content)
        self.assertNotIn("摘要未提供严重级别统计", content)

    def test_failure_header_takes_priority_over_counts(self) -> None:
        report = """PR 检视失败
模式: INITIAL_REVIEW
发现: Critical 0, High 0, Medium 2, Low 0
后台执行异常: unable to open database file
"""

        card = build_review_card(report, pr_url=self.pr_url)

        self.assertEqual(card["header"]["template"], "red")
        self.assertEqual(card["header"]["title"]["content"], "❌ PR 检视失败")
        self.assertIn("检视未完成", self.card_text(card))


if __name__ == "__main__":
    unittest.main()
