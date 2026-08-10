# Durable Adaptive Routing

## 目标

Adaptive Router 在低风险告警上减少无关 Specialist 调用，同时保留事故处置的证据完整性、恢复能力和人工审批边界。它是 Harness 的确定性控制策略，不是一个拥有额外权限的自由推理 Agent。

## 决策合同

`incident-risk-v1` 只读取 Metrics Investigator 的结构化结果和任务写意图：

| 信号 | 路线 | 后续行为 |
| --- | --- | --- |
| 明确健康 | `single_agent_triage` | 安全拒绝事故结论，不创建动作 |
| 明确异常 | `verified_multi_agent` | 运行 Logs、Runbook、Verifier、Synthesis |
| Metrics 失败或结果缺失 | `verified_multi_agent` | fail-closed，保留完整验证链路 |

Router 不能创建工单、跳过 Verifier、扩大工具 allowlist 或授予审批。即使路线为完整验证，写操作仍停在 `WAITING_APPROVAL`。

## 持久化与恢复

编排模式保存在 `TaskEnvelope.metadata.orchestration`。路由后，Runtime 在调度剩余 Specialist 前完成以下动作：

1. 将 policy version、route、reason、risk score、fail-closed 和实际路线写入 `state.artifacts.routing_decision`；
2. 追加 `routing_decision` 审计事件；
3. 记录 `agent.risk_router` Span；
4. 写入 checkpoint。

因此进程在路由后中断时，`resume` 会复用相同决定，只运行尚未完成的 Specialist。`after_routing_decision` failpoint 用于回归这个故障窗口。

## Shadow 到 Active

建议按以下顺序上线：

1. 使用 `adaptive_shadow`；Router 记录建议，但实际仍运行完整 Multi-Agent。
2. 按服务、告警类型和时间窗口统计 route accuracy、escalation recall、false downgrade、unnecessary escalation、P95、token 和工具 attempts。
3. 人工复核所有建议降级样本，确认没有证据缺口或高风险写意图误判。
4. 只对低风险租户或服务启用 `adaptive`，保留快速回退到 `fixed_multi` 的配置。

Active 的首要门槛应是 false downgrade 为 0；延迟和成本改善是次要指标。样本量不足或置信区间过宽时继续 Shadow，而不是根据均值提前全量。

## 可观测字段

CLI 结果返回 `orchestration`、`routing_decision` 和 `outcome_class`。Trace 中 Runtime Span 带 orchestration 和 outcome class，Router Span 带 route、reason、risk score、shadow 与 fail-closed。安全拒答同时使用终态 `safely_refused` 和 outcome class `safe_refusal`；依赖故障、模型合约违规和 Runtime 异常保留 `failed`，并分别标记 `execution_failure` 或 `model_contract_failure`。旧数据库中已经完成的 `failed + safe_refusal` 快照保持原样，只有新运行采用新终态，以保留历史审计真实性。
