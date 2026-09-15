# 检视契约

## 目录

- [Packet 与最终报告边界](#packet-与最终报告边界)
- [Run manifest](#run-manifest)
- [证据分类](#证据分类)
- [PR 变更归因](#pr-变更归因)
- [严重级别](#严重级别)
- [Finding 字段](#finding-字段)
- [首次/增量新 finding packet](#首次增量新-finding-packet)
- [修复后复检契约](fix-verification-contract.md)
- [共识与结束](#共识与结束)

## Packet 与最终报告边界

A/B 之间只传递本契约规定的 JSON packet，便于机器校验和逐轮更新。Leader 完成裁决后，必须按照 `references/report-template.md` 生成固定 Markdown 报告；该报告是给人和代码托管平台阅读的最终表现形式。共识状态不等于 GitHub 写入授权，发布权限由 `SKILL.md` 的明确用户授权规则控制。

## Run manifest

每次启动子 Agent 的 packet 都应带有同一个 `run` 对象，用于让下一次检视追踪历史：

```json
{
  "review_id": "R-20260806-001",
  "mode": "INITIAL_REVIEW | FIX_VERIFICATION | INCREMENTAL_REREVIEW",
  "risk_tier": "low | standard | high",
  "repository": "/absolute/repository/path",
  "base": "base-ref-or-sha",
  "previous_head": "previous-reviewed-sha-or-null",
  "current_head": "current-head-sha",
  "prior_review_id": "previous-review-id-or-null",
  "github_target": "https://github.com/owner/repo/pull/123-or-null",
  "publish_policy": "AUTO_AFTER_PANEL_DECISION | REPORT_ONLY | NOT_APPLICABLE",
  "publish_status": "NOT_ATTEMPTED | PUBLISHED | FAILED | SKIPPED",
  "mode_reason": "可验证的历史和 Git 依据",
  "mode_confidence": "high | medium | low"
}
```

复检 run 还应记录 `recheck_scope_policy: "FROZEN_HIGH_CRITICAL_ONLY"`。它表示默认只更新旧 finding 生命周期；不是新增普通意见的授权。

每次 A 首包的 `scope` 还必须包含 `deployment_context`，字段和状态值遵循 `references/distributed-deployment-review.md`。即使目标是单机或本次变更不影响分布式路径，也要用仓库证据记录 `topology` 与 `distributed_impact`，不能省略该判断。分布式 ownership、共享状态、迁移、异步接管或滚动升级变更默认至少为 `risk_tier=high`。

`previous_head`、`current_head` 和 `finding_id` 是修复复检建立 lineage 的最低要求。没有可靠 lineage 时不得自动关闭旧 finding。若存在 GitHub PR URL，还要保留 `github_target`、`publish_policy` 和 `publish_status`，以便避免重复发布并追踪外部状态。

新 run 仅在目标 PR 发布授权有效且用户未要求只读/仅报告时使用 `AUTO_AFTER_PANEL_DECISION`，表示可发布 `AGREED` 或通过门禁的 `FINAL_BY_A` finding；没有授权或用户要求仅报告时使用 `REPORT_ONLY`，不适用 GitHub 发布时使用 `NOT_APPLICABLE`。历史 packet 中的 `AUTO_AFTER_CONSENSUS` 只作为兼容值读取，不应写入新 run，也不能据此丢失既有 finding lineage。

每次 run 还必须建立 Skill-owned runtime context。该 context 不改变 packet_type，但用于让 Leader、A、B 共享同一个 SQLite 协作边界：

~~~json
{
  "run_id": "R-20260806-001",
  "state_db": "/absolute/app-state/review-pr-with-panel/runs.sqlite3",
  "current_epoch": 1,
  "event_cursor": 0,
  "heartbeat_interval_seconds": 15,
  "lease_timeout_seconds": 60,
  "settle_grace_seconds": 120
}
~~~

state_db 不得指向 Skill 安装目录、PR 仓库或 worktree。所有事件、packet 和状态查询都必须同时带 run_id、agent_id、epoch；A/B 不得扫描目录猜测当前 run。

runtime event envelope：

~~~json
{
  "run_id": "R-20260806-001",
  "agent_id": "A",
  "epoch": 1,
  "seq": 18,
  "event_type": "heartbeat | progress | observation | packet | ack | lifecycle",
  "phase": "diff_analysis",
  "status": "RUNNING",
  "payload": {},
  "created_at": "2026-08-06T00:00:00+00:00"
}
~~~

同一 run_id/agent_id/epoch 的 seq 单调递增；完全相同的重复事件必须幂等，旧 epoch 事件不得覆盖新 epoch。heartbeat 表示 runtime 存活，progress 表示阶段推进，observation 表示 Leader 完成一个等待窗口；不能把没有 progress 或一次等待超时当作 Agent 失败。

## 证据分类

- `FACT`：可直接从代码、测试、配置或日志确认。
- `INFERENCE`：由事实支持的推断。
- `ASSUMPTION`：尚未验证的必要假设。
- `UNKNOWN`：当前信息不足。

每条 finding 必须把事实与推断分开。理论最坏情况不能替代真实可达路径。

## PR 变更归因

Finding 的“问题存在”与“由当前 PR 负责”是两个独立命题。A、B 和 Leader 都必须用相同 base/head 独立验证归因；三方中的任一环节都不得用“当前 head 存在问题”、行号位于 diff、PR 触达了相邻代码或 A/B 已共识来替代因果证明。

候选先分类：

- `INTRODUCED`：问题在 base 不存在，当前 diff 直接创建了触发路径或错误行为；
- `WORSENED`：相关缺陷在 base 已存在，但当前 diff 可证明地扩大了入口、影响范围或失败强度；
- `CONTRACT_INCOMPLETE`：当前 diff 通过代码、API、规范或测试建立了具体的新行为承诺，但 head 未完整实现；
- `PRE_EXISTING`：base 与 head 的问题行为实质相同；
- `TOUCHED_ONLY`：PR 只改变调用点、日志、预算透传、文档或邻近代码，问题根因和可达性没有因当前 diff 改变；
- `ATTRIBUTION_UNCLEAR`：无法用当前证据确定归因。

只有前三类可成为 finding。后三类立即淘汰，不进入 packet、报告、统计或发布。`CONTRACT_INCOMPLETE` 必须给出代码/API/规范/测试中的具体 `scope_obligation`；PR 标题或描述中的宽泛目标单独不足。若问题位置在未改动代码，仍须给出当前 diff 中的 `causal_hunks` 并说明它如何创建责任；找不到因果 hunk 就不能报告。`git blame` 和历史提交只能辅助定位，不能代替 base/head 行为比较。

## 严重级别

- `Critical`：现实可达，可能造成灾难性、广泛或不可逆影响，且无有效缓解。
- `High`：触发条件现实，可能造成重大安全、数据、可用性或业务影响。
- `Medium`：问题真实但影响受限、需要较强前置条件或存在有效缓解。
- `Low`：影响轻微、局部、可恢复，但仍是具体正确性或稳健性问题。
- `Suggestion`：改进建议，不作为缺陷 finding。

严重级别与置信度分开。置信度只用 `high`、`medium`、`low`。

## Finding 字段

每条新 finding 至少包含：

```json
{
  "finding_id": "F-001",
  "revision": 1,
  "title": "简洁、可验证的问题标题",
  "category": "correctness | security | reliability | concurrency | performance | compatibility | tests | other",
  "locations": [{"path": "src/file.ext", "line_start": 10, "line_end": 12, "symbol": "optional"}],
  "change_attribution": {
    "classification": "INTRODUCED | WORSENED | CONTRACT_INCOMPLETE",
    "base_behavior": "base 中同一路径的可观察行为",
    "head_behavior": "当前 head 中新增或恶化的可观察行为",
    "causal_hunks": [{"path": "src/file.ext", "line_start": 10, "line_end": 12}],
    "causal_link": "该 hunk 如何导致新增、恶化或承诺未完成",
    "scope_obligation": "CONTRACT_INCOMPLETE 时必填；其他分类可为 null"
  },
  "claim": "当前代码为什么错误",
  "expected_behavior": "正确行为",
  "evidence": [{"type": "FACT", "detail": "具体证据"}],
  "execution_path": ["入口", "关键分支", "问题点"],
  "trigger_conditions": ["必要条件"],
  "existing_controls": ["已检查的防护及其有效性"],
  "impact": "可观察后果与范围",
  "severity": "Critical | High | Medium | Low | Suggestion",
  "severity_rationale": "影响和可能性依据",
  "confidence": "high | medium | low",
  "verification": "复现、测试或反例方法",
  "remediation_direction": "修复方向而非大段补丁",
  "open_questions": []
}
```

行号以当前 head 为准。finding_id 在全部轮次和后续复检中保持稳定，内容变化时递增 revision；修复状态变化不创建新的 finding_id。

分布式 finding 还必须把副本/owner/lease/重试/滚动升级等必要前置条件写入 `trigger_conditions`，把跨组件路径写入 `execution_path`，并在 `existing_controls` 中明确现有事务、fencing、幂等、readiness 或恢复机制为何不足。

## 首次/增量新 finding packet

A 初始包：

```json
{
  "packet_type": "A_INITIAL",
  "round": 0,
  "run": {},
  "model": {"requested": "...", "effective": "... | UNKNOWN", "reasoning_requested": "...", "reasoning_effective": "... | UNKNOWN", "fork_turns": "none"},
  "scope": {
    "repository": "...",
    "base": "...",
    "head": "...",
    "files_reviewed": [],
    "deployment_context": {
      "topology": "single_node | distributed | both | unknown",
      "evidence": [],
      "affected_components": [],
      "risk_areas_checked": [],
      "distributed_impact": "NONE | SAFE_WITH_EVIDENCE | RISK_IDENTIFIED | UNVERIFIED",
      "limitations": []
    },
    "limitations": []
  },
  "findings": []
}
```

B 复核包：

```json
{
  "packet_type": "B_VERIFICATION",
  "round": 1,
  "run": {},
  "model": {},
  "reviews": [{
    "finding_id": "F-001",
    "revision_reviewed": 1,
    "validity": "CONFIRMED | PARTIALLY_CONFIRMED | REJECTED | INSUFFICIENT_EVIDENCE",
    "attribution_decision": "CONFIRMED | REJECT_PRE_EXISTING | REJECT_TOUCHED_ONLY | INSUFFICIENT_EVIDENCE",
    "attribution_evidence": "B 独立比较 base/head 后的决定性依据",
    "evidence_check": "...",
    "counterevidence": [],
    "severity_decision": "MAINTAIN | UPGRADE | DOWNGRADE | NOT_APPLICABLE | UNDETERMINED",
    "suggested_severity": "High",
    "rationale": "...",
    "requested_verification": ["可执行的补证动作"],
    "confidence": "high | medium | low",
    "proposed_status": "AGREED | REVISION_REQUIRED | DISPUTED | CLOSED_REJECTED"
  }],
  "supplementary_findings": []
}
```

A 复查包：

```json
{
  "packet_type": "A_RECHECK",
  "round": 1,
  "run": {},
  "model": {},
  "responses": [{
    "finding_id": "F-001",
    "response": "ACCEPT | PARTIAL_ACCEPT | REJECT_WITH_EVIDENCE | NEED_MORE_EVIDENCE | WITHDRAW",
    "new_revision": 2,
    "new_evidence": [],
    "rationale": "...",
    "current_validity": "CONFIRMED | PARTIALLY_CONFIRMED | REJECTED | INSUFFICIENT_EVIDENCE",
    "current_severity": "High",
    "consensus": false,
    "final_technical_position": null
  }]
}
```

## `A_RECHECK` 状态转移表

response、current_validity、consensus 与 final_technical_position 是一个不可拆分的状态机，不得自由组合：

| response | current_validity | consensus | final_technical_position | Leader 收束方向 |
| --- | --- | --- | --- | --- |
| `ACCEPT` | `CONFIRMED` / `PARTIALLY_CONFIRMED` | `true` | 必须为空 | `AGREED` |
| `PARTIAL_ACCEPT` | `CONFIRMED` / `PARTIALLY_CONFIRMED` | `false` | 必填 | 无新实质证据时 `FINAL_BY_A`；否则 `DISPUTED` |
| `REJECT_WITH_EVIDENCE` | `CONFIRMED` / `PARTIALLY_CONFIRMED` | `false` | 必填 | 无新实质证据时 `FINAL_BY_A`；否则 `DISPUTED` |
| `NEED_MORE_EVIDENCE` | `INSUFFICIENT_EVIDENCE` | `false` | 必须为空 | `DISPUTED`，完成最小补证后再继续 |
| `WITHDRAW` | `REJECTED` | `true` | 必填 | `CLOSED_REJECTED`；最终立场记录撤回依据 |

当 A 决定维持或撤回 finding 并结束当前分歧时，`A_RECHECK` response 必须填写 `final_technical_position`；不能等到固定轮次才给出终审立场。`NEED_MORE_EVIDENCE` 不得伪装成最终立场。`ACCEPT` 与 `WITHDRAW` 表示双方已就该 finding 的处置达成一致；`PARTIAL_ACCEPT` 与 `REJECT_WITH_EVIDENCE` 表示 A 仍保留自己的最终技术立场，因此必须保持 `consensus=false`。

## 共识与结束

首次检视中，只有对问题有效性、PR 变更归因、位置、核心触发条件、主要影响和严重级别实质一致时标记 `AGREED`。修复方案文字不同不自动构成分歧。Leader 必须在最终裁决前验证 `change_attribution`；即使 A/B 都确认，归因字段缺失、因果 hunk 不成立或 base/head 行为没有实质差异时也必须 `CLOSED_REJECTED`。

- B 确认且 A 维持：`AGREED`。
- A 接受 B 的修订、升级或降级：`AGREED`。
- A 撤回且 B 驳回：`CLOSED_REJECTED`。
- B 的 supplementary finding 被 A 以证据驳回：`CLOSED_REJECTED`，保留 B 的理由但不作为 actionable finding。
- A 已直接回应 B 的最强反证、给出 `final_technical_position` 并维持自己的 actionable finding，而 B 没有新的实质证据：立即以 `FINAL_BY_A` 收束，保留并披露 B 异议；不需要为了达到固定轮数继续争论。
- A 撤回自己的 finding，或接受 B 的 supplementary finding 后，按更新后的有效性与级别形成 `AGREED` 或 `CLOSED_REJECTED`，不得把已撤回 finding 作为 `FINAL_BY_A` 发布。
- A 使用 `NEED_MORE_EVIDENCE`、没有回应决定性反证，或双方仍提出尚未验证的新实质证据：`DISPUTED`。只有完成最小补证后才能继续；达到三轮安全上限仍不能裁决时保持 `DISPUTED`，不得用轮数自动生成 `FINAL_BY_A`。

状态层级必须唯一：上述内部 packet/finding-level 状态为 `DISPUTED`；Leader 生成对外 Markdown、飞书摘要和 run 总结时，顶层报告状态映射为 `DISPUTED_OPEN`。不得把 `DISPUTED_OPEN` 回写成 finding 共识，也不得把 `DISPUTED` 误当成可发布决定。

修复复检使用独立的精简契约 `references/fix-verification-contract.md`，不要为普通复检加载本文件的首检 schema。
