# Incident Diagnosis 场景

## 企业使用场景

该场景面向 SRE、运维和平台团队的事故初诊。系统读取监控、日志和标准处置手册，形成证据绑定的根因假设；它不会直接修改生产环境，只能在人工审批后创建可审计工单。

## 三条并行证据链

```text
Metrics Investigator -> error rate / P95 / saturation
Log Investigator     -> error code / frequency / correlation
Runbook Researcher   -> approved diagnosis and remediation steps
                              |
                           Verifier
                              |
                    Incident Synthesis
```

当前确定性 Demo 中，`payment-api` 同时满足：

- 错误率 12.8%，显著高于 1.2% 基线；
- P95 延迟 1840ms，数据库连接池利用率 98%；
- 日志出现 `DB_POOL_TIMEOUT`、`POOL_SATURATED` 和 `HTTP_503`；
- Runbook 明确覆盖 database pool exhaustion。

只有上述证据经过 Verifier 检查后，Synthesis 才把根因置信度标记为 `high` 并准备故障工单。明确健康时任务进入 `safely_refused`；任一关键数据源永久失败时任务进入 `failed`。两者置信度均为 `insufficient` 且不生成待审批动作，但任务级 SLO 可以区分预期拒答与依赖故障。

## 自适应编排

Runtime 支持三种模式：

| 模式 | Router | 实际执行 |
| --- | --- | --- |
| `fixed_multi` | 不运行 | 始终执行 Metrics、Logs、Runbook |
| `adaptive_shadow` | 记录建议 | 仍执行完整 Multi-Agent 链路 |
| `adaptive` | 记录并执行 | 健康信号只做 Metrics triage；其余执行完整链路 |

当前 `incident-risk-v1` 使用结构化 Metrics 结果，不让模型自由决定是否省略安全检查。异常信号且任务含写意图时风险分为 3，进入 `verified_multi_agent`；健康信号进入 `single_agent_triage`；Metrics 失败或字段缺失时 fail-closed，仍执行 Logs、Runbook 和 Verifier。任何路线都不能绕过写操作审批。

## 故障注入

Fault Injector 位于工具 provider adapter 内侧，因此请求仍然经过 allowlist、schema、timeout、retry、circuit breaker 和 Trace。支持：

- `transient`：抛出可重试的上游瞬时错误；
- `permanent`：抛出不可重试错误；
- `timeout`：让 handler 超过 Gateway deadline。

```bash
PYTHONPATH=src python -m enterprise_agent \
  --db work/fault-demo.db run \
  --inject-fault get_runbook:timeout:1
```

每个故障和恢复 attempt 都持久化为 Tool Span，便于验证 retry recovery rate、错误分类和父子 Trace 完整性。

## 安全边界

- Metrics、Logs、Runbook 工具均为只读并受参数 Schema 和 allowlist 约束。
- 不将日志全文、凭据或审批 token 写入 Span attributes。
- Runbook 是建议来源，不等于自动执行授权。
- 创建工单仍绑定持久化 `action_id`、人工审批和 idempotency key。
