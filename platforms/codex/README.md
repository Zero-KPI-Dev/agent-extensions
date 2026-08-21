# Codex Extensions

本目录本身就是一个 Codex 团队 marketplace。首次安装：

```bash
cd agent-extensions
codex plugin marketplace add "$PWD/platforms/codex"
codex plugin add feishu-pr-review@zero-kpi-dev
```

## 配置 Feishu PR Review

从仓库根目录执行：

```bash
FEISHU_PLUGIN_ROOT="$PWD/platforms/codex/plugins/feishu-pr-review"

python3 -m pip install -r "$FEISHU_PLUGIN_ROOT/requirements-long-connection.txt"
python3 "$FEISHU_PLUGIN_ROOT/scripts/configure.py" bot add pr-review
python3 "$FEISHU_PLUGIN_ROOT/scripts/configure.py" repo set owner/repo /absolute/path/to/repository
python3 "$FEISHU_PLUGIN_ROOT/scripts/configure.py" repo default owner/repo
python3 "$FEISHU_PLUGIN_ROOT/scripts/install_launchd.py" --load
python3 "$FEISHU_PLUGIN_ROOT/scripts/doctor.py"
```

飞书开发者后台需要为应用启用机器人能力、订阅 `im.message.receive_v1`，并选择“使用长连接接收事件”。长连接方式不需要公网地址，也不需要手工配置机器人 Open ID。

真实配置会保存在：

```text
~/Library/Application Support/Codex/feishu-pr-review/config.json
```

它不在 Git 仓库内。不要复制其他人的配置文件；每台独立网关应运行自己的配置向导。

完整使用方法、健康检查、卸载和故障排查见 [插件 README](plugins/feishu-pr-review/README.md)。

安装或更新后请重启 Codex，并新建一个任务加载新版本。
