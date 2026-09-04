# GitHub PR 自动发布规则

## 触发条件

当用户输入包含可解析的 GitHub Pull Request URL，且没有明确表示“只输出报告”“不要发布”“不要写 GitHub”时，将本次运行的 `publish_policy` 设为 `AUTO_AFTER_PANEL_DECISION`。不需要用户再发送“发布”命令。历史记录中的 `AUTO_AFTER_CONSENSUS` 仅作为兼容值读取。

以下情况不触发自动发布：

- URL 不是 GitHub PR URL，或仓库/PR 号无法解析；
- 用户明确选择只读或仅输出报告；
- GitHub 连接器不可用、未授权或目标无法确认；
- 结论只有 `DISPUTED` 或证据不足，且没有任何需要同步到 GitHub 的 finding 生命周期变化；
- 当前模式为 `NO_NEW_REVISION`，或 GitHub 已记录相同 head、相同 finding lifecycle 状态，且没有新的共识或 A 终审确认的 actionable finding。

“本轮已关闭全部旧 finding”本身不是跳过理由。若 GitHub 最近一份机器可读 review 仍记录 `PARTIALLY_FIXED`、`NOT_FIXED`、`UNVERIFIABLE` 或其他非终态，而本轮 A/B 已将其更新为 `FIXED_VERIFIED`，必须发布一次新的 `COMMENT` review body，使 GitHub 上的 lineage 到达终态；即使 actionable finding 数量为 0、inline 数量为 0，也不能以“无可行动问题”为由设为 `SKIPPED`。

### 复检生命周期发布优先级

按以下顺序裁决，前项优先于后项：

1. GitHub 最近一份机器可读 review 与本轮 finding lifecycle 不同：发布新的 `COMMENT` review body，沿用稳定 finding ID，记录本轮 `review_id` 和 current head；旧 finding 不新增 inline。
2. 非终态迁移为 `FIXED_VERIFIED`：属于必须发布的 lifecycle 终态更新，优先级高于“没有 actionable finding”。
3. 同一 `review_id` 已发布，或 GitHub 已记录相同 current head 与相同 lifecycle 状态：跳过，避免重复 review。
4. `NO_NEW_REVISION`，或 lifecycle 没有变化且没有新的可发布 finding：跳过。

## 发布前检查

1. 解析 `owner/repo`、PR number 和当前 head SHA。
2. 读取 PR 元数据和当前 diff；inline 位置必须以当前 PR diff 为准。
3. 生成 `references/report-template.md` 规定的完整 Markdown 报告。
4. 用 `review_id` 和隐藏元数据标识检查该 run 是否已经发布，避免重复评论。
5. 复检先比较 GitHub 最近一份机器可读 review 与本轮 finding lifecycle；有状态变化时按上述优先级发布 review body，不能只检查 actionable finding 数量。
6. 发布已经达到共识的 finding，以及满足下述门禁的 `FINAL_BY_A` 的 A-owned actionable finding；`DISPUTED`、`DISPUTED_OPEN`（分别为 finding-level 与顶层报告状态）、`INSUFFICIENT_EVIDENCE` 和 `CLOSED_REJECTED` 都不作为 actionable inline comment 发布。
7. 不得把旧意见的重述或生命周期更新再次发布为 inline comment。

### `FINAL_BY_A` 发布门禁

`FINAL_BY_A` 只适用于 A 在首次检视中提出并负责的 finding，不适用于 B 的 supplementary finding，也不替代复检新增 High/Critical finding 必须由 A/B 共同确认的门禁。只有同时满足以下条件才可发布：

1. A 已直接回应 B 的最强反证，并在 `final_technical_position` 中明确维持 finding 的有效性、触发路径和严重级别；
2. A 的立场由当前代码、测试、配置或可复现行为等直接证据支持，不是重复原主张或仅凭角色权威；
3. B 没有尚未处理的新实质证据；若 A 使用 `NEED_MORE_EVIDENCE` 或未回应决定性反证，状态必须保持 `DISPUTED`；
4. review body 和对应 inline 必须明确标注“`FINAL_BY_A`，非 A/B 共识”，披露 B 的异议、反证摘要以及 A 为什么仍维持结论；
5. 默认 action 仍为 `COMMENT`，不得据此自动 `APPROVE`、`REQUEST_CHANGES` 或关闭 thread。

B 的 supplementary finding 被 A 以证据驳回时标记 `CLOSED_REJECTED`，只在协作记录中保留 lineage，不发布为 actionable finding。A 接受该补充问题后，仍需按正常共识或复检新增意见门禁处理。

## Inline 与汇总评论

使用 GitHub `add_review_to_pr`，默认 action 为 `COMMENT`。不要因为检视结论自动 `APPROVE` 或 `REQUEST_CHANGES`；除非用户另有明确策略。

对每个 finding 采用以下规则：

- finding 能绑定到当前 PR diff 中的新增或修改行，且有明确 `path`、`line`、`side=RIGHT`：放入 `file_comments`，发布为 inline comment。
- finding 跨文件、针对整体行为、无法绑定到当前 diff 行，或 GitHub 拒绝该位置：放入 review body 的 `## 非 inline Finding` 章节。
- inline comment 使用稳定的 `review_id` 和 `finding_id`，包含严重级别、结论、事实证据、影响和修复方向；不要只发布一句模糊的“这里有问题”。
- `FINAL_BY_A` inline 除上述内容外，还必须披露 B 的异议并明确说明这是 A 终审结论而非 A/B 共识。
- review body 使用完整固定 Markdown 报告，包含范围、所有 finding 状态、协作记录和局限。

修复复检中，旧 finding 的 `FIXED_VERIFIED`、`NOT_FIXED`、`PARTIALLY_FIXED` 或 `UNVERIFIABLE` 使用稳定 finding ID，在新的 `COMMENT` review body 中更新生命周期摘要，不新增 inline，不把旧 finding 换 ID 后重复发布。只要状态相对 GitHub 最近已发布记录发生变化，就应发布该 lifecycle 更新；其中非终态到 `FIXED_VERIFIED` 的迁移必须发布。只有当前 revision 新引入或实质恶化、经 A/B 共同确认、严重级别为 `High`/`Critical`，且会阻断主要功能交付或显著威胁系统稳定性、可用性或数据完整性的新问题/回归，才允许新增 inline。不要自动解析或关闭旧 review thread，除非用户明确要求。

## 发布结果与失败处理

发布成功后，在最终回答中记录 GitHub review URL、action、inline 数量、汇总 finding 数量和跳过的分歧项。

发布失败时不得假装成功，也不要盲目重复写入。报告具体失败原因、已经完成的本地检视和可安全重试的下一步；若无法确认是否已写入，先读取同一 `review_id` 的评论/评审再决定是否重试。
