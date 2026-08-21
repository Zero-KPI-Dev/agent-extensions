# Feishu PR Review

> 团队仓库用户请先按 [`platforms/codex/README.md`](../../README.md) 安装 Codex marketplace，再执行本页的机器人配置。本文中的 `~/plugins/feishu-pr-review` 表示插件源目录；在团队仓库中对应 `platforms/codex/plugins/feishu-pr-review`。

这是一个本机优先的 Codex 插件：在飞书群里 `@` 机器人并发送 GitHub PR 链接或默认仓库的 PR 号码，机器人先确认收到，再把任务提交到与 Codex App 共用的本机 App Server，调用插件内置的 `review-pr-with-panel` 完成只读检视，最后把结果回传飞书；GitHub 上的可行动检视意见按该 Skill 的发布规则写入 PR。只 `@` 机器人，或询问 `help`、`帮助`、`怎么用` 时，会直接收到使用帮助卡片，不创建检视任务。

## 运行方式

- 飞书网关需要一直保持长连接、维护队列并回传，所以由 macOS `launchd` 自启动并在崩溃后拉起。
- 共享 Codex App Server 由本插件通过 `launchd` 常驻在本机 Unix socket：`~/.codex/app-server-control/app-server-control.sock`。Codex App 和飞书网关连接同一个服务，因此任务会进入同一份 Codex 本地历史；但外部客户端创建的运行中 thread 会由网关持有写入权，Codex App 当前不能实时接管或打开该 turn。
- MCP server 是 Codex 的本机工具入口，Codex 通过 `.mcp.json` 按需以 stdio 启动，不需要单独常驻。
- 完整的 `review-pr-with-panel` Skill 已打包在 `skills/review-pr-with-panel/`，包含运行脚本和全部检视规则，不依赖用户目录下另行安装的 Skill。
- 每个任务都会创建自己的 Codex thread，任务状态会记录 `codex_thread_id`；飞书只收到受理消息和最终检视结果，不接收中间进度。
- 网关和 MCP 共享 `~/Library/Application Support/Codex/feishu-pr-review/state.sqlite3`，后台任务不会因为飞书聊天窗口关闭而丢失。

## 1. 创建配置

第一次使用可以直接运行交互式向导，不需要手工编辑 JSON：

```bash
python3 ~/plugins/feishu-pr-review/scripts/configure.py bot add pr-review
python3 ~/plugins/feishu-pr-review/scripts/configure.py repo set owner/repo ~/Workspaces/your-repository
# 配置多个仓库后，可指定只输入 PR 号时使用的默认仓库
python3 ~/plugins/feishu-pr-review/scripts/configure.py repo default owner/repo
python3 -m pip install -r ~/plugins/feishu-pr-review/requirements-long-connection.txt
```

向导会把配置保存到下面的位置，并用隐藏输入读取 App Secret 和 Verification Token：

`~/Library/Application Support/Codex/feishu-pr-review/config.json`

当只配置一个仓库时，它会自动作为默认仓库；配置多个仓库后，使用 `repo default owner/repo` 指定默认仓库。默认仓库支持只发送 `PR 314`、`#314` 或 `pr314`，完整链接始终可以覆盖默认仓库并避免歧义。

也可以复制 `config.example.json` 后手工修改。配置支持多个机器人，每个机器人有独立的：

- 显示名称、接入方式、事件路径和启用开关。默认接入方式是 `long_connection`，不需要公网地址。
- 飞书 App ID 和 App Secret；长连接会自动解析机器人 Open ID，Webhook 备用模式才需要手工填写 Verification Token 和机器人 Open ID。
- 是否必须 `@` 机器人。

检视 Skill 默认从插件目录自动解析，不需要配置绝对路径。只有开发调试时需要替换 Skill，才使用环境变量 `REVIEW_SKILL_PATH` 临时覆盖；旧配置中的 `~/.codex/skills/review-pr-with-panel/SKILL.md` 会自动迁移到插件内置版本。

查看或修改配置：

```bash
python3 ~/plugins/feishu-pr-review/scripts/configure.py bot list
python3 ~/plugins/feishu-pr-review/scripts/configure.py bot add review-backend
python3 ~/plugins/feishu-pr-review/scripts/configure.py bot remove review-backend
python3 ~/plugins/feishu-pr-review/scripts/configure.py repo list
python3 ~/plugins/feishu-pr-review/scripts/configure.py runtime concurrency 4
```

只有把某个机器人的 `transport` 改成 `webhook` 时，才需要为它配置 `event_path`、公网 HTTPS 回调地址、Verification Token 和机器人 Open ID。长连接机器人不需要配置这些回调字段，也不需要公网 URL。

网关会监测配置文件变化并自动重建长连接；监听端口等进程级参数修改后，重新运行 `install_launchd.py --load` 即可。

不要把真实密钥提交到仓库或插件市场。

## 2. 检查本机

```bash
python3 ~/plugins/feishu-pr-review/scripts/doctor.py
```

如果你使用的 `codex` 不在默认 PATH，可在配置中把 `codex_binary` 写成绝对路径。

默认执行器是 `codex_runner: "app_server"`，并通过 `codex_app_server_transport: "shared_unix"` 接入 Codex App 的共享任务通道。如需临时回退到旧的独立 stdio 通道，可把 transport 改成 `"stdio"`；如需回退到旧的非交互执行方式，再把 `codex_runner` 改成 `"exec"`。

后台默认同时执行 4 个 PR 检视任务，可通过 `runtime concurrency` 在 1 到 8 之间调整。不同 PR 可以并行执行；同一个 PR 已有等待中或执行中的任务时，新请求会合并到原任务，不会创建第二个 Codex 会话。若重复请求来自另一个群，最终结果会同时回传到订阅该任务的群。任务结束后仍可明确发起复检。并发数修改后需要重启飞书网关才会生效。

通过 App Server 创建 Codex task 后，网关会在首个 `turn/start` 成功、rollout 已落盘时调用 `thread/name/set`，按 `review-pr-with-panel` 的规则把侧边栏标题设为 `<Repository>#<PR号>`，例如 `EchoMem#345`。旧版 App Server 不支持该方法时只记录告警，不影响检视执行。

为了让有效 PR URL 的共识意见能够按 Skill 规则发布到 GitHub，默认的 app-server 审批配置是：

```json
{
  "codex_approval_policy": "on-request",
  "codex_approvals_reviewer": "auto_review"
}
```

不要把 `codex_approval_policy` 改成 `"never"`，否则 GitHub 写入类 MCP 调用会被 Codex 直接阻止，并在最终卡片中显示 `approval policy is never`。`auto_review` 只处理 app-server 的审批请求；检视本身仍保持只读，Skill 也不会自动 approve、request changes 或关闭线程。

## 3. 启动本机网关

先生成 launchd 配置：

```bash
python3 ~/plugins/feishu-pr-review/scripts/install_launchd.py
```

确认配置无误后加载并启动网关和共享 App Server：

```bash
python3 ~/plugins/feishu-pr-review/scripts/install_launchd.py --load
```

第一次切换到共享方式，执行完上面的命令后，请从菜单完全退出 Codex App，再重新打开。这样 Codex App 才会连接到同一个本机 App Server；之后飞书触发的任务会出现在任务列表中。任务运行期间打开它可能显示“已在另一个应用中打开”，这是因为网关正在持有该 thread 的写入权；等待任务完成后再打开，必要时重启一次 Codex App。网关和共享 App Server 都由 `launchd` 管理，重启 Codex App 不会关闭它们。

健康检查：

```bash
curl http://127.0.0.1:8787/health
```

日志在：

`~/Library/Application Support/Codex/feishu-pr-review/logs/`

卸载自启动：

```bash
python3 ~/plugins/feishu-pr-review/scripts/install_launchd.py --unload
```

## 4. 配置飞书长连接

在飞书开发者后台给每个应用添加机器人能力，申请发送消息和接收群聊中 `@` 机器人消息的权限，并订阅 `im.message.receive_v1`。

事件订阅方式选择“使用长连接接收事件”，不填写公网 URL。每个机器人都使用自己的 App ID 和 App Secret，本机网关会自动建立对应连接。

如果后台提示需要先建立连接，先运行：

```bash
python3 ~/plugins/feishu-pr-review/scripts/install_launchd.py --load
```

然后在飞书后台保存长连接订阅设置。用下面命令确认连接状态：

```bash
curl http://127.0.0.1:8787/health
```

如果使用 `webhook` 备用模式，才需要 HTTPS 转发；当前 HTTP 适配器支持 Verification Token，不支持 Encrypt Key 加密回调。

## 5. 在 Codex 中加载插件

插件已经登记到个人 marketplace：

`~/.agents/plugins/marketplace.json`

重启 Codex 后，MCP 工具会按 `.mcp.json` 启动。可以让 Codex 调用 `feishu_review_health` 检查状态，也可以直接调用 `feishu_review_submit` 做本地联调。

## 6. 飞书使用示例

查看帮助：

```text
@PR 检视机器人 help
@PR 检视机器人 怎么用
```

只 `@` 机器人也会返回帮助卡片。帮助会根据当前配置展示默认仓库、可用的 PR 编号简写、完整链接格式和后台执行规则；没有识别到 PR 的消息会返回带提示的帮助卡片。

```text
@PR 检视机器人 https://github.com/acme/service/pull/123
请重点关注权限校验、并发安全和数据库迁移风险。
```

如果已经设置默认仓库，也可以简写为：

```text
@PR 检视机器人 复检 PR 314
```

机器人先回：

```text
已收到 PR 检视请求（a1b2c3d4）。我会在后台执行完整的 review-pr-with-panel 流程；完成后回传结果，并按 Skill 规则把共识后的可行动意见发布到 GitHub PR。
```

飞书不会收到 thread 创建或中间步骤消息。Codex App 会记录该任务，但由于当前 app-server 只允许一个客户端持有 thread 写入权，外部网关执行期间不能在 Codex App 中实时查看同一个 turn；任务完成后可在 App 中打开历史。最终摘要默认以飞书交互式卡片回传：卡片头部颜色会随最高待处理问题级别变化，并用 🔴/🟠/🟡/🔵 标出 Critical/High/Medium/Low，让是否存在待处理意见一眼可见；`FIX_VERIFIED` 等成功结论中的历史已修复 finding 只作为验证记录展示，不计入待处理意见。同时展示 PR、Review ID、模式、GitHub 发布状态和主要发现，并提供“查看 GitHub PR”按钮。若租户拒绝卡片消息，网关会自动回退为普通文本，避免结果丢失。

如果需要临时关闭卡片，可在配置文件中设置：

```json
{
  "feishu_result_format": "text"
}
```

默认值为 `card`；配置修改后网关会自动热加载。

## 当前边界

这一版默认使用飞书长连接；HTTP webhook 只是备用传输。长连接依赖 `lark-channel-sdk`，共享 App Server 依赖 `websockets`，多机器人会各自建立连接。网关、队列、MCP、共享 App Server 和 launchd 都是本机实现，不需要公网地址。GitHub 的 PR 读取、检视和发布由插件内置的 `review-pr-with-panel` Skill 负责，飞书入口不另写一套评审规则。
