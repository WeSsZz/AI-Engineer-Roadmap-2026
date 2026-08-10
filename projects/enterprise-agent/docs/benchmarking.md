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

默认模式使用无外部 LLM 的确定性 policy adapter，只评估编排和 Agent Infra。Model-backed 模式让四种策略都复用生产路径中的 `IncidentSynthesisAgent`、同一结构化输出契约和 evidence allowlist，避免为 benchmark 单独实现一套“宽松答案解析器”。

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
- Model schema compliance：模型输出是否通过根因、置信度、字段类型和证据引用约束。
- Token / cache：prompt、completion、total、cache hit/miss token 及命中率。
- Model latency：每个模型 attempt 的总耗时、均值和总体标准差。
- Estimated cost：按报告内固定的价格快照估算，不等同于账单。
- Prediction consistency：同一策略、同一 case 重复运行时，众数预测所占比例。

## 隔离与复现

```bash
PYTHONPATH=src python -m enterprise_agent benchmark \
  --repetitions 5 \
  --output work/benchmark-report.json
```

每个 run 创建独立 Tool Runtime 和 SQLite 数据库。报告包含 schema version、dataset version、完整 case 定义、环境、逐次评分和聚合值。使用 `--workspace work/benchmark-runs` 可保留每次运行的 Trace 数据库用于审查。

## Model-backed v2

先做一个较小的双策略运行：

```bash
PYTHONPATH=src python -m enterprise_agent benchmark \
  --llm-provider deepseek \
  --model deepseek-v4-flash \
  --thinking disabled \
  --strategies single_agent multi_agent \
  --repetitions 2 \
  --workspace work/deepseek-benchmark-runs \
  --output work/deepseek-benchmark-v2.json
```

模型只在 Metrics、Logs、Runbook 已交叉验证的正样本上综合答案；健康或缺证据 case 不调用模型。评测从不授予审批，也不调用 `create_ticket`。默认上限为 16 个模型调用、50,000 total tokens 和 0.10 美元估算成本，可用 `--max-model-calls`、`--max-total-tokens`、`--max-estimated-cost-usd` 收紧。预算在一个完整 case 的所有策略执行完后检查，以保留策略间配对公平性，因此 token/成本可能最多超出一个 case 的实际消耗。

报告 schema v2 包含 prompt 与非敏感运行配置的 SHA-256 指纹、价格快照和提前停止原因；不包含 API key、完整 prompt、原始响应或 reasoning content。DeepSeek V4 Flash 的价格快照来自[官方价格页](https://api-docs.deepseek.com/quick_start/pricing)，缓存是 best-effort，命中行为应结合[上下文缓存说明](https://api-docs.deepseek.com/guides/kv_cache)解释。

## 当前结果边界

已提交的 v1 报告 [`benchmarks/incident-diagnosis-v1.json`](../benchmarks/incident-diagnosis-v1.json) 来自 5 cases × 5 repetitions。所有策略的正确性指标均为 1.0，说明数据集目前太小且规则化，不能区分推理质量。Model-backed v2 管线已由 mock transport 回归测试覆盖；真实模型结果必须单独运行并保留报告，不能用 mock 数字声称模型效果。
