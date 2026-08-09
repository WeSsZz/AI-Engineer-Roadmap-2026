# Agent Architecture Benchmark

## 目标

评测器用于回答一个具体问题：在相同事故、工具策略和故障条件下，不同编排架构的质量、效率和恢复行为有什么差异。它不是用一个成功 Demo 为 Multi-Agent 背书。

## 策略定义

| 策略 | 执行方式 | 当前定位 |
| --- | --- | --- |
| Fixed Workflow | 固定顺序调用 Metrics → Logs → Runbook | 简单、可预测的 baseline |
| Single Agent | 单个 Generalist 根据前一步结果决定是否继续 | 自适应低开销 baseline |
| Plan-Executor | Planner 产生显式计划，Executor 自适应执行 | 规划与执行边界 baseline |
| Multi-Agent | 三个 Specialist 并行，Verifier 交叉验证，Synthesis 汇总 | 隔离上下文、审计和恢复能力 baseline |

当前策略是无外部 LLM 的确定性 policy adapter，只评估编排和 Agent Infra。接入真实模型后必须增加 token、成本、重复运行方差和语义评分。

## 数据集

`incident-diagnosis-v1` 包含五类 case：

1. payment-api 数据库连接池耗尽；
2. Metrics 瞬时故障后恢复；
3. Runbook 查询超时后恢复；
4. Logs 永久失败，必须拒绝确定性根因和写操作；
5. order-api 健康，必须安全拒绝事故结论。

每个 case 声明 expected root cause、required evidence types、expected tools、fault rules 和 recoverability，评分器不读取策略内部状态。

## 指标契约

- Task success：正样本根因正确且 confidence 为 high；负样本根因为 null 且明确安全拒绝。
- Root-cause accuracy：预测根因是否等于标注，包括应无根因的 case。
- Safe-refusal rate：负样本没有生成确定性根因或待审批动作。
- Evidence completeness：期望证据类型被覆盖的比例。
- Tool precision/recall：实际选用工具与 case 标注工具集合的集合指标。
- Tool attempts：包括失败和重试 attempt。
- Retry recovery rate：可恢复故障出现失败 attempt 后是否最终正确完成。
- P50/P95：本机 wall-clock 延迟，采用 nearest-rank；只适合同环境相对比较。

## 隔离与复现

```bash
PYTHONPATH=src python -m enterprise_agent benchmark \
  --repetitions 5 \
  --output work/benchmark-report.json
```

每个 run 创建独立 Tool Runtime 和 SQLite 数据库。报告包含 schema version、dataset version、完整 case 定义、环境、逐次评分和聚合值。使用 `--workspace work/benchmark-runs` 可保留每次运行的 Trace 数据库用于审查。

## 当前结果边界

已提交报告 [`benchmarks/incident-diagnosis-v1.json`](../benchmarks/incident-diagnosis-v1.json) 来自 5 cases × 5 repetitions。所有策略的正确性指标均为 1.0，说明数据集目前太小且规则化，不能区分推理质量。它已经能揭示工具选择和固定运行时开销；下一步必须扩充真实脱敏 case 并接入模型，才能形成有统计意义的架构结论。
