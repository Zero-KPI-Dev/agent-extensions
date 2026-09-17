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

    def test_final_by_a_is_never_rendered_as_a_clean_review(self) -> None:
        report = """PR 检视完成
模式: INITIAL_REVIEW
结论: FINAL_BY_A
主要发现摘要: A 维持 F-001；B 的反证已披露但未改变终审结论。
"""

        card = build_review_card(report, pr_url=self.pr_url)

        self.assertEqual(card["header"]["template"], "orange")
        self.assertEqual(card["header"]["title"]["content"], "⚠️ 检视发现待处理问题 · 发布未确认")
        content = self.card_text(card)
        self.assertIn("FINAL_BY_A", content)
        self.assertIn("存在 A 终审确认的待处理问题", content)
        self.assertNotIn("✅ **未发现待处理问题**", content)

    def test_final_by_a_with_counts_still_discloses_non_consensus_outcome(self) -> None:
        report = """PR 检视完成
模式: INITIAL_REVIEW
结论: FINAL_BY_A
发现数量: Critical 0 / High 1 / Medium 0 / Low 0 / Suggestion 0
主要发现摘要:
- F-001 High: A 维持 finding；B 的异议已保留。
"""

        card = build_review_card(report, pr_url=self.pr_url)

        content = self.card_text(card)
        self.assertEqual(card["header"]["template"], "orange")
        self.assertIn("🟠 发现 1 个问题（含 High）· 发布未确认", content)
        self.assertIn("存在 A 终审确认的待处理问题", content)

    def test_bold_markdown_fields_and_counts_are_parsed(self) -> None:
        report = """- **PR**：[EchoMem #486](https://github.com/tech-innovation-group/EchoMem/pull/486)
- **review_id**：`R-20260906-012859-69a53c773153`
- **mode**：`INCREMENTAL_REREVIEW`（首检后 PR 更新，已追加复核）
- **结论**：`ACTION_REQUIRED`。Leader 与独立 A/B 完成检视，共识确认 1 项待处理问题。
- **当前待处理数量**：Critical **0** / High **0** / Medium **1** / Low **0** / Suggestion **0**
- **主要发现摘要**：F-001：认证帮助链接仍指向无效说明。
- **GitHub 发布状态**：已发布 COMMENT review，含 1 条行内意见。
- **未发布或阻塞原因**：无。
"""

        card = build_review_card(report, pr_url=self.pr_url)

        self.assertEqual(card["header"]["template"], "yellow")
        self.assertEqual(card["header"]["title"]["content"], "🟡 发现 1 个问题（最高 Medium）")
        content = self.card_text(card)
        self.assertIn("R-20260906-012859-69a53c773153", content)
        self.assertIn("🟡 **Medium** 1", content)
        self.assertNotIn("摘要未提供严重级别统计", content)

    def test_sectioned_report_table_and_publish_failure_are_parsed(self) -> None:
        report = """## 检视结论

- PR：[tech-innovation-group/echomem#542](https://github.com/tech-innovation-group/echomem/pull/542)
- review_id：`R-20260915-074340-2ea8368c1a47`
- mode：`INITIAL_REVIEW`
- 结论：`ACTION_REQUIRED`。发现 4 项 High、2 项 Medium。

## 当前待处理发现

| Critical | High | Medium | Low | Suggestion |
|---:|---:|---:|---:|---:|
| 0 | 4 | 2 | 0 | 0 |

主要发现：
1. **High**：第一个问题。

## GitHub 发布状态

- 状态：`FAILED`
- 已发布：0 条行内意见，0 条 review body
- 未发布或阻塞原因：目标任务没有可用的发布通道。

## 检视范围与限制
- 只读检视。
"""

        card = build_review_card(report, pr_url=self.pr_url)

        self.assertEqual(card["header"]["template"], "red")
        self.assertEqual(card["header"]["title"]["content"], "❌ 检视完成 · GitHub 发布失败")
        content = self.card_text(card)
        self.assertIn("🟠 **High** 4", content)
        self.assertIn("🟡 **Medium** 2", content)
        self.assertIn("**状态**\\u3000发布失败", content)
        self.assertIn("目标任务没有可用的发布通道", content)
        self.assertNotIn("## GitHub 发布状态", content)
        self.assertNotIn("摘要未提供严重级别统计", content)

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

    def test_fix_verified_legacy_total_counts_are_reclassified_as_history(self) -> None:
        report = """- PR：https://github.com/tech-innovation-group/echomem/pull/475
- review_id：`R-20260904-032743-eb7691332924`
- mode：`FIX_VERIFICATION`
- 结论：`FIX_VERIFIED`。3 个历史 finding 均已共同验证关闭；未发现新增可行动问题。
- 发现数量：Critical 0 / High 1 / Medium 2 / Low 0 / Suggestion 0。以上均为历史 lineage finding，当前全部为 `FIXED_VERIFIED`；开放 actionable finding 为 0。
- 主要发现摘要：
  - High `F-001`：状态更新为 `FIXED_VERIFIED`。
  - Medium `F-002`：保持 `FIXED_VERIFIED`。
  - Medium `F-003`：保持 `FIXED_VERIFIED`。
- GitHub 发布状态：`PUBLISHED`，已发布 `COMMENT` review，inline 0。
- 未发布或阻塞原因：无未发布意见；required checks 仍在运行。
"""

        card = build_review_card(report, pr_url=self.pr_url)

        self.assertEqual(card["header"]["template"], "green")
        self.assertEqual(card["header"]["title"]["content"], "✅ PR 复检通过")
        content = self.card_text(card)
        self.assertIn("历史问题已验证修复，本轮无待处理意见", content)
        self.assertIn("**历史问题**", content)
        self.assertIn("3 条", content)
        self.assertIn("🟠 **High** 1", content)
        self.assertIn("🟡 **Medium** 2", content)
        self.assertNotIn("共发现 3 个待处理问题", content)

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

    def test_missing_exact_head_is_not_presented_as_new_findings(self) -> None:
        report = """- **PR**：[EchoMem #514](https://github.com/tech-innovation-group/EchoMem/pull/514)
- **review_id**：未创建。
- **mode**：INCREMENTAL_REREVIEW（拟定；目标解析失败，未执行）。
- **结论**：目标解析失败。GitHub 当前 base/head 与本轮冻结值一致，但本地缺少精确 head 对象 `b27ca90dae6280c487d7bb0827fffd71bba3dd1b`。
- **当前待处理发现数量**：Critical：未核验；High：未核验；Medium：未核验；Low：未核验；Suggestion：未核验。
- **主要发现摘要**：本轮未进入 diff 检视及 A/B 验证。
- **历史已验证修复**：上轮记录共 4 项（High 1、Medium 3），本轮未重新验证。
- **GitHub 发布状态**：未发布 COMMENT review。
- **未发布或阻塞原因**：精确 head 对象缺失。
"""
        card = build_review_card(report, pr_url="https://github.com/tech-innovation-group/EchoMem/pull/514")
        content = self.card_text(card)

        self.assertEqual(card["header"]["title"]["content"], "⏸️ PR 检视未完成 · 未发布")
        self.assertIn("**结论** 目标解析失败", content)
        self.assertIn("未核验（不能沿用上轮统计）", content)
        self.assertNotIn("发现 4 个问题", content)
        self.assertNotIn("**High** 1", content)
        self.assertNotIn("**主要发现**", content)

    def test_no_new_revision_links_previous_review_without_claiming_new_findings(self) -> None:
        report = """- **PR**：[EchoMem #100](https://github.com/tech-innovation-group/EchoMem/pull/100)
- **review_id**：`R-20260916-074327-e00e8917666d`
- **mode**：`NO_NEW_REVISION`
- **结论**：`NO_NEW_REVISION`。没有新提交可供复检。
- **当前待处理发现数量**：Critical 0 / High 0 / Medium 1 / Low 0 / Suggestion 0
- **GitHub 发布状态**：`SKIPPED`。同一 head、同一 finding 已发布于[上一轮 COMMENT review](https://github.com/tech-innovation-group/EchoMem/pull/100#pullrequestreview-5220270981)，未重复发布。
"""
        card = build_review_card(
            report,
            pr_url="https://github.com/tech-innovation-group/EchoMem/pull/100",
            job_id="a357704dac444ed5aa1d8204d402ba0f",
        )
        content = self.card_text(card)

        self.assertEqual(card["header"]["title"]["content"], "🔁 无新提交 · 未重复检视或发布")
        self.assertIn("本轮没有新提交，未重新运行 A/B", content)
        self.assertIn("**任务 ID** a357704d", content)
        self.assertNotIn("发现 1 个问题（最高 Medium）", content)
        self.assertIn("查看上次已发布的 review", content)
        self.assertIn("https://github.com/tech-innovation-group/EchoMem/pull/100#pullrequestreview-5220270981", content)

    def test_actionable_review_without_publication_status_is_not_shown_as_published(self) -> None:
        report = """- **PR**：[EchoMem #542](https://github.com/tech-innovation-group/EchoMem/pull/542)
- **mode**：INITIAL_REVIEW
- **结论**：ACTION_REQUIRED
- **当前待处理发现数量**：Critical 0 / High 1 / Medium 0 / Low 0
- **GitHub 发布状态**：未说明
"""
        card = build_review_card(report, pr_url="https://github.com/tech-innovation-group/EchoMem/pull/542")
        self.assertEqual(card["header"]["title"]["content"], "🟠 发现 1 个问题（含 High）· 发布未确认")
        self.assertIn("不要把此卡片视为已发布", self.card_text(card))

    def test_merged_pr_duplicate_uses_historical_label_and_does_not_mention_author(self) -> None:
        report = """- **PR**：[EchoMem #100](https://github.com/tech-innovation-group/EchoMem/pull/100)
- **mode**：NO_NEW_REVISION
- **结论**：MERGED
- **当前待处理发现数量**：Critical 0 / High 0 / Medium 1 / Low 0
- **GitHub 发布状态**：SKIPPED，既有 review：https://github.com/tech-innovation-group/EchoMem/pull/100#pullrequestreview-5220270981
"""
        card = build_review_card(report, pr_url="https://github.com/tech-innovation-group/EchoMem/pull/100", mention_open_id="ou_author")
        content = self.card_text(card)
        self.assertEqual(card["header"]["title"]["content"], "🔁 PR 已合并 · 未重复检视或发布")
        self.assertIn("上轮问题记录", content)
        self.assertNotIn("<at id=ou_author>", content)


if __name__ == "__main__":
    unittest.main()
