---
name: feishu-pr-review
description: 管理本机飞书 PR 检视网关，查询、提交、取消或重试异步检视任务。
---

# Feishu PR Review

这个插件把飞书当作异步入口，把本机常驻网关当作队列和回传层。真正的 PR 检视必须交给本插件内置的 `review-pr-with-panel` Skill，不要在这个入口 Skill 中复制检视逻辑，也不要实现修复。

网关支持多个可命名机器人。每个机器人使用独立的飞书凭证和传输方式；默认使用飞书长连接，不需要公网地址，SDK 会在连接后自动解析机器人 Open ID。只有 `webhook` 机器人需要手工配置机器人 Open ID 和事件路径。配置文件变化会自动热加载并重建长连接。

## 触发方式

用户在飞书群里 `@` 机器人并发送 GitHub PR URL，或发送默认仓库的 `PR 314`、`#314`、`pr314` 后，网关会：

1. 立即回传已受理消息和任务短 ID。
2. 在本机持久化队列中通过与 Codex App 共用的 Unix socket App Server 执行 turn；同一 PR 的首次任务创建持久化 thread，后续检视/复检优先按已保存的 `codex_thread_id` 续接尚未归档的 task。若用户已归档旧 task，则尊重归档意图，不自动 `unarchive`，而是为新请求创建新 task；旧 thread 确认不可恢复时也新建。
3. 要求 Codex 使用 `review-pr-with-panel` 的完整 Leader + A/B 流程。
4. 任务完成后只把最终摘要回传飞书，并保留 GitHub PR 上按下方规则发布的检视意见；不要发送中间进度消息。存在待处理意见的结论若该机器人的 `author_mappings` 已配置 PR 作者，则最终成功卡片同时 `@` 对应飞书用户；`NO_ACTIONABLE_FINDINGS`、`FIX_VERIFIED`/`FIXED_VERIFIED` 等可合入结论若已配置 `merge_maintainers`，则改为 `@` 当前仓库的合入者，且不得回退提醒 PR 作者。人员查询或映射缺失不得导致结果回传失败。

用户只 `@` 机器人，或发送 `help`、`帮助`、`怎么用`、询问“能做什么”等使用问题时，网关直接返回帮助卡片，不创建检视任务。无法识别出 PR 的消息也会返回带提示的帮助卡片；包含有效 PR 链接或编号时，检视意图始终优先。

用户发送 `@机器人 我的 Open ID` 时，返回该消息发送者在当前机器人应用下的飞书 Open ID，不创建检视任务。Open ID 与飞书应用绑定，不得跨机器人复用作者映射。

对于有效 GitHub PR URL，发布规则遵循 `review-pr-with-panel`：用户或已明确授权的网关流程允许向该 PR 发布，且用户未要求 report-only 时，发布共识或 A 终审确认的可行动意见；`FINAL_BY_A` 意见必须披露 B 异议。仅提交 PR 评审请求不自动构成发布授权；没有授权时先交付完整报告，按发布规则处理批准。当前 diff 行可定位时使用行内意见，否则使用 review body。不要自动 approve、request changes 或关闭线程。

## MCP 工具

本插件的本机 MCP server 提供：

- `feishu_review_health`：检查配置、网关和队列。
- `feishu_review_status`：查看任务状态和最终摘要。
- `feishu_review_submit`：从 Codex 侧提交任务。
- `feishu_review_cancel`：取消待执行或运行中的任务。
- `feishu_review_retry`：重新排队已结束任务。

机器人配置由 `scripts/configure.py bot add <key>` 管理。作者提醒映射使用：

```bash
python3 scripts/configure.py author set <bot-key> <github-login> <feishu-open-id>
python3 scripts/configure.py author list <bot-key>
python3 scripts/configure.py author remove <bot-key> <github-login>
```

作者映射按机器人保存并自动热加载；不要把凭证写进 Skill、仓库或提交记录。

修复验证通过后的合入者提醒使用：

```bash
python3 scripts/configure.py maintainer set <bot-key> <owner/repo|*> <feishu-open-id> [更多-open-id...]
python3 scripts/configure.py maintainer list <bot-key>
python3 scripts/configure.py maintainer remove <bot-key> <owner/repo|*>
```

仓库专属配置优先于 `*` 默认配置；这些 Open ID 同样按飞书应用隔离并自动热加载。

MCP server 由 Codex 通过 stdio 按需启动；飞书监听网关由 macOS `launchd` 常驻。二者共享用户级 SQLite 状态库，不把任务状态写入仓库、Skill 或插件目录。
