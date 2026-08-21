# Agent Extensions

`Zero-KPI-Dev` 的内部 Agent 扩展仓库，用于集中维护可复用的 Skills、Plugins、Agent 配置和跨平台资源。

仓库只保存源代码、示例配置和安装说明。真实凭证、本机配置、任务数据库、日志和生成缓存不得提交。

## 目录结构

```text
agent-extensions/
├── platforms/
│   ├── README.md
│   └── codex/
│       ├── .agents/plugins/marketplace.json
│       ├── plugins/
│       │   └── feishu-pr-review/
│       └── skills/
├── shared/
│   ├── prompts/
│   └── skills/
├── scripts/
├── CONTRIBUTING.md
└── SECURITY.md
```

- `platforms/<agent>/plugins/`：特定 Agent 平台的插件。
- `platforms/<agent>/skills/`：特定 Agent 平台的 Skills。
- `platforms/<agent>/.agents/`：该平台需要的 marketplace 或发现元数据。
- `shared/`：不绑定某个 Agent 的通用提示词、Skill 源稿、Schema 和参考资料。

新增其他 Agent 时，在 `platforms/<agent-name>/` 下建立独立目录，不要混用 Codex 的 manifest 或 marketplace。

## 安装 Codex 插件

克隆仓库后，把 Codex 子目录登记为团队 marketplace：

```bash
git clone git@github.com:Zero-KPI-Dev/agent-extensions.git
cd agent-extensions

codex plugin marketplace add "$PWD/platforms/codex"
codex plugin add feishu-pr-review@zero-kpi-dev
```

然后按照 [Codex 平台说明](platforms/codex/README.md)配置飞书机器人、代码仓库和本机自启动服务。安装或更新插件后，请重启 Codex，并新建任务加载新版 Skill 和 MCP 工具。

## 更新

```bash
cd agent-extensions
git pull --ff-only
codex plugin add feishu-pr-review@zero-kpi-dev
```

如果更新包含飞书网关代码，再执行：

```bash
python3 platforms/codex/plugins/feishu-pr-review/scripts/install_launchd.py --load
```

## 发布前检查

```bash
python3 scripts/validate_repository.py
```

该检查会验证 marketplace、插件 manifest、禁止提交的运行时文件、真实凭证特征和非测试代码中的个人绝对路径。CI 会重复运行该检查及插件测试。
