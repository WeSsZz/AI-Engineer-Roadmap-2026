# Durable Agent Runtime Architecture

## Harness 与 Agent Infra 的边界

Agent 产生局部决策；Harness 控制一次任务如何推进；Agent Infra 则提供任务跨进程、跨时间运行所需的持久化、工具网关、可观测性和评测能力。

本项目目前包含两层：

```text
Agent Layer
  Knowledge Researcher / Data Analyst / Verifier / Synthesis

Runtime Layer
  State Machine / Checkpoint / Event Store / Context Engine / Tool Runtime
```

## Checkpoint 语义

SQLite 中同时保存：

- `tasks.snapshot_json`：当前任务、状态和 Agent 结构化结果
- `tasks.version`：乐观并发控制版本
- `events`：按照 sequence 递增的 append-only 审计轨迹

快照用于快速恢复，事件用于解释任务发生过什么。事件插入使用 `(task_id, sequence)` 唯一键，重复 checkpoint 不会重复写入同一个事件。

## 恢复策略

Runtime 根据持久化状态选择下一步：

- `VERIFYING`：直接使用已保存的 Specialist 结果，不重复检索和查询
- `SYNTHESIZING`：直接使用已保存的验证结果
- `WAITING_APPROVAL`：返回待审批动作，不执行写工具
- `EXECUTING_ACTION`：检查持久化审批与 `action_id`，然后安全重试写工具
- 终态：直接返回存储结果，不执行任何阶段

如果进程在只读 Specialist 执行中、但 checkpoint 前中断，该阶段可能重新执行。它被限制为只读操作，因此采用 at-least-once 语义是安全的。

Adaptive 模式在 Metrics 结果后立即持久化 `routing_decision`，再执行后续 Specialist。若此时中断，`resume` 会复用原决定和已完成的 Metrics 结果，不会重新路由，也不会重复 Metrics 调用。旧任务没有编排元数据时按 `fixed_multi` 恢复，保持向后兼容。

## 写操作幂等

审批绑定一个在合成阶段就已固定的 `pending_action.action_id`。Action Executor 必须同时满足：

1. 审批记录存在；
2. 审批记录的 `action_id` 与待执行动作完全一致；
3. 工具调用携带相同 `action_id` 作为 idempotency key。

Demo 工单系统使用 SQLite 唯一约束实现幂等。如果进程在工单创建后、Runtime checkpoint 前崩溃，恢复执行会取回原工单并标记 `replayed=true`。

真实系统仍需目标服务原生支持幂等键；仅在 Agent Runtime 本地缓存工具结果无法彻底消除“副作用成功但响应丢失”的重复风险。

## 当前保证

- 状态只能按照有限状态机合法转换。
- 每个已完成阶段都有持久化快照。
- 终态任务重复 resume 不会再次执行。
- 并发 checkpoint 使用版本比较，过期 Worker 会失败。
- 高风险动作没有匹配审批时不能执行。
- Demo 写工具在崩溃恢复后不会创建重复工单。
- Adaptive 路由决定与任务状态一起 checkpoint，并写入 Event Log 和 Trace。

## 尚未保证

- Worker 租约和分布式任务抢占。
- 自动超时、指数退避、熔断和限流。
- PostgreSQL/Kafka 场景下的跨服务一致性。
- 真实模型调用的确定性重放。
- Prompt Injection 与企业身份系统集成。
- Trace 指标和系统化离线评测。
