---
name: review-pr-with-panel
description: >
  Review a GitHub pull request, branch, commit, patch, or working-tree diff with a read-only Leader plus independent A/B verification. Use for initial reviews and follow-up fix verification. Optimize follow-up reviews for low token use by reusing prior finding lineage, inspecting only relevant revision deltas, and suppressing new review opinions unless a newly introduced High/Critical defect blocks functional delivery or threatens system stability. Do not use to implement fixes or present one Agent's self-review as independent verification.
---

# PR 检视专家团

让当前 Codex 主线程担任 Leader；只创建两个只读子 Agent A/B。不得另建 Leader，也不得把同一 Agent 的自我反思称为独立复核。

## 按需读取资源

不要在启动时无条件加载所有 reference。

1. 先读 `references/review-modes.md`，确定模式并检查是否为 `NO_NEW_REVISION`。
2. 仅当需要启动 A/B 时，读 `references/runtime-coordination.md` 和 `references/model-policy.md`。`INITIAL_REVIEW` 读 `references/review-contract.md`；复检读更短的 `references/fix-verification-contract.md`，只有门禁通过的新候选需要完整 Finding 字段时才再读 `references/review-contract.md`。
3. 在启动 A 前读 `references/agent-a.md`；A packet 通过门禁后、启动 B 前再读 `references/agent-b.md`。
4. 仅在生成最终报告时读 `references/report-template.md`。
5. 仅在输入含可解析的 GitHub PR URL 且允许发布时读 `references/github-publish.md`。

选中某个 reference 后完整读取。`NO_NEW_REVISION` 不启动 A/B，也不加载 Agent、模型和 runtime 细则。

## 确认对象与历史

1. 用只读 Git 检查确认仓库绝对路径、base、当前 head、授权变更集、排除项和用户关注点。
2. 有历史检视时，优先复用最近一轮机器可读的 `review_id`、repository、base/head、finding_id、revision、状态和关键证据；不要把整段会话或完整旧报告重复放进 Agent prompt。
3. 用 `previous_head..current_head` 判断变更是否针对旧 finding、是否有独立新增范围，以及旧触发路径是否仍能映射。
4. 缺少可靠 SHA、仓库身份或 finding_id 时，不凭“之前看过”自动关闭旧 finding。
5. 只检视用户授权的变更。可以读取未改动代码验证调用链，但不得混入无关旧问题。
6. 不修改代码、不提交代码；只有 GitHub 自动发布规则允许外部写入。

### GitHub PR task 命名

对可解析的 GitHub PR URL，在启动 A/B 前运行 `python3 scripts/pr_session_title.py '<PR_URL>'`，并用当前 task 重命名能力将标题设为 `<repository>#<number>`。记录 `RENAMED` 或 `SKIPPED_TOOL_UNAVAILABLE`；工具不可用不算 review failure。非 PR 输入不重命名。

## 模式与复检范围冻结

Leader 按 `references/review-modes.md` 独立选择：

- `INITIAL_REVIEW`：无可靠历史，或用户明确要求完整重检。
- `FIX_VERIFICATION`：当前 revision 主要用于处理可追踪的旧 finding。
- `INCREMENTAL_REREVIEW`：既修复旧 finding，又含独立的新实质变更。
- `NO_NEW_REVISION`：head 未变化；直接报告，不启动 A/B。

### 复检证据包

对 `FIX_VERIFICATION` 及 `INCREMENTAL_REREVIEW` 的旧 finding 轨道，Leader 只构造一次最小证据包并复用于 A/B：

- 旧 finding 的 ID、revision、原触发路径、预期行为、严重级别和关闭条件；
- previous/current head 与只关联该 finding 的 changed hunks、当前符号和定向测试；
- 必要的调用者、防护和未决问题。

默认不传完整旧报告、完整会话、无关文件或整个旧/新源码快照。只有无法验证原路径时才按需扩读；不要因为“可能有用”扩大范围。

### 复检新增意见门禁

在任何复检模式中默认冻结 finding 集合：A/B 的首要任务是更新旧 finding 生命周期，不是重新做一次首检。用户明确要求完整重检时改用 `INITIAL_REVIEW`；普通复检不得新增检视意见，只有下述重大问题例外。

只有同时满足以下条件，才允许把复检中新发现的问题作为新 finding：

1. 由当前 revision 新引入或实质恶化，并能定位到当前变更；
2. 有直接证据和现实可达路径，不是风格、建议、测试偏好或理论风险；
3. 严重级别为 `High` 或 `Critical`；
4. 会阻断主要功能交付，或显著威胁系统稳定性、可用性或数据完整性。

不满足门禁的候选不进入 packet、最终报告或 GitHub 评论，也不得借“补充意见”绕过。若独立新变更确实需要普通完整检视，Leader 必须说明范围升级为 `INITIAL_REVIEW` 的原因，或先取得用户明确要求；不要在复检中静默扩大任务。

## 启动与协调

需要 A/B 时，按 `references/runtime-coordination.md`：cleanup → create run → register A → A 通过门禁后 register B。把 run_id、state_db、agent_id、epoch 和 event cursor 注入 Agent；运行数据不得写进 Skill 目录、仓库或 worktree。最终报告和发布尝试结束后在 finally 路径关闭 run 并 cleanup。

一次 `wait_agent` 超时只代表等待窗口结束。记录 observation，继续有界等待；不要在首个超时后发送催促 prompt。仅在总预算或 lease 门禁触发时发送一次聚焦的 settle 请求。只有明确 errored、取消确认或 lease/宽限期均失效时才替换 Agent，并递增 epoch。

## 模型与成本策略

按 `references/model-policy.md` 记录 requested/effective 模型及 reasoning。默认使用成本感知的 `balanced` profile；低风险窄复检优先轻量模型和中等推理，高风险关闭决策再升级。用户明确要求最高质量时使用 `quality`。

显式传 `model`/`reasoning_effort` 时必须同时传 `fork_turns: "none"`，并使用自包含 Agent prompt；不得省略 `fork_turns` 或使用 `all` 后再声称模型覆盖生效。若当前 `spawn_agent` 工具元数据列出 Luna，就先真实请求 Luna，不得因为其他工具、旧会话或猜测而预先改用 Terra。只有该次 Luna 调用返回明确的不支持/不可用错误后才按降级链重试，并记录原始错误。不要把完整 prompt 重发给同一 Agent；后续 turn 只发送仍有分歧的 finding ID、新证据和具体问题。

## A/B 编排

### 首次检视

1. A 读取授权 diff，返回 `A_INITIAL`。
2. A packet 通过质量门禁后，B 使用相同代码基准独立验证并返回 `B_VERIFICATION`。
3. 仅把有实质分歧的 finding 发回同一个 A；无新证据时停止争论。
4. 最多三轮 `B → A`；第三轮仍有分歧时保留 B 异议并按 contract 收束。

### 修复复检快路径

1. A 只读取复检证据包和必要代码，返回紧凑的 `A_FIX_VERIFY`。
2. B 独立读取同一证据包及 A 的逐条结论，返回 `B_FIX_VERIFICATION`。
3. A/B 对状态和关闭证据一致时，Leader 直接裁决；不要例行启动 `A_FIX_RECHECK`。
4. 仅当 B 提供会改变状态的反证、双方存在实质分歧、需要最小补证，或 B 首先发现通过门禁的重大新候选时，向同一个 A 发送一次聚焦 delta，返回 `A_FIX_RECHECK` 或对新候选的 `A_RECHECK`。默认最多这一轮，不重复调用 B。
5. 仍有分歧时标记 `DISPUTED_OPEN`；证据不足时标记 `UNVERIFIABLE`。不得为了省成本误标 `FIXED_VERIFIED`。

对多个旧 finding 一次批量验证；后续只传递未决 ID。重大新候选必须由 A/B 双方独立确认才可进入最终报告或 GitHub inline。子 Agent 不得修改文件、提交、实现修复或发布评论。无子 Agent 能力时，明确说明无法形成独立双 Agent 复核，并询问是否接受单 Agent 降级。

## 报告与 GitHub 发布

按 `references/report-template.md` 生成 GitHub 可渲染 Markdown。首检使用完整 finding 模板；复检使用紧凑生命周期表，只展开未关闭、证据不足或通过新增意见门禁的项目。保留机器可读 lineage 和五个固定章节。

对有效 GitHub PR URL，且用户未要求只读/仅报告时，按 `references/github-publish.md` 自动发布：

- 只发布已达成一致的 actionable finding，默认 action 为 `COMMENT`；
- 复检不得为重述旧意见创建新的 inline comment；旧 finding 只在 review body 更新生命周期；
- 只有通过复检新增意见门禁的 High/Critical 新问题或回归才可新增 inline；
- 不自动批准、请求修改或关闭旧 thread；发布失败要报告真实状态。

## 最终裁决

Leader 不按票数裁决，也不发明折中级别。输出模式、base/head、局限、旧 finding 状态和证据、有效新 finding、未决分歧、轮数、requested/effective 模型与选择理由。

无有效 finding 输出 `NO_ACTIONABLE_FINDINGS`；全部旧 finding 共同验证后输出 `FIX_VERIFIED`；部分残留输出 `PARTIALLY_FIXED`；未决分歧输出 `DISPUTED_OPEN`；无新版本输出 `NO_NEW_REVISION`。避免声称绝对无缺陷。

当 packet 已保存为 JSON 时，可运行 `python3 scripts/validate_review_packet.py <packet.json>` 做机械校验；语义仍由 Leader 判断。

## 禁止事项

- 不把复检变成无边界的重新扫描，也不新增 Low/Medium/Suggestion 意见。
- 不把重复旧 finding 包装成“新问题”，不重复发布旧意见。
- 不把等待超时当成 Agent 失败，不把 Leader observation 当成 heartbeat。
- 不把 SQLite 写进 Skill、仓库或 worktree。
- 不用 PR 标题或 owner 命名 task。
