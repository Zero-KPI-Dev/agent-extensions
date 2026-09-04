# 分布式部署影响检视

仅在目标实际采用或可能采用多副本、多进程路由、共享控制面/存储、租约、分片、异步接管或滚动升级时读取。目标是把 PR 的代码路径放回真实部署拓扑中判断，不是看到 Kubernetes 文件就机械地产生 finding。

## 建立部署上下文

Leader 先从仓库内的架构文档、部署清单、配置和相关未改动代码建立最小 `deployment_context`：

```json
{
  "topology": "single_node | distributed | both | unknown",
  "evidence": ["支持该判断的文件、配置或代码事实"],
  "affected_components": ["router", "core", "worker", "database", "shared-storage"],
  "risk_areas_checked": ["ownership-routing", "coordination", "storage", "retry-idempotency", "rollout-compatibility", "failure-backpressure", "network-observability"],
  "distributed_impact": "NONE | SAFE_WITH_EVIDENCE | RISK_IDENTIFIED | UNVERIFIED",
  "limitations": []
}
```

- `NONE`：变更不进入分布式运行路径，并有可核验依据。
- `SAFE_WITH_EVIDENCE`：进入相关路径，但现有协议、防护和测试足以维持不变量。
- `RISK_IDENTIFIED`：存在可定位、现实可达的分布式缺陷；应形成 finding。
- `UNVERIFIED`：缺少决定性配置、环境或多副本证据；不得伪装成安全，也不得只凭理论风险形成 finding。

不要把 live 集群访问当成普通代码检视的前提。未经用户授权时只使用仓库证据；缺少真实故障注入或多副本验证时写入 limitations。

## 必查风险面

根据变更实际可达路径选择相关项，但不得因为 diff 没改部署 YAML 就跳过代码层影响。

### 所有权与路由

- 租户、会话、任务或分片是否始终解析到唯一且仍存活的 owner；路由键、规范化和哈希在各组件间是否一致。
- owner 迁移、Pod 重启、扩缩容、租约过期和 stale route 后是否会重新解析，而不是继续写旧实例。
- 稳定实例身份、启动代次、fencing generation 是否贯穿写入路径；旧 owner 是否可能在失去租约后继续提交。
- Router/网关的缓存、允许目标集合、健康判定和回退是否会把请求发到未就绪、不拥有数据或已 draining 的副本。

### 协调、并发与一致性

- `threading.Lock`、进程内缓存、内存队列和本地单例只能保护单进程，不能被当作全局互斥或全局真相。
- 跨副本 claim、调度、发布和状态迁移是否由事务条件更新、唯一约束、租约/心跳、CAS 或等价机制保护。
- 超时、网络分区和重试后是否可能出现双 owner、丢失更新、ABA、乱序覆盖或 split-brain；比较时间时是否依赖不安全的本机时钟。
- 强一致、最终一致和可降级投影的边界是否明确；缓存失效和 read-after-write 假设是否符合实际后端。

### 存储与数据完整性

- 区分本地临时状态、稳定实例私有状态、共享数据库和共享文件系统；路径和锁语义不能在这些边界间偷换。
- 多副本写共享目录时检查原子写/rename、文件锁有效范围、临时文件命名、并发覆盖、部分写入和恢复策略。
- SQLite、JSONL 或本地索引若被多主机并发写入，必须有明确单写者或外部协调；共享文件系统本身不是数据库租约。
- 数据库迁移、索引、唯一约束和事务隔离要兼容旧/新版本并存；失败重试不能产生双权威、重复记录或不可恢复半状态。

### 异步任务、重试与幂等

- 默认按至少一次执行思考：请求超时、响应丢失、worker 崩溃、lease expiry 和接管都可能重复执行。
- 幂等键必须覆盖同一逻辑操作且在副本间共享；去重不能只存在本地内存，key 重用也不能吞掉不同 payload。
- claim、heartbeat、完成提交和失败恢复之间要有清晰状态机；旧 epoch/owner 的迟到结果不得覆盖新 owner。
- 队列容量、并发限制和熔断是每副本还是全局要明确；扩容不能意外把允许负载和下游压力放大 N 倍。

### 滚动升级与故障恢复

- 新旧 Pod 并存时，API、事件、数据库 schema、配置、序列化格式和共享文件产物是否双向兼容。
- readiness、draining、优雅终止、PDB 和 termination grace 是否足以阻止新流量并完成/移交在途工作。
- 单副本退出、目标集合变化、依赖超时和共享后端短暂不可用时，系统是否有界失败、可恢复且不会形成惊群。
- 配置或镜像只更新部分组件时，协议版本偏差是否会错误路由、拒绝合法请求或写入旧格式。

### 网络、安全与可观测性

- Service/Ingress/NetworkPolicy/端口和 route allowlist 是否与实际消费者一致；内部信任边界不能代替业务身份校验。
- 日志、指标和 trace 应能按 tenant、operation、owner instance、boot/epoch、route target 和结果关联，但不得泄露凭据。
- 至少考虑双副本并发、owner 重启/接管、重复投递、旧消息迟到、滚动升级和共享后端抖动的定向测试或故障注入。

## EchoMem 当前 CCE 拓扑

检视 EchoMem 时，以当前仓库为准，优先读取：

- `docs/design/features/19.deployment-topologies.md`
- `deploy/cce/echomem/README.md`
- `deploy/cce/echomem/NETWORK_TOPOLOGY.md`
- `deploy/cce/echomem/base/core-statefulset.yaml`
- `deploy/cce/echomem/base/router-deployment.yaml`
- `deploy/config/environments/cce/<target>/profile.json`
- 与本次 diff 相关的 `control_store`、`tenant_router`、`tenant_coordination`、commit/worker 和共享存储实现

当前架构的关键不变量是：Router 多副本负责 tenant 路由；Core StatefulSet 使用稳定实例身份；共享控制库存储 tenant ownership、lease、generation/fencing 和 heartbeat；Core/Adapter 共享 RDS 与 SFS 数据边界；滚动发布期间新旧进程可能同时运行。因此：

- 任何影响 tenant/session 路由、认证映射、owner claim/renew/release、heartbeat/readiness 或 target allowlist 的变更，都要追踪到 Router → owner Core → 共享状态的完整路径。
- 任何新增进程内锁、缓存、队列、SQLite/JSONL/本地文件状态的变更，都要证明它只需副本内语义，或存在跨副本协调。
- 任何 commit、worker、publication、migration 或共享文件写入变更，都要检查重复执行、旧 owner 迟到写、原子发布、恢复和版本偏差。
- 单节点测试通过只能证明局部行为；若结论依赖多副本不变量，应要求相应的并发、接管或滚动升级证据。

## Finding 与报告规则

- 只有能连接“当前 PR 变更 → 真实部署路径 → 被破坏不变量 → 可观察影响”的候选才形成 finding；泛泛的分布式担忧写入已检查风险面或 limitations。
- finding 的 `trigger_conditions` 应包含必要的副本数、owner/lease 状态、重试或升级条件；`execution_path` 应跨越实际组件；`existing_controls` 要说明防护为何不足。
- 可能导致跨租户错误路由、双写/数据损坏、旧 owner 提交、广泛不可用或无法安全升级的问题通常为 High/Critical，但仍需按现实可达性和现有缓解定级。
- 即使没有 finding，最终报告也必须说明 `deployment_context`、受影响组件、分布式结论及未完成的多副本验证，避免把“未检查”表达成“无影响”。
