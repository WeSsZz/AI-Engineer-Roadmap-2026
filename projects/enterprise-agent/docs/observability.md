# Persistent Trace 与可观测性

## 为什么 Event Log 之外还需要 Span

Event Log 记录任务发生了什么，适合审计和恢复；Span 记录一次操作从何处调用、耗时多久以及是否失败，适合定位瓶颈和建立 SLO。两者保留不同语义，不互相替代。

## 数据模型

每个 Span 包含：

- `span_id`：单次操作唯一标识。
- `trace_id`：当前使用稳定的 `task_id`，使多次恢复属于同一条逻辑 Trace。
- `parent_span_id`：建立 Runtime → Agent → Tool 的调用树。
- `kind` 与 `name`：区分 runtime、agent、tool 以及具体操作。
- 开始/结束时间、毫秒耗时、状态和错误类型。
- 低基数结构化 attributes；不写入密钥、审批 token 或完整业务正文。

`run` 和每次非终态 `resume` 都创建一个新的根 Span。这样既能看到一项任务跨进程的完整历史，又不会假装恢复前后是同一个连续进程调用。

## Tool attempt 语义

Tool Gateway 的每条 `ToolCallRecord` 会立即转换为持久化 Tool Span。一次调用发生重试时，每个 attempt 都有独立 Span；参数校验、权限、限流和熔断拒绝使用 attempt `0` 并标记 error。

Trace sink 的失败不会改变业务工具调用结果，错误保留在 `ToolRuntime.sink_errors` 中，避免观测系统故障扩大为业务故障。

## 查询与指标

```bash
PYTHONPATH=src python -m enterprise_agent \
  --db work/enterprise_agent.db trace TASK_ID
```

命令输出原始 Span 和以下聚合：

- Span 总数与仍为 `running` 的数量。
- 总体、`kind`、`name` 维度的 count、error count、error rate。
- P50/P95 延迟，使用 deterministic nearest-rank 算法。

未结束 Span 不参与延迟和错误率计算，并单独计入 `running_count`。生产版本应由恢复扫描器结合 Worker lease 判断它们是仍在执行还是已经中断。

## 当前边界

- SQLite 适合单机学习和故障语义验证，不是分布式 Trace 后端。
- 目前没有跨 HTTP/消息队列传播 W3C Trace Context。
- 尚未提供 OpenTelemetry exporter、采样、保留策略和 cardinality guard。
- Agent Span 覆盖可观察输入输出元数据，不记录模型隐藏推理内容。
