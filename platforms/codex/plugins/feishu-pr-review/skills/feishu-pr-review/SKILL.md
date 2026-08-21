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
2. 在本机持久化队列中通过与 Codex App 共用的 Unix socket App Server 创建可持久化的 thread 并执行 turn；任务记录会保存 `codex_thread_id`，Codex App 会显示完整执行过程。
3. 要求 Codex 使用 `review-pr-with-panel` 的完整 Leader + A/B 流程。
4. 任务完成后只把最终摘要回传飞书，并保留 GitHub PR 上按下方规则发布的检视意见；不要发送中间进度消息。

用户只 `@` 机器人，或发送 `help`、`帮助`、`怎么用`、询问“能做什么”等使用问题时，网关直接返回帮助卡片，不创建检视任务。无法识别出 PR 的消息也会返回带提示的帮助卡片；包含有效 PR 链接或编号时，检视意图始终优先。

对于有效 GitHub PR URL，除非用户明确要求 report-only，发布规则遵循 `review-pr-with-panel`：共识后的可行动意见发布到 GitHub PR；当前 diff 行可定位时使用行内意见，否则使用 review body。不要自动 approve、request changes 或关闭线程。

## MCP 工具

本插件的本机 MCP server 提供：

- `feishu_review_health`：检查配置、网关和队列。
- `feishu_review_status`：查看任务状态和最终摘要。
- `feishu_review_submit`：从 Codex 侧提交任务。
- `feishu_review_cancel`：取消待执行或运行中的任务。
- `feishu_review_retry`：重新排队已结束任务。

机器人配置由 `scripts/configure.py bot add <key>` 管理；不要把凭证写进 Skill、仓库或提交记录。

MCP server 由 Codex 通过 stdio 按需启动；飞书监听网关由 macOS `launchd` 常驻。二者共享用户级 SQLite 状态库，不把任务状态写入仓库、Skill 或插件目录。
