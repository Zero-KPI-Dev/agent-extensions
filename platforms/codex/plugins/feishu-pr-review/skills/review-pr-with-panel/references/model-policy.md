# 子 Agent 模型策略

## 目标

让 Leader 根据检视模式、风险、变更范围和历史质量动态选择模型与推理强度。A 负责主检视或修复验证，B 负责独立反证和关闭门禁。模型选择必须记录 requested 与 effective，不能把推理强度当作模型能力的完全替代。

## 当前环境能力

始终以本次运行的 `spawn_agent` 工具公开组合为准，不使用 `create_thread`、其他工具、旧会话或模型自报来判断子 Agent 可用性。当前 Codex host 的 `spawn_agent` 元数据公开：

- `gpt-5.6-sol`：`low`、`medium`、`high`、`xhigh`、`max`、`ultra`；
- `gpt-5.6-terra`：`low`、`medium`、`high`、`xhigh`、`max`、`ultra`；
- `gpt-5.6-luna`：`low`、`medium`、`high`、`xhigh`、`max`，不含 `ultra`。

因此不要请求 `gpt-5.6-luna/ultra`。用户或 Leader 需要“Luna 的最高推理”时使用 `gpt-5.6-luna/max`；需要 `ultra` 时把模型升级到支持它的 Sol 或 Terra。

## 模型覆盖与 fork

`spawn_agent` 的显式模型覆盖与完整历史 fork 不兼容。启动 A/B 时固定：

```text
fork_turns: "none"
model: <requested model>
reasoning_effort: <requested effort>
```

A/B prompt 必须自包含仓库、范围、base/head、run context、证据包和输出契约。不得省略 `fork_turns` 或使用 `fork_turns: "all"` 后同时传模型；那会导致覆盖被拒绝或继承父模型，也会把整段 Leader 历史复制给子 Agent，增加 token。

当 `spawn_agent` 元数据列出 `gpt-5.6-luna` 及所需 reasoning 时，必须先按原请求真实调用 Luna。不得仅根据“似乎不可用”、旧记录或其他工具的模型清单提前写成 Terra。调用成功即记录该请求为 effective；若运行时另行返回实际模型则以其为准。只有当前调用明确返回 unsupported/unavailable 才能降级；若无法确认实际模型，记录 `effective: UNKNOWN`，不得编造。

## Leader 决策矩阵

默认使用 `balanced`。模型档位是基于当前已知实测与人工判断设置的质量下限，不把任何单一外部排行榜的分数固化成永久事实；模型版本或运行环境变化时应重新校准。窄范围复检通过最小证据包控制成本，但仍遵守 Terra 至少 `xhigh`、Luna 只使用 `xhigh`/`max`、Sol 至少 `high` 的下限。

| 模式与风险 | Agent A | Agent B | 适用判断 |
|---|---|---|---|
| 首次，高风险或 broad | `sol/xhigh`；极复杂时 `sol/max` | `terra/max` | 安全、权限、并发、迁移、事务、大重构 |
| 首次，standard | `sol/high` | `luna/max` | 变更可追踪、范围正常 |
| 首次，low 且 narrow | `terra/xhigh` | `luna/max` | 低风险、小范围、边界清晰 |
| 修复复检，高风险 | `sol/high`；复杂关闭门禁可升 `sol/xhigh` | `terra/max` | 关闭错误代价高，但范围仍冻结 |
| 修复复检，standard | `terra/xhigh` | `luna/max` | 只验证旧触发路径和直接回归 |
| 修复复检，low 且 narrow | `terra/xhigh` | `luna/xhigh` | 证据和变更均局部；独立性来自不同 Agent 上下文 |

如果历史质量为 `weak` 或 `none`、模式置信度为 `low`、旧路径无法清楚映射，按更高风险配置运行；若无法建立可靠 lineage，则直接选择 `INITIAL_REVIEW`。

## Profile

用户可以指定 profile，但未指定时由 Leader 按上表自适应选择：

| Profile | 默认倾向 | 用途 |
|---|---|---|
| `quality` | A/B 比 balanced 高一档；高风险优先 `sol/max` + `terra/max` | 用户明确要求最高质量或极高风险任务 |
| `balanced` | 按上表；standard 首检使用 `sol/high` + `luna/max`，standard 复检使用 `terra/xhigh` + `luna/max` | 默认，兼顾证据质量与 token 成本 |
| `economy` | A `terra/xhigh`、B `luna/xhigh` | 仅限低风险、小型、高频任务，仍不突破质量下限 |
| `custom` | 使用用户或 Leader 明确选择 | 必须记录选择理由 |

安全关键、数据迁移、并发、权限边界或大范围重构不得自动采用 `economy`。用户明确要求低成本时可以记录风险警告，但不应隐瞒由此产生的检视能力下降。

## Prompt 与轮次预算

- 复检 prompt 只携带旧 finding 的稳定 ID、原触发路径、关闭条件、相关 changed hunks 和定向验证入口；让 Agent 从仓库读取必要代码，不嵌入大段源码。
- A/B 复用同一最小证据包；给 B 追加 A 的逐条状态即可，不附带 A 的冗长过程描述。
- A/B 已一致时不启动 A recheck。存在分歧时只发送未决 finding、新证据和一个具体问题，不重发完整 prompt。
- 不用提升 reasoning 来补偿错误的超大范围；先冻结范围、去重上下文，再按风险升级模型。

## 可用性与降级

只使用当前子 Agent 工具明确公布的模型和推理强度组合。降级时优先选择仍满足质量下限的可用组合，不为了保留同一模型而跌破该模型的推理下限：

- A `sol/ultra` → `sol/max` → `sol/xhigh` → `sol/high` → `terra/max` → `terra/xhigh` → `STOP_UNAVAILABLE`；
- A `sol/max` → `sol/xhigh` → `sol/high` → `terra/max` → `terra/xhigh` → `STOP_UNAVAILABLE`；
- A `sol/xhigh` → `sol/high` → `terra/max` → `terra/xhigh` → `STOP_UNAVAILABLE`；
- A `sol/high` → `terra/max` → `terra/xhigh` → `STOP_UNAVAILABLE`；
- A `terra/xhigh` → `terra/max` → `sol/high` → `STOP_UNAVAILABLE`；
- B `luna/max` → `luna/xhigh` → `terra/xhigh` → `sol/high` → `STOP_UNAVAILABLE`；
- B `luna/xhigh` → `luna/max` → `terra/xhigh` → `sol/high` → `STOP_UNAVAILABLE`；
- B `terra/max` → `terra/xhigh` → `sol/high` → `STOP_UNAVAILABLE`；
- B `terra/xhigh` → `terra/max` → `sol/high` → `STOP_UNAVAILABLE`。

只有父配置的 effective 模型与推理强度已确认满足上述下限，才允许把它作为 `STOP_UNAVAILABLE` 前的额外候选；父配置未知、仅知道 reasoning 名称，或低于任一对应下限时都必须停止并报告不可用。不得把“继承父配置”写成无条件降级终点。

若显式组合被运行时拒绝：

1. 先检查是否错误地用了默认/`all` fork；修正为 `fork_turns: "none"` 后使用同一模型重试一次，这不算模型降级。
2. 不要用猜测的模型 slug 重试，也不要把参数冲突误报为 Luna 不可用。
3. 仅在修正 fork 后仍收到明确的模型不支持/不可用错误时，按对应链切换到下一档。
4. 在最终报告中记录 requested、effective、fork_turns、原始错误摘要、降级链和原因。
5. 两个子 Agent 都无法启动时停止，不得把 Leader 的第二遍阅读描述为独立复核。

## 独立性

B 使用不同模型变体是为了降低相同推理路径的相关性，不代表 B 的级别低于 A。B 必须拿到原始代码基准并亲自验证，不能只评论 A 的措辞。修复复检中，B 的关闭意见尤其需要独立证据；推理强度高不等于可以跳过代码重读。

## 最终记录

最终报告包含：

```text
Mode: INITIAL_REVIEW | FIX_VERIFICATION | INCREMENTAL_REREVIEW | NO_NEW_REVISION
Risk tier: low | standard | high
Agent A requested: model / reasoning
Agent A effective: model / reasoning
Agent B requested: model / reasoning
Agent B effective: model / reasoning
Agent A/B fork_turns: none
Profile: quality | balanced | economy | custom
Selection reason: ...
```
