# GitHub PR 自动发布规则

## 触发条件

当用户输入包含可解析的 GitHub Pull Request URL，且没有明确表示“只输出报告”“不要发布”“不要写 GitHub”时，将本次运行的 `publish_policy` 设为 `AUTO_AFTER_CONSENSUS`。不需要用户再发送“发布”命令。

以下情况不触发自动发布：

- URL 不是 GitHub PR URL，或仓库/PR 号无法解析；
- 用户明确选择只读或仅输出报告；
- GitHub 连接器不可用、未授权或目标无法确认；
- 结论只有 `DISPUTED`、`FINAL_BY_A` 或证据不足，没有任何已达成一致的 actionable finding。

## 发布前检查

1. 解析 `owner/repo`、PR number 和当前 head SHA。
2. 读取 PR 元数据和当前 diff；inline 位置必须以当前 PR diff 为准。
3. 生成 `references/report-template.md` 规定的完整 Markdown 报告。
4. 用 `review_id` 和隐藏元数据标识检查该 run 是否已经发布，避免重复评论。
5. 仅发布已经达到共识的 finding；`DISPUTED`、`INSUFFICIENT_EVIDENCE` 和 `CLOSED_REJECTED` 不作为 actionable inline comment 发布。
6. 复检先检查 finding lineage 和历史 review；不得把旧意见的重述或生命周期更新再次发布为 inline comment。

## Inline 与汇总评论

使用 GitHub `add_review_to_pr`，默认 action 为 `COMMENT`。不要因为检视结论自动 `APPROVE` 或 `REQUEST_CHANGES`；除非用户另有明确策略。

对每个 finding 采用以下规则：

- finding 能绑定到当前 PR diff 中的新增或修改行，且有明确 `path`、`line`、`side=RIGHT`：放入 `file_comments`，发布为 inline comment。
- finding 跨文件、针对整体行为、无法绑定到当前 diff 行，或 GitHub 拒绝该位置：放入 review body 的 `## 非 inline Finding` 章节。
- inline comment 使用稳定的 `review_id` 和 `finding_id`，包含严重级别、结论、事实证据、影响和修复方向；不要只发布一句模糊的“这里有问题”。
- review body 使用完整固定 Markdown 报告，包含范围、所有 finding 状态、协作记录和局限。

修复复检中，旧 finding 的 `FIXED_VERIFIED`、`NOT_FIXED`、`PARTIALLY_FIXED` 或 `UNVERIFIABLE` 只更新同一份 review body 的生命周期摘要，不新增 inline，不把旧 finding 换 ID 后重复发布。只有当前 revision 新引入或实质恶化、经 A/B 共同确认、严重级别为 `High`/`Critical`，且会阻断主要功能交付或显著威胁系统稳定性、可用性或数据完整性的新问题/回归，才允许新增 inline。不要自动解析或关闭旧 review thread，除非用户明确要求。

## 发布结果与失败处理

发布成功后，在最终回答中记录 GitHub review URL、action、inline 数量、汇总 finding 数量和跳过的分歧项。

发布失败时不得假装成功，也不要盲目重复写入。报告具体失败原因、已经完成的本地检视和可安全重试的下一步；若无法确认是否已写入，先读取同一 `review_id` 的评论/评审再决定是否重试。
