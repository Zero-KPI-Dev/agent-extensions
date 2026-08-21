# Platforms

每个 Agent 平台使用独立目录保存自己的安装元数据和适配实现：

```text
platforms/<agent-name>/
├── plugins/
├── skills/
└── <platform-specific metadata>
```

目前已包含 `codex/`。未来可新增 `claude-code/`、`gemini-cli/` 或其他平台；只有真正共享且不含平台语法的内容才放入仓库根目录的 `shared/`。
