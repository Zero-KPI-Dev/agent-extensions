# Contributing

## 放置规则

- 平台专用内容放在 `platforms/<agent-name>/`。
- 可直接安装的 Codex 插件放在 `platforms/codex/plugins/<plugin-name>/`。
- 单独的 Codex Skill 放在 `platforms/codex/skills/<skill-name>/`。
- 可跨 Agent 复用的源稿和约定放在 `shared/`，平台适配层仍留在各自平台目录。
- 每个扩展必须包含自己的 README、依赖说明、配置示例和卸载方式。

## 安全规则

禁止提交：

- App Secret、Access Token、GitHub Token、私钥或真实账号标识。
- `.env`、实际 `config.json`、SQLite 数据库、日志、PID、Socket 和虚拟环境。
- `/Users/<name>/` 等个人绝对路径；测试夹具中的明确假路径除外。
- Codex 插件缓存目录。只提交扩展源代码。

配置示例必须使用 `replace_me`、`owner/repo` 等明显占位值。

## Codex 插件发布清单

1. 保持 `.codex-plugin/plugin.json`、插件目录名和 marketplace 名称一致。
2. 新插件追加到 `platforms/codex/.agents/plugins/marketplace.json`。
3. 运行仓库检查和插件测试。
4. 更新插件版本和变更说明。
5. 在全新目录中走一遍 README 安装流程。

## 本地验证

```bash
python3 scripts/validate_repository.py

cd platforms/codex/plugins/feishu-pr-review
python3 -m unittest discover -s tests

cd skills/review-pr-with-panel
python3 -m unittest discover -s tests
```
