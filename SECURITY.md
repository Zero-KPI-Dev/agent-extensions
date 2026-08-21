# Security

即使仓库是私有的，也不得把真实凭证或本机运行状态提交进来。

## 当前插件的敏感数据位置

`feishu-pr-review` 的真实配置和运行数据位于使用者本机：

```text
~/Library/Application Support/Codex/feishu-pr-review/
```

其中可能包含飞书 App Secret、任务记录和日志。整个目录都不属于本仓库，也不应复制到 Issue、PR 或聊天记录中。

GitHub 和 Codex 的认证由每个使用者自己的客户端管理，不应写入插件配置。

## 使用建议

- 每位独立运行网关的同事使用自己的飞书应用和凭证。
- 团队共用一个飞书机器人时，只允许一台指定主机运行该机器人的长连接网关。
- 怀疑凭证泄露时，先在对应平台吊销或轮换凭证，再清理 Git 历史；仅删除最新提交并不能消除泄露。
- 提交前运行 `python3 scripts/validate_repository.py`，并人工检查 `git diff --cached`。

发现安全问题时，请通过组织内部安全渠道报告，不要创建包含真实凭证的公开 Issue。
