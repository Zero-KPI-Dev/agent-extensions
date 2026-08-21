# Skill-owned runtime coordination

本 Skill 使用一个共享 SQLite 文件作为本地协调总线，不启动外部微服务。每次检视创建独立 run_id，所有 Agent、事件和 packet 都必须带有该 run_id。

## 状态位置

数据库不放在 Skill 安装目录、PR 仓库或 worktree 中。默认路径由运行环境解析：

- macOS：Codex 应用状态目录下的 review-pr-with-panel/runs.sqlite3
- Windows：LOCALAPPDATA 下的 Codex/review-pr-with-panel/runs.sqlite3
- Linux：XDG_STATE_HOME/codex/review-pr-with-panel/runs.sqlite3，没有时使用用户状态目录

可通过 REVIEW_PR_PANEL_STATE_DIR 指定状态目录，或通过 REVIEW_PR_PANEL_STATE_DB 指定数据库文件。Leader 创建 run 后，必须把实际 state_db 和 run_id 注入 A/B 的 runtime context；Agent 不得扫描目录猜测数据库。

## 启动与关闭

Skill 开始检视时：

1. 调用 scripts/review_runtime.py cleanup，只清理过期 terminal run 和旧 heartbeat/progress。
2. 调用 scripts/review_runtime.py create 创建 run。
3. 在 spawn_agent 前调用 register-agent。
4. 将 run_id、state_db、agent_id、epoch 传给 Agent。

Skill 完成时必须在 finally 路径关闭 run：

~~~text
正常完成 → COMPLETED
外部发布失败但检视完成 → COMPLETED，并在 summary 记录 publish_status
主动取消 → CANCELLED
不可恢复的运行错误 → FAILED
~~~

如果 Leader 崩溃，没有进程可以执行 finally。下一次 Skill 启动时根据 lease 和 updated_at 扫描旧 run，先标记为可恢复/ABANDONED，再按 retention 清理；不得直接删除仍在 lease 内的 run。

## 事件协议

事件至少包含：

~~~json
{
  "run_id": "R-20260807-001",
  "agent_id": "A",
  "epoch": 1,
  "seq": 18,
  "event_type": "heartbeat",
  "phase": "diff_analysis",
  "status": "RUNNING",
  "created_at": "2026-08-07T00:00:00+00:00",
  "payload": {}
}
~~~

- run_id：本次检视的隔离边界。
- agent_id：A、B 或 Leader。
- epoch：替换 Agent 时递增，旧 epoch 的事件不能覆盖新 Agent。
- seq：同一 run/agent/epoch 内单调递增；相同 payload 的重复事件幂等。
- event_type：heartbeat、progress、observation、packet、ack 或 lifecycle。
- phase：当前阶段，例如 diff_analysis、test_verification、recheck。

Leader 用 event_id 作为游标读取事件，不依赖“读取到哪个最后消息”的模糊状态。

## Heartbeat、progress 与 lease

Heartbeat 表示 Agent runtime 仍然存活；progress 表示任务阶段有推进；final packet 表示业务结果已经可裁决。三者不能混为一谈。

推荐默认值：

~~~text
heartbeat_interval = 15s
lease_timeout = 60s
settle_grace = 120s
event_retention = 1d
terminal_run_retention = 30d
~~~

Heartbeat 必须由 Agent wrapper 或底层 runtime hook 自动发出，不应依赖模型“记得发送”。模型提示中的阶段性 progress 只能作为补充，不能被当作可靠 liveness。

当前 multi_agent_v1 若没有生命周期 callback，Skill 只能记录启动、工具边界、最终 packet 和模型主动报告的 progress；此时 wait_agent 超时必须仍然记为 UNKNOWN_STILL_RUNNING，不得声称已经拥有实时 heartbeat。

Leader 在每个有界 `wait_agent` 窗口结束后可以写入 `observation` 事件。该事件表示 Leader 观察到“这一段等待窗口结束”，不是 Agent A/B 的 heartbeat，也不会刷新目标 Agent 的 lease。它的作用是留下明确的等待轨迹，并让 Skill 在没有 runtime callback 时继续等待，而不是把一次 transport timeout 解释成 Agent 失败。只有底层 Agent wrapper/runtime hook 写入的 `heartbeat` 才能证明 Agent 仍然存活。

等待窗口本身不需要产生新的模型 prompt。首个窗口超时后只记录 observation 并继续等待；不要例行发送“进度如何”或重传任务。窄范围复检的逻辑总预算通常为 120 秒，高风险复检通常为 240 秒；首次检视 narrow/low 通常为 300 秒，standard/high 或 broad 通常为 600 秒。只有达到总预算或 lease 门禁时才发送一次包含未决 finding ID 和输出契约的聚焦 settle 请求。

Lease 过期时的固定动作：

~~~text
lease 过期
  → 标记 UNKNOWN_STILL_RUNNING
  → 发送一次非中断 settle 请求
  → 等待 settle_grace
  → 仍未恢复才允许取消/替换
  → 新 Agent 使用新 epoch
~~~

绝不能把一次等待窗口超时直接当成 Agent 失败。

## 并发与清理

SQLite 开启 WAL、foreign keys 和 busy timeout。不同 PR 共享数据库，但所有查询和写入必须带 run_id。不得使用全局 current_agent 或单一活动 run 字段。

清理策略：

- heartbeat：保留 1 天；
- progress：保留 3～7 天；
- 已完成 raw packet：保留 30 天；
- run summary/finding lineage：保留 90 天；
- active run：不清理；
- terminal run 超过 retention：删除其 agents、events、packets 和 run 记录；
- 删除后执行 WAL checkpoint，必要时再 VACUUM。

清理由 Skill 在每次启动和结束时执行，不需要单独的守护进程。

## CLI 入口

常用命令：

~~~bash
python3 scripts/review_runtime.py state-path
python3 scripts/review_runtime.py create --repository /repo --pr-number 123 --base-sha BASE --head-sha HEAD
python3 scripts/review_runtime.py register-agent --run-id RUN --agent-id A --role primary --epoch 1
python3 scripts/review_runtime.py heartbeat --run-id RUN --agent-id A --epoch 1 --seq 1 --phase diff_analysis --activity working
python3 scripts/review_runtime.py progress --run-id RUN --agent-id A --epoch 1 --seq 2 --phase test_verification --message "running tests"
python3 scripts/review_runtime.py observe-wait --run-id RUN --target-agent A --phase waiting_for_a_initial --wait-seconds 30 --timed-out
python3 scripts/review_runtime.py events --run-id RUN --after-event-id 0
python3 scripts/review_runtime.py close --run-id RUN --status COMPLETED --summary-json '{}'
python3 scripts/review_runtime.py cleanup
~~~

这些命令是 Skill 的内部执行面，不要求用户手动调用。
