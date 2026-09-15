# GitHub Markdown 检视报告模板

## 目录

- [使用规则](#使用规则)
- [状态值](#状态值)
- [固定模板](#固定模板)

## 使用规则

Leader 必须按以下固定顺序生成一份最终 Markdown 报告。A/B 的 JSON packet 只用于内部协作，不直接作为 GitHub 评论发布。

- 保留机器可读的 HTML 元数据注释，供下一次检视建立 lineage；不要放入秘密或 token。
- 保留所有章节；没有内容时写 `无`、`无有效 finding` 或规定的状态值，不要删除章节。
- 每个 finding 使用稳定的 `finding_id`，复检只更新状态和 revision，不改成新的 ID。
- 事实与推断分开标注；位置优先使用指向当前 head 的 GitHub permalink。
- 每个新 actionable finding 必须显示 `INTRODUCED`、`WORSENED` 或 `CONTRACT_INCOMPLETE` 归因，以及 base/head 行为差异和当前 diff 的 causal hunk；仅被触达或顺带发现的既有问题不进入报告。
- `NO_ACTIONABLE_FINDINGS` 不等于绝对无缺陷，必须说明覆盖范围和局限。
- 面向用户或调用方的严重级别总数只统计当前仍需行动的开放 finding；`FIXED_VERIFIED`、`OBSOLETE` 等历史关闭项保留原级别时必须另列为“历史已验证修复”。`FIX_VERIFIED` 和 `NO_ACTIONABLE_FINDINGS` 的当前待处理数量必须全部为 0。
- `INITIAL_REVIEW` 使用完整模板。`FIX_VERIFICATION` 和 `INCREMENTAL_REREVIEW` 默认使用后文的复检紧凑模板；只展开未关闭、证据不足或通过 High/Critical 新增意见门禁的项目，不复述旧 finding 全文。
- 复检的 `publish_status` 先取决于授权和实际执行。没有有效发布授权或用户要求 report-only 时记录 `NOT_ATTEMPTED`，交付完整评审。授权有效时，不能只由 actionable finding 数量决定是否发布：GitHub 最近发布的 lifecycle 与本轮不同，尤其迁移为 `FIXED_VERIFIED` 时，应记录为 `PUBLISHED` 或真实的 `FAILED`；状态未变化、同一 `review_id` 已发布或 `NO_NEW_REVISION` 时记录 `SKIPPED`。

## 状态值

- `ACTION_REQUIRED`：存在需要处理的有效 finding。
- `NO_ACTIONABLE_FINDINGS`：本轮没有可执行 finding。
- `FIX_VERIFIED`：既有 finding 已由 A、B 共同验证修复。
- `PARTIALLY_FIXED`：部分修复，仍需处理残留风险。
- `FINAL_BY_A`：首次检视中 A 已回应 B 的最强反证并作出终审；存在 A 确认的 actionable finding，同时保留并披露 B 异议。
- `DISPUTED_OPEN`：A、B 对修复或结论仍有实质分歧，保持开放。
- `NO_NEW_REVISION`：当前版本已经检视过，没有新的待检视版本。

内部 packet 中的 finding-level `DISPUTED` 在对外报告中显示为 finding 当前状态 `DISPUTED`，并将顶层报告状态映射为 `DISPUTED_OPEN`；两者不是两个不同裁决。

## 固定模板

```markdown
<!-- review-pr-with-panel
review_id: {{review_id}}
prior_review_id: {{prior_review_id}}
mode: {{INITIAL_REVIEW | FIX_VERIFICATION | INCREMENTAL_REREVIEW | NO_NEW_REVISION}}
status: {{ACTION_REQUIRED | NO_ACTIONABLE_FINDINGS | FIX_VERIFIED | PARTIALLY_FIXED | FINAL_BY_A | DISPUTED_OPEN | NO_NEW_REVISION}}
repository: {{repository}}
pull_request: {{pull_request_or_none}}
base: {{base}}
previous_head: {{previous_head_or_none}}
current_head: {{current_head}}
rounds: {{rounds}}
github_target: {{github_target_or_none}}
publish_policy: {{AUTO_AFTER_PANEL_DECISION | REPORT_ONLY | NOT_APPLICABLE}}
publish_status: {{NOT_ATTEMPTED | PUBLISHED | FAILED | SKIPPED}}
-->

## 检视结论

> **状态：** `{{status}}`
>
> **摘要：** {{用一到三句话说明结论、主要风险和是否需要行动。}}

## 检视范围

| 项目 | 内容 |
|---|---|
| 模式 | `{{mode}}` |
| 仓库 / PR | `{{repository}}` / `{{pull_request_or_none}}` |
| Base | `{{base}}` |
| Previous head | `{{previous_head_or_none}}` |
| Current head | `{{current_head}}` |
| GitHub 发布 | `{{publish_status}}`；{{published_review_url_or_none}} |
| 模式依据 | {{Leader 的历史和 Git 判断依据}} |
| 部署拓扑 | `{{single_node | distributed | both | unknown}}`；受影响组件：{{affected_components_or_none}} |
| 分布式影响 | `{{NONE | SAFE_WITH_EVIDENCE | RISK_IDENTIFIED | UNVERIFIED}}`；{{关键依据、已检查风险面和多副本验证限制}} |
| 局限 | {{未覆盖内容、缺失环境或证据限制}} |

## Finding 汇总

| ID | 严重级别 | 状态 | 位置 | 置信度 |
|---|---|---|---|---|
| `F-001` | `High` | `OPEN` | [`src/file.ts:42`]({{github_permalink}}) | `high` |

### `F-001` — [High] {{问题标题}}

| 字段 | 内容 |
|---|---|
| 当前状态 | `{{OPEN | FIXED_VERIFIED | PARTIALLY_FIXED | NOT_FIXED | UNVERIFIABLE | OBSOLETE | FINAL_BY_A | DISPUTED}}` |
| Revision | `{{revision}}` |
| 位置 | [`{{path}}:{{line}}`]({{github_permalink}}) |
| 变更归因 | `{{INTRODUCED | WORSENED | CONTRACT_INCOMPLETE}}` |
| 置信度 | `{{high | medium | low}}` |

**结论**

{{用一句话说明问题或修复验证结论。}}

**事实证据**

- `[FACT]` {{可以直接从代码、测试、配置或日志确认的事实。}}
- `[FACT]` {{第二条事实；没有则删除该行。}}

**PR 归因证据**

- Base 行为：{{base 中同一路径的可观察行为。}}
- Head 行为：{{当前 head 的新增或恶化行为。}}
- 因果 hunk：[`{{changed_path}}:{{changed_line}}`]({{github_diff_permalink}}) — {{该变更如何建立因果关系。}}
- Scope obligation：{{仅 CONTRACT_INCOMPLETE 必填；否则写“不适用”。}}

**推断与影响**

- `[INFERENCE]` {{由事实推出的行为、影响和范围。}}

**执行路径与触发条件**

`{{入口}}` → `{{关键分支}}` → `{{问题点或修复点}}`

触发条件：{{输入、状态、权限、部署或环境前置条件。}}

**已检查的防护**

{{列出校验、过滤、事务、回滚、重试、权限控制或测试，以及它们是否足够。}}

**修复方向 / 验证建议**

{{给出修复方向；修复复检时说明已验证的测试、反例或最小补证动作。}}

## 非 inline Finding

{{列出无法绑定到当前 PR diff 行的 finding_id、原因和完整结论；没有则写“无”。}}

## 未解决分歧

{{列出 finding_id、A 与 B 的分歧、各自证据和当前处理状态；`FINAL_BY_A` 必须写明 A 的最终技术立场、B 的最强反证及其未改变结论的原因，并标注“非共识结论”；没有则写“无”。}}

## 协作记录

无论首检还是复检，协作记录都必须使用下方 Markdown 表格，不得压缩成单行文本。展示模型组合时遵循：成功且 `effective=requested` 写为 `` `model / reasoning`（已按请求执行）``；只有发生覆盖或降级时才写 `` `requested` → `effective` `` 并给出原因。人类可读报告不显示 `UNKNOWN / UNKNOWN`；执行信息确实缺失时改用中文说明具体缺口。

epoch、lease、heartbeat、settle、cancel 和 packet 校验过程属于内部调度日志，正常完成时不要写入协作记录。只有它们确实降低了结论可靠性或导致失败时，才增加一行“运行异常 / 局限”，用一句面向用户的中文说明影响，不展开内部时序。

| 项目 | 内容 |
|---|---|
| Agent A | {{agent_a_model_execution_summary}} |
| Agent B | {{agent_b_model_execution_summary}} |
| Agent fork | `fork_turns={{none}}`；{{model_override_error_or_none}} |
| Profile | `{{quality | balanced | economy | custom}}` |
| 使用轮数 | `{{rounds}}` |
| Leader 选择理由 | {{模型、推理强度和模式选择理由。}} |
| GitHub 发布 | `{{publish_status}}` · `{{COMMENT_or_none}}` · inline `{{inline_count}}` |
```

## 复检紧凑模板

复检仍保留五个固定章节，但将重复信息压缩为生命周期表。只有门禁通过的新 finding 才追加完整 finding 区块。

```markdown
<!-- review-pr-with-panel
review_id: {{review_id}}
prior_review_id: {{prior_review_id}}
mode: {{FIX_VERIFICATION | INCREMENTAL_REREVIEW | NO_NEW_REVISION}}
status: {{FIX_VERIFIED | PARTIALLY_FIXED | DISPUTED_OPEN | NO_NEW_REVISION}}
repository: {{repository}}
base: {{base}}
previous_head: {{previous_head}}
current_head: {{current_head}}
rounds: {{rounds}}
publish_status: {{NOT_ATTEMPTED | PUBLISHED | FAILED | SKIPPED}}
-->

## 检视结论

> **状态：** `{{status}}` — {{一句话说明是否允许关闭以及剩余风险。}}

## 检视范围

`{{previous_head}}..{{current_head}}`；仅验证 `{{finding_ids}}` 的原触发路径、关闭条件和直接回归。部署拓扑：`{{topology}}`；分布式影响：`{{distributed_impact}}`；受影响组件：{{affected_components_or_none}}。局限：{{limitations_or_none}}。

## Finding 汇总

| ID | 原级别 | 当前状态 | 决定性证据 | 残留风险 |
|---|---|---|---|---|
| `F-001` | `High` | `FIXED_VERIFIED` | `[FACT]` {{一条决定性事实}} | 无 |

{{只对 NOT_FIXED、PARTIALLY_FIXED、UNVERIFIABLE、DISPUTED_OPEN 或通过门禁的新 High/Critical finding 添加简短展开；已关闭项不再复述原意见。}}

## 未解决分歧

{{仅列未决 finding、A/B 各自新增证据和最小补证动作；没有则写“无”。}}

## 协作记录

| 项目 | 内容 |
|---|---|
| Agent A | {{agent_a_model_execution_summary_or_not_started}} |
| Agent B | {{agent_b_model_execution_summary_or_not_started}} |
| Agent fork | `fork_turns={{none_or_not_started}}`；{{model_override_error_or_none}} |
| Profile | `{{profile_or_none}}` |
| 使用轮数 | `{{rounds}}` |
| Leader 选择理由 | {{模型、推理强度和模式选择理由；NO_NEW_REVISION 时说明未启动 A/B。}} |
| GitHub 发布 | `{{publish_status}}` · `{{COMMENT_or_none}}` · inline `{{inline_count}}` |
```
