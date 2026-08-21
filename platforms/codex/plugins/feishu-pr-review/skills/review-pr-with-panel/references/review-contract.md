# 检视契约

## 目录

- [Packet 与最终报告边界](#packet-与最终报告边界)
- [Run manifest](#run-manifest)
- [证据分类](#证据分类)
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
  "publish_policy": "AUTO_AFTER_CONSENSUS | REPORT_ONLY | NOT_APPLICABLE",
  "publish_status": "NOT_ATTEMPTED | PUBLISHED | FAILED | SKIPPED",
  "mode_reason": "可验证的历史和 Git 依据",
  "mode_confidence": "high | medium | low"
}
```

复检 run 还应记录 `recheck_scope_policy: "FROZEN_HIGH_CRITICAL_ONLY"`。它表示默认只更新旧 finding 生命周期；不是新增普通意见的授权。

`previous_head`、`current_head` 和 `finding_id` 是修复复检建立 lineage 的最低要求。没有可靠 lineage 时不得自动关闭旧 finding。若存在 GitHub PR URL，还要保留 `github_target`、`publish_policy` 和 `publish_status`，以便避免重复发布并追踪外部状态。

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

## 首次/增量新 finding packet

A 初始包：

```json
{
  "packet_type": "A_INITIAL",
  "round": 0,
  "run": {},
  "model": {"requested": "...", "effective": "... | UNKNOWN", "reasoning_requested": "...", "reasoning_effective": "... | UNKNOWN", "fork_turns": "none"},
  "scope": {"repository": "...", "base": "...", "head": "...", "files_reviewed": [], "limitations": []},
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

round=3 时，仍有分歧的 `A_RECHECK` response 必须填写 `final_technical_position`。

## 共识与结束

首次检视中，只有对问题有效性、位置、核心触发条件、主要影响和严重级别实质一致时标记 `AGREED`。修复方案文字不同不自动构成分歧。

- B 确认且 A 维持：`AGREED`。
- A 接受 B 的修订、升级或降级：`AGREED`。
- A 撤回且 B 驳回：`CLOSED_REJECTED`。
- 事实、可达性、影响或级别仍冲突：`DISPUTED`。
- 三轮后仍冲突：`FINAL_BY_A`，保留 B 异议。

修复复检使用独立的精简契约 `references/fix-verification-contract.md`，不要为普通复检加载本文件的首检 schema。
