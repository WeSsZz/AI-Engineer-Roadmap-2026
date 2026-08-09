# Reliable Tool Gateway

## 目标

Tool Gateway 将 Agent 产生的工具意图转换为受控、可解释的执行。Agent 不能绕过 Gateway 直接调用企业系统。

一次调用按以下顺序处理：

```text
Tool Allowlist
  -> Argument Schema Validation
  -> Approval and Idempotency Policy
  -> Fixed-window Rate Limit
  -> Circuit Breaker
  -> Concurrency Slot
  -> Handler Execution with Timeout
  -> Retry Decision
  -> ToolCallRecord
```

## Schema 与 Policy

`ArgumentSpec` 描述参数类型、是否必填、是否允许空值和领域校验函数。未知参数也会被拒绝，避免模型生成的拼写错误或多余字段悄悄进入业务系统。

`ToolPolicy` 当前支持：

- `timeout_seconds`
- `max_retries`
- `retry_backoff_seconds`
- `max_concurrency`
- `queue_timeout_seconds`
- `rate_limit_calls` / `rate_limit_window_seconds`
- `circuit_failure_threshold` / `circuit_recovery_seconds`
- `requires_approval`
- `idempotency_required`

高风险工具只要允许重试，就必须要求 idempotency key；注册不满足该约束的工具会直接失败。

## 错误分类与重试

| 错误 | 含义 | 自动重试 |
| --- | --- | --- |
| `ToolValidationError` | 参数缺失、未知、类型或领域校验失败 | 否 |
| `ToolPermissionError` | 工具未授权或缺少人工审批 | 否 |
| `ToolTransientError` | 上游临时不可用 | 是 |
| `ToolTimeoutError` | 超过工具级超时 | 是 |
| `ToolPermanentError` | 业务拒绝、代码错误或不可恢复失败 | 否 |
| `ToolRateLimitError` | 固定窗口额度耗尽 | 否，由上层调度 |
| `ToolConcurrencyError` | 活跃 handler 已达到上限 | 否，由上层排队 |
| `CircuitOpenError` | 连续失败后熔断 | 否，等待恢复窗口 |

瞬时错误与超时使用指数退避：

```text
backoff = base * 2^(attempt - 1)
```

## 超时与并发语义

Python 线程不能被安全强杀。`future.cancel()` 只能取消尚未开始的任务；已经运行的 handler 可能在调用方收到超时后继续执行。

因此 Gateway 在 handler 真正结束前不会释放并发槽。这样慢调用无法通过连续超时和重试绕过 `max_concurrency`。写工具还必须依赖目标服务支持的幂等键，因为仅限制线程无法消除“副作用成功但响应丢失”的重复风险。

## 熔断

工具在最终失败次数达到阈值后进入 open 状态。恢复窗口结束后只允许一次 half-open 探测：

- 探测成功：清零失败次数并关闭熔断；
- 探测失败：重新打开熔断；
- 探测进行中：其他调用快速拒绝。

参数、权限、限流和并发容量拒绝不会计入上游工具连续失败。

## 调用记录

每次实际尝试记录：

- `call_id` 和 `task_id`
- 工具名和 attempt
- completed / failed / rejected
- duration
- error type
- idempotency key
- started time

成功调用的聚合信息会进入 Agent Result 和任务 Event Log。完整 `ToolCallRecord` 当前保存在 Runtime 内存中，下一阶段会统一持久化为 Trace Span。

## 已验证场景

- 缺失、未知、类型错误和领域非法参数
- 人工审批与幂等键缺失
- 瞬时失败后恢复
- 永久失败不重试
- 超时分类和重试
- 超时 handler 持续占用并发槽
- 连续失败触发熔断
- 固定窗口限流
- 并发饱和快速失败
- 两个并发 Worker 使用同一幂等键只创建一个工单
