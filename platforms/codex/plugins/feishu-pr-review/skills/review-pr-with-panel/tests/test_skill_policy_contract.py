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
        self.assertNotIn(
            "结论只有 `DISPUTED`、`FINAL_BY_A` 或证据不足",
            publish,
        )


if __name__ == "__main__":
    unittest.main()
