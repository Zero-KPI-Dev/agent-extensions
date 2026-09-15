from __future__ import annotations

import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (SKILL_ROOT / relative).read_text(encoding="utf-8")


class SkillPolicyContractTests(unittest.TestCase):
    def test_review_profiles_keep_strong_reasoning_floors(self) -> None:
        policy = _read("references/model-policy.md")

        self.assertIn("| 首次，standard | `sol/high` | `luna/max` |", policy)
        self.assertIn("| 首次，low 且 narrow | `terra/xhigh` | `luna/max` |", policy)
        self.assertIn("| 修复复检，standard | `terra/xhigh` | `luna/max` |", policy)
        for forbidden in ("`terra/medium`", "`luna/medium`", "`luna/high`"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, policy)
        self.assertNotIn("当前父模型 `high`", policy)
        self.assertIn("父配置的 effective 模型与推理强度已确认满足上述下限", policy)

    def test_agent_a_final_position_terminates_stable_initial_disagreement(self) -> None:
        skill = _read("SKILL.md")
        contract = _read("references/review-contract.md")
        publish = _read("references/github-publish.md")

        self.assertIn("只有出现新的实质证据才继续下一轮", skill)
        self.assertIn("A 已明确回应 B 的反证并维持结论", skill)
        self.assertIn("立即以 `FINAL_BY_A` 收束", contract)
        self.assertIn("B 的 supplementary finding 被 A 以证据驳回", contract)
        self.assertIn("finding-level 状态为 `DISPUTED`", contract)
        self.assertIn("顶层报告状态映射为 `DISPUTED_OPEN`", contract)
        self.assertIn("## `A_RECHECK` 状态转移表", contract)
        self.assertIn(
            "| `REJECT_WITH_EVIDENCE` | `CONFIRMED` / `PARTIALLY_CONFIRMED` | `false` | 必填 |",
            contract,
        )
        self.assertIn("| `WITHDRAW` | `REJECTED` | `true` | 必填 |", contract)
        self.assertIn("`DISPUTED`、`DISPUTED_OPEN`", publish)
        self.assertNotIn("三轮后仍冲突：`FINAL_BY_A`", contract)

    def test_report_and_github_policy_support_final_by_a(self) -> None:
        report = _read("references/report-template.md")
        publish = _read("references/github-publish.md")
        contract = _read("references/review-contract.md")

        self.assertIn("FINAL_BY_A", report)
        self.assertIn("`FINAL_BY_A` 的 A-owned actionable finding", publish)
        self.assertIn("披露 B 的异议", publish)
        self.assertIn("AUTO_AFTER_PANEL_DECISION", report)
        self.assertIn("AUTO_AFTER_PANEL_DECISION", contract)
        self.assertIn("设为 `AUTO_AFTER_PANEL_DECISION`", publish)
        finding_status_line = next(
            line for line in report.splitlines() if line.startswith("| 当前状态 |")
        )
        self.assertIn("DISPUTED", finding_status_line)
        self.assertNotIn("DISPUTED_OPEN", finding_status_line)
        self.assertNotIn(
            "结论只有 `DISPUTED`、`FINAL_BY_A` 或证据不足",
            publish,
        )

    def test_successful_explicit_agent_model_does_not_render_unknown_pair(self) -> None:
        policy = _read("references/model-policy.md")
        agent_a = _read("references/agent-a.md")
        agent_b = _read("references/agent-b.md")
        report = _read("references/report-template.md")

        self.assertIn("记录 `effective=requested`", policy)
        self.assertIn("`effective=requested`", agent_a)
        self.assertIn("`effective=requested`", agent_b)
        self.assertIn("已按请求执行", report)
        self.assertIn("不显示 `UNKNOWN / UNKNOWN`", report)
        self.assertIn("内部调度日志", report)
        self.assertNotIn("`A {{agent_a_model_execution_summary}}`", report)
        collaboration_sections = report.split("## 协作记录")[1:]
        self.assertEqual(len(collaboration_sections), 2)
        self.assertTrue(all("| 项目 | 内容 |" in section for section in collaboration_sections))
        collaboration_lines = [
            line for line in report.splitlines() if line.startswith("| Agent A |") or line.startswith("| Agent B |")
        ]
        self.assertEqual(len(collaboration_lines), 4)
        self.assertTrue(all("effective_model" not in line for line in collaboration_lines))

    def test_fix_verified_lifecycle_transition_must_publish(self) -> None:
        skill = _read("SKILL.md")
        publish = _read("references/github-publish.md")
        report = _read("references/report-template.md")

        self.assertIn("非终态迁移为 `FIXED_VERIFIED`", skill)
        self.assertIn("actionable finding 为 0 也必须发布", skill)
        self.assertIn("### 复检生命周期发布优先级", publish)
        self.assertIn("优先级高于“没有 actionable finding”", publish)
        self.assertIn("不能以“无可行动问题”为由设为 `SKIPPED`", publish)
        self.assertIn("应记录为 `PUBLISHED` 或真实的 `FAILED`", report)

    def test_distributed_deployment_impact_is_a_first_class_review_context(self) -> None:
        skill = _read("SKILL.md")
        distributed = _read("references/distributed-deployment-review.md")
        contract = _read("references/review-contract.md")
        agent_a = _read("references/agent-a.md")
        agent_b = _read("references/agent-b.md")
        report = _read("references/report-template.md")

        self.assertIn("references/distributed-deployment-review.md", skill)
        self.assertIn("EchoMem 中会影响运行时行为的 PR", skill)
        self.assertIn("ownership-routing", distributed)
        self.assertIn("旧 owner", distributed)
        self.assertIn("滚动升级", distributed)
        self.assertIn("共享文件系统本身不是数据库租约", distributed)
        self.assertIn("deployment_context", contract)
        self.assertIn("不能只验证单进程调用链", agent_a)
        self.assertIn("不得把 A 的“无分布式影响”当作已证实结论", agent_b)
        self.assertIn("| 分布式影响 |", report)

    def test_new_findings_require_base_head_change_attribution(self) -> None:
        skill = _read("SKILL.md")
        contract = _read("references/review-contract.md")
        agent_a = _read("references/agent-a.md")
        agent_b = _read("references/agent-b.md")
        publish = _read("references/github-publish.md")
        report = _read("references/report-template.md")

        self.assertIn("### PR 变更归因门禁", skill)
        self.assertIn("`PRE_EXISTING`、`TOUCHED_ONLY` 或 `ATTRIBUTION_UNCLEAR`", skill)
        self.assertIn('"change_attribution"', contract)
        self.assertIn("base/head 的同一执行路径", agent_a)
        self.assertIn("不得只证明当前 head 中问题存在", agent_b)
        self.assertIn("即使 A/B 共识也不得发布", publish)
        self.assertIn("**PR 归因证据**", report)


if __name__ == "__main__":
    unittest.main()
