# 修复复检精简契约

只在 `FIX_VERIFICATION` 或 `INCREMENTAL_REREVIEW` 中读取。目标是用最小 packet 更新旧 finding 生命周期；不要复述完整旧报告或运行一次新的首次检视。

## Run 与证据包

每个 packet 保留同一个 `run`：`review_id`、`prior_review_id`、mode、risk_tier、repository、base、previous/current head、publish policy/status、mode reason/confidence，并设置 `recheck_scope_policy: "FROZEN_HIGH_CRITICAL_ONLY"`。保留 requested/effective model、reasoning 和 `fork_turns: "none"`；runtime 未暴露实际模型时使用 `UNKNOWN`，不得把 requested 值当作确认过的 effective。

Leader 为 A/B 复用同一证据包：旧 finding ID/revision、原触发路径、预期行为、关闭条件、关联 changed hunks、当前相关符号、定向测试和局限。事实使用 `FACT`；推断使用 `INFERENCE`；缺口使用 `UNKNOWN`。置信度只用 `high`、`medium`、`low`。

## 生命周期状态

- `FIXED_VERIFIED`：原路径已消失，行为符合预期，A/B 都有充分关闭证据。
- `PARTIALLY_FIXED`：部分路径修复但仍有残留。
- `NOT_FIXED`：原路径仍可达。
- `UNVERIFIABLE`：证据不足；给出最小补证动作。
- `OBSOLETE`：原行为或代码已移除，且影响不再存在。
- `REGRESSION_INTRODUCED`：修复导致门禁级回归。
- `DISPUTED_OPEN`：聚焦 recheck 后仍有实质分歧。

## A_FIX_VERIFY

```json
{
  "packet_type": "A_FIX_VERIFY",
  "round": 0,
  "run": {"mode": "FIX_VERIFICATION"},
  "model": {},
  "scope": {"previous_head": "...", "current_head": "...", "relevant_hunks": [], "limitations": []},
  "prior_findings": [{
    "finding_id": "F-001",
    "original_revision": 1,
    "current_status": "FIXED_VERIFIED | PARTIALLY_FIXED | NOT_FIXED | UNVERIFIABLE | OBSOLETE | REGRESSION_INTRODUCED",
    "evidence": [{"type": "FACT", "detail": "决定性事实"}],
    "residual_risk": "...",
    "regression_check": "...",
    "confidence": "high | medium | low"
  }],
  "new_findings": []
}
```

每个旧 finding 只保留会改变状态的证据，不重述原意见。

## B_FIX_VERIFICATION

```json
{
  "packet_type": "B_FIX_VERIFICATION",
  "round": 1,
  "run": {},
  "model": {},
  "reviews": [{
    "finding_id": "F-001",
    "revision_reviewed": 1,
    "status_decision": "CLOSE | KEEP_OPEN | PARTIAL | UNVERIFIABLE | REGRESSION",
    "proposed_status": "FIXED_VERIFIED | PARTIALLY_FIXED | NOT_FIXED | UNVERIFIABLE | OBSOLETE | REGRESSION_INTRODUCED",
    "evidence_check": "决定性事实",
    "counterevidence": [],
    "remaining_trigger_path": "...",
    "regression_check": "...",
    "rationale": "...",
    "requested_verification": [],
    "confidence": "high | medium | low"
  }],
  "supplementary_findings": []
}
```

A/B 状态一致时由 Leader 直接裁决，不调用 A recheck。

## 可选 A_FIX_RECHECK

仅当 B 提供会改变状态的反证、双方存在实质分歧或需要最小补证时，向同一个 A 发送一次未决 finding delta：

```json
{
  "packet_type": "A_FIX_RECHECK",
  "round": 1,
  "run": {},
  "model": {},
  "responses": [{
    "finding_id": "F-001",
    "response": "ACCEPT_CLOSURE | PARTIAL_ACCEPT | KEEP_OPEN_WITH_EVIDENCE | NEED_MORE_EVIDENCE | REOPEN",
    "current_status": "FIXED_VERIFIED | PARTIALLY_FIXED | NOT_FIXED | UNVERIFIABLE | OBSOLETE | REGRESSION_INTRODUCED | DISPUTED_OPEN",
    "new_evidence": [],
    "rationale": "...",
    "consensus": false,
    "disagreement_reason": "..."
  }]
}
```

只允许这一轮，不例行再次调用 B。仍有分歧时保持 `DISPUTED_OPEN`。

## 复检新增意见门禁

`new_findings` 和 `supplementary_findings` 默认必须为空。只有当前 revision 新引入或实质恶化、具有直接证据和现实可达路径、严重级别为 `High`/`Critical`，且会阻断主要功能交付或显著威胁系统稳定性、可用性或数据完整性的问题才可例外加入。

例外 finding 使用 `references/review-contract.md` 的完整 Finding 字段，并额外包含：

```json
{
  "recheck_gate": {
    "introduced_or_worsened_by_current_revision": true,
    "direct_evidence": true,
    "delivery_or_stability_impact": true
  }
}
```

Low、Medium、Suggestion、风格意见、测试偏好和理论风险不得进入复检 packet。A 首先发现时由 B 独立确认；B 首先发现时只为该候选调用一次 A 聚焦确认。没有 A/B 双方确认，不得进入最终 actionable finding 或 GitHub inline。

## 关闭规则

- 只有 A、B 都确认原触发路径已消失、修复符合预期且没有未解释残留风险，才允许 `FIXED_VERIFIED`。
- 一致时不需要 A 再次确认同一结论。
- 任一方仍能建立原路径时保持 `NOT_FIXED`、`PARTIALLY_FIXED` 或 `DISPUTED_OPEN`。
- 证据不足时保持 `UNVERIFIABLE` 并给出最小补证动作。
