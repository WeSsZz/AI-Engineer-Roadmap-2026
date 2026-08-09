# Evaluation

> 本文件只记录真实实验；初始模板中的数值保持为空。

## 数据集

- 当前版本：`incident-diagnosis-v1`
- 当前规模：5 个确定性合成 case，包含正常事故、瞬时故障、超时、永久故障和健康服务
- 标注：期望根因、证据类型、工具集合、是否应安全拒绝、故障是否可恢复
- 当前用途：验证评测管线和 Agent Infra 语义，不代表真实生产泛化能力
- 后续目标：80–100 个开发/保留集分离的真实脱敏事故 case

## 指标

### Retrieval

- Recall@5 / Recall@10
- MRR
- Rerank 后的 NDCG（可选）

### Answer

- 答案正确性
- 引用忠实度
- 无法回答时的拒答质量

### Agent

- 任务成功率
- 工具选择准确率
- 参数准确率
- 平均工具调用次数

### System

- P50 / P95 延迟
- 每请求 token 或估算成本
- 错误率
- Trace 完整率：具有 Runtime→Agent→Tool 可达父链的 Span 比例
- 孤儿/未结束 Span 数量：发现进程硬退出和埋点缺口

### Tool Gateway

- Schema rejection rate
- Retry recovery rate
- Timeout rate
- Circuit-open rejection rate
- Concurrency/rate-limit rejection rate
- 每个工具的 P50 / P95 latency
- 平均 attempts / call

## 检索实验

| Run | 方法 | Recall@5 | MRR | P95 延迟 | 备注 |
| --- | --- | ---: | ---: | ---: | --- |
| R0 | Dense baseline |  |  |  |  |
| R1 | BM25 |  |  |  |  |
| R2 | Hybrid + RRF |  |  |  |  |
| R3 | Hybrid + Reranker |  |  |  |  |

## Agent 实验

2026-08-10 本机基线：CPython 3.12.2、macOS arm64、5 cases × 5 repetitions。完整逐次结果见 [`benchmarks/incident-diagnosis-v1.json`](benchmarks/incident-diagnosis-v1.json)。

| Run | 方法 | 任务成功率 | 工具 Precision | P50 ms | P95 ms | 平均 attempts |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| A0 | Fixed Workflow | 1.000 | 0.867 | 2.866 | 85.771 | 3.2 |
| A1 | Single Agent | 1.000 | 1.000 | 2.374 | 86.471 | 2.8 |
| A2 | Plan-Executor | 1.000 | 1.000 | 2.744 | 89.901 | 2.8 |
| A3 | Multi-Agent | 1.000 | 0.800 | 9.515 | 89.395 | 3.4 |

四种策略在这个小型确定性数据集上的根因准确率、安全拒绝率、证据完整率和故障恢复率均为 1.0。当前证据只支持：自适应 Single-Agent/Plan-Executor 在健康 case 少做无关调用；Multi-Agent 的 checkpoint、并行 Span 和交叉验证带来可观察的固定开销。它不支持“Multi-Agent 质量普遍更高”的结论。

## Tool Gateway 实验

| Run | 策略 | Recovery Rate | Invalid Call Rate | P95 延迟 | 平均 Attempts |
| --- | --- | ---: | ---: | ---: | ---: |
| G0 | 无策略 baseline |  |  |  |  |
| G1 | Schema + 分类错误 |  |  |  |  |
| G2 | Timeout + Retry |  |  |  |  |
| G3 | Rate Limit + Circuit Breaker |  |  |  |  |

## Incident Diagnosis 实验

| Run | 故障 | 期望行为 | Root Cause | 写操作 | Trace |
| --- | --- | --- | --- | --- | --- |
| I0 | 无注入故障 | 三源交叉验证 | high | 等待审批 | 完整 |
| I1 | Metrics transient ×1 | Gateway 重试恢复 | high | 等待审批 | 失败+成功 attempt |
| I2 | Runbook timeout ×1 | 超时后重试恢复 | high | 等待审批 | 失败+成功 attempt |
| I3 | Logs permanent | Verifier 阻断 | insufficient | 禁止 | 永久错误 span |

## 失败案例

对每个重要失败记录：输入、期望、实际轨迹、根因、修复和回归测试。
