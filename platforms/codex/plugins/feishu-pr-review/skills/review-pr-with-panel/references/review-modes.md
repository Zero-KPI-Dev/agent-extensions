# 检视模式与历史识别

## 目标

让 Leader 根据当前会话、历史 packet 和 Git 版本自动判断本次任务是首次检视还是已有检视后的复检。模式判断必须可解释、可追溯，不能只根据用户的一句“再看一下”猜测。

## 可用信号

按可靠性从高到低使用：

1. 用户明确指令，例如“完整重检”“只确认上次问题是否修复”。
2. 当前会话中的 `review_id`、`prior_review_id`、`repository`、`base`、`head`、`finding_id` 和最终状态。
3. 已保存的 JSON packet 或报告，其中至少有上一轮 head 和 finding_id。
4. Git 中可解析的上一轮 head 到当前 head 的差异。
5. 普通自然语言历史。只有当它能与仓库和版本唯一对应时，才作为可靠证据。

## 判断顺序

1. 解析当前仓库、PR、base、current head 和用户明确范围。
2. 在当前会话和可用历史中寻找同一仓库及同一 PR/base 的最近一次完整结果。
3. 确认历史结果是否包含 machine-readable 的 `review_id`、`head` 和 finding_id。缺少这些字段时把历史质量标为 `weak`。
4. 若 current head 等于历史 head，选择 `NO_NEW_REVISION`，除非用户明确要求强制重检。
5. 若 current head 不同，读取 `previous_head..current_head`，将变更分为：
   - 既有 finding 的修复相关路径；
   - 修复附近的上下文变更；
   - 与既有 finding 无关的新功能或新路径。
6. 只有前两类占主导、旧 finding 的触发路径仍可映射时，选择 `FIX_VERIFICATION`。
7. 同时存在第三类实质变更时，选择 `INCREMENTAL_REREVIEW`。
8. 无可靠历史、无法映射旧 head、base 大幅改变、force-push 后无法建立等价范围，选择 `INITIAL_REVIEW`。

## 模式定义

### `INITIAL_REVIEW`

从完整授权变更集发现候选 finding。A 负责发现，B 负责独立验证；最多三轮。

### `FIX_VERIFICATION`

只针对上一轮 finding 验证修复结果，同时检查与原触发路径直接相邻的回归。复用同一份最小证据包，不重传完整历史、完整旧报告或无关 diff。A 不重复描述原 finding，只更新触发路径、证据和关闭状态；B 独立验证关闭条件。A/B 一致时在 B 返回后结束；只有实质分歧才追加一次聚焦的 A recheck。

复检默认冻结 finding 集合。除非当前 revision 新引入或实质恶化了具有直接证据、现实可达、`High`/`Critical` 严重级别，且会阻断主要功能交付或显著威胁系统稳定性、可用性或数据完整性的问题，否则不得新增 finding。

### `INCREMENTAL_REREVIEW`

把任务拆成两条关联轨道：

- 旧 finding：按 `FIX_VERIFICATION` 验证修复；
- 新变更：默认只做范围识别与复检新增意见门禁，不做无边界首次扫描。只有通过 `High`/`Critical` 功能交付或稳定性门禁的问题才可新增 finding。

用户明确要求完整检视新增范围，或新变更很大、涉及高风险边界且无法与旧结果分离时，将授权范围升级为 `INITIAL_REVIEW` 并说明成本影响；不得在复检中静默扩大范围。

### `NO_NEW_REVISION`

历史记录表明当前 head 已经检视过，且用户未要求强制重检。输出历史结果引用、当前版本和未重新启动 Agent 的原因。

## 风险与置信度

Leader 至少判断：

- `risk_tier`：`low`、`standard`、`high`；
- `scope_complexity`：`narrow`、`normal`、`broad`；
- `history_quality`：`strong`、`weak`、`none`；
- `mode_confidence`：`high`、`medium`、`low`。

权限、安全、数据迁移、并发、事务、持久化、支付、身份认证和大范围重构默认属于 `high`。风险不确定时先升级模型或补读必要证据，不自动扩大检视范围；只有 lineage 失效或用户明确要求时才升级为 `INITIAL_REVIEW`。

## 历史记录要求

每次有 Agent 运行的最终结果都应保留：

- `review_id`、`mode` 和 `prior_review_id`；
- repository、base、previous_head、current_head；
- finding_id、revision、生命周期状态；
- 旧 finding 的原触发路径、关闭条件和精简证据摘要；
- A/B requested 与 effective model/reasoning；
- github_target、publish_policy、publish_status 和已发布的 review URL（如有）；
- 模式判断依据、局限和未解决分歧。

下一次检视优先使用这些字段建立 lineage。只有自然语言总结而没有版本和 finding 标识时，不得把旧意见自动标记为已修复或已关闭。
