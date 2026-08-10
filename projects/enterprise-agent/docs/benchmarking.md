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
| Adaptive Harness | Metrics triage 后按风险选择单 Agent 拒答或多 Specialist + Verifier | 企业级质量、成本和风险折中方案 |

默认模式使用无外部 LLM 的确定性 policy adapter，只评估编排和 Agent Infra。Model-backed 模式让五种策略都复用生产路径中的 `IncidentSynthesisAgent`、同一结构化输出契约和 evidence allowlist，避免为 benchmark 单独实现一套“宽松答案解析器”。

## 数据集

`incident-diagnosis-v1` 包含五类 case：

1. payment-api 数据库连接池耗尽；
2. Metrics 瞬时故障后恢复；
3. Runbook 查询超时后恢复；
4. Logs 永久失败，必须拒绝确定性根因和写操作；
5. order-api 健康，必须安全拒绝事故结论。

五条 case 已迁出 `benchmark.py`，位于 `src/enterprise_agent/suites/development` 与 `src/enterprise_agent/suites/regression`。每条任务声明 provenance、许可/敏感性、人工审核状态、expected outcome、required evidence、fault rules 和 recoverability，评分器不读取策略内部状态。`held-out` 是独立目录，默认 benchmark 不加载它。

suite 支持 JSON 和 JSONL，并使用 `schema_version: 1`。Loader 拒绝缺失字段、未知字段、非法 suite kind、重复 task ID 和不支持的 schema version。运行外部 suite：

```bash
PYTHONPATH=src python -m enterprise_agent benchmark \
  --suite path/to/suite-or-directory \
  --output work/custom-suite-report.json
```

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
- Route accuracy：Router 选择是否符合 case 标注的期望路线。
- Escalation recall / false downgrade：应完整验证的 case 是否被升级，以及被错误降级的比例；这是 Active 上线的安全门槛。
- Unnecessary escalation：健康 case 被送入完整验证链路的比例，用于衡量可节省的开销。
- Paired comparison：按 case + repetition 配对，报告候选策略相对 Single-Agent 的成功率、延迟、工具、token 和成本差。
- Bootstrap 95% CI：对成对平均延迟差进行固定种子的非参数重采样；小样本区间只用于暴露不确定性，不替代扩大数据集。

## Benchmark v4 Grader 合同

每个 run 依次执行四个互不抵消的 grader：

- `outcome`：最终环境状态、根因和置信度是否满足 reference outcome；
- `evidence`：required evidence types 的覆盖率；
- `safety`：安全拒答要求与 policy violation，任何违规都是硬失败；
- `recovery`：要求恢复的故障是否恢复，以及是否出现重复副作用。

报告的 `grades` 保存完整 dimension、violations 和 feedback，`grader_passes`/`grader_scores` 提供便于聚合的平坦索引。Task success 要求四个 grader 全部通过，因此延迟、成本或其他高分不能抵消安全失败。

## 隔离与复现

```bash
PYTHONPATH=src python -m enterprise_agent benchmark \
  --repetitions 5 \
  --output work/benchmark-report.json
```

每个 run 创建独立 Tool Runtime 和 SQLite 数据库。报告 schema v4 包含 dataset version、suite provenance、完整 case 定义、环境、四类 grader 的逐维评分和聚合值。使用 `--workspace work/benchmark-runs` 可保留每次运行的 Trace 数据库用于审查。

## Model-backed v2

先做一个较小的核心三策略运行：

```bash
PYTHONPATH=src python -m enterprise_agent benchmark \
  --llm-provider deepseek \
  --model deepseek-v4-flash \
  --thinking disabled \
  --strategies single_agent multi_agent adaptive_harness \
  --repetitions 2 \
  --max-model-calls 18 \
  --workspace work/deepseek-benchmark-runs \
  --output work/deepseek-benchmark-v2.json
```

模型只在 Metrics、Logs、Runbook 已交叉验证的正样本上综合答案；健康或缺证据 case 不调用模型。评测从不授予审批，也不调用 `create_ticket`。默认上限为 16 个模型调用、50,000 total tokens 和 0.10 美元估算成本，可用 `--max-model-calls`、`--max-total-tokens`、`--max-estimated-cost-usd` 收紧。预算在一个完整 case 的所有策略执行完后检查，以保留策略间配对公平性，因此 token/成本可能最多超出一个 case 的实际消耗。

CLI 在每个 run 开始和结束时向 `stderr` 实时输出进度，并立即 flush；完成行包含累计模型调用、token、估算成本和 wall-clock 时间。收到 `Ctrl+C` 后，Runner 会关闭当前 Runtime、保留已经完成的 runs、写出 partial report，并以 `stop_reason: user_interrupt` 和退出码 130 明确区分用户中断与失败。模型预算达到上限后，只跳过后续需要模型的正样本；不调用模型的负样本仍继续执行，以免安全拒答指标被预算调度系统性漏采样。

报告 schema v2 包含 prompt 与非敏感运行配置的 SHA-256 指纹、价格快照和提前停止原因；不包含 API key、完整 prompt、原始响应或 reasoning content。DeepSeek V4 Flash 的价格快照来自[官方价格页](https://api-docs.deepseek.com/quick_start/pricing)，缓存是 best-effort，命中行为应结合[上下文缓存说明](https://api-docs.deepseek.com/guides/kv_cache)解释。

## Benchmark v2.1 方法

默认 `--execution-order counterbalanced` 按 repetition 和 case 轮换策略位置，避免 Single-Agent 永远先请求模型、Multi-Agent 永远后请求模型的顺序混杂。每个 run 记录 repetition 和执行位置。`paired_comparisons` 以 Single-Agent 为 baseline，分别报告全部 case 与实际调用模型的 case；Bootstrap 使用 2,000 次固定种子重采样，保证报告可复现。

`normalized_status` 统一策略内部不同状态：正确正样本为 `completed`，正确负样本为 `safely_refused`，合约或执行错误为 `failed`。`status` 仍保存底层 Runtime 原始值，避免为了评分丢失审计信息。

Adaptive Harness 的 Router 先读取 Metrics：低于事故阈值时走 `single_agent_triage` 并安全拒答；高风险异常走 `verified_multi_agent`，并行调查 Logs/Runbook，经过 Verifier 后才允许模型综合。每次选择同时写入 route、routing reason 和 Span attributes。

Benchmark 与生产 Runtime 共用 `IncidentRiskRouter`，避免离线策略和线上策略漂移。报告同时保留 `expected_route`、`evaluated_route`、`route_correct`、`false_downgrade` 和 `unnecessary_escalation`，可直接形成 Shadow 流量的路由混淆矩阵。

## 当前结果边界

已提交的 v1 报告 [`benchmarks/incident-diagnosis-v1.json`](../benchmarks/incident-diagnosis-v1.json) 来自 5 cases × 5 repetitions。所有策略的正确性指标均为 1.0，说明数据集目前太小且规则化，不能区分推理质量。真实 Model-backed v2.1 的 30/30 runs 同样达到 1.0 质量指标；Adaptive 相对 Single-Agent 的成对延迟差置信区间跨过 0，因此当前证据支持 Shadow 灰度和安全性验证，不支持宣称显著性能提升。
