# Evaluation

> 本文件只记录真实实验；初始模板中的数值保持为空。

## 数据集

- 当前版本：`incident-diagnosis-v2`
- 当前规模：20 个确定性合成 case；默认运行 18 条，held-out 2 条需显式选择
- 数据位置：development 6、regression 4、security 4、recovery 4、held-out 2
- 审核状态：原有 5 条已标记人工审核；新增 15 条 synthetic draft 待人工复核
- 标注：期望根因、证据类型、工具集合、是否应安全拒绝、故障是否可恢复
- 评分：Benchmark v4 独立运行 outcome、evidence、safety、recovery grader，并保存逐 dimension 结果
- 当前用途：验证评测管线和 Agent Infra 语义，不代表真实生产泛化能力
- 后续目标：80–100 个开发/保留集分离的真实脱敏事故 case

## 指标

### Retrieval

- Recall@5 / Recall@10
- MRR
- NDCG
- 引用忠实度与不可回答问题的安全拒答率
- 按语料版本、索引版本和检索阶段报告 P50/P95 延迟

### Memory

- Working Memory 压缩后的必需事实与 evidence ID 保留率
- Episodic Memory precision/recall
- 过期或已删除记忆的错误命中率
- 跨租户泄漏次数
- 历史记忆与实时证据冲突时的错误覆盖次数

### Answer

- 答案正确性
- 引用忠实度
- 无法回答时的拒答质量

### Agent

- 任务成功率
- `pass@1`、连续可靠性 `pass^3`、所有 Trial 全通过的 case 比例
- 每个 case 的成功率和最长连续成功次数
- 工具选择准确率
- 参数准确率
- 平均工具调用次数
- 模型结构化输出合规率与重复预测一致性

`pass^3` 按同一 case/strategy 的连续三次 Trial 窗口计算；不足三次时报告 `null`，不能用单次成功率代替。安全违规由 safety grader 作为硬失败处理，不能被延迟或成本收益抵消。

### System

- P50 / P95 延迟
- 每请求 token 或估算成本
- Prompt cache hit/miss tokens 与模型 attempt 数
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

2026-08-11 R0–R2：CPython 3.12.2、macOS arm64，项目自编 `incident-runbooks:1.0.0` 合成语料 10 份，`incident-retrieval-smoke:1.0.0` 共 30 条（26 条正样本、4 条范围/空结果负样本）。完整逐 case 结果见 [`retrieval-dense-v1.json`](benchmarks/retrieval-dense-v1.json)、[`retrieval-bm25-v1.json`](benchmarks/retrieval-bm25-v1.json) 和 [`retrieval-hybrid-v1.json`](benchmarks/retrieval-hybrid-v1.json)。

R0 使用无外部依赖的 `hashing-char-ngram-v1`，用于验证语料、Chunk、Evidence、租户过滤和评测合同，不是学习型语义 Embedding。正样本评测在全语料上排序；负样本验证租户和 metadata scope。`payment-pool-action` 未进入 Dense Top-5，是当前明确 badcase；BM25 和 RRF 修复了 Top-5 召回。BM25 在这组短、术语密集的合成文档上 MRR/NDCG 最好，RRF 被较弱的 hashing Dense 拖累，因此 Runtime 当前选择 BM25。该结果不支持外推到真实企业语料。

| Run | 方法 | Recall@5 | MRR | NDCG@5 | 空结果准确率 | P95 ms | 备注 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| R0 | Hashing Dense baseline | 0.9615 | 0.8590 | 0.8855 | 1.0000 | 0.192 | 1 个 Top-5 badcase |
| R1 | BM25 | 1.0000 | 0.9327 | 0.9497 | 1.0000 | 0.127 | 当前 Runtime 默认 |
| R2 | Dense + BM25 RRF | 1.0000 | 0.8923 | 0.9196 | 1.0000 | 0.372 | 召回恢复，排序弱于 BM25 |
| R3 | Hybrid + Reranker |  |  |  |  |  | 计划中 |

## Memory 实验

2026-08-11 M0–M3：确定性合同测试 10 条全部通过；同一次全量回归共 103 条测试全部通过。该结果验证 Runtime/Store 安全合同，不代表已经完成基于人工标注相似事故集的语义召回评测。

| Run | 场景 | Memory Precision | 必需事实保留率 | Policy Violations | 备注 |
| --- | --- | ---: | ---: | ---: | --- |
| M0 | Working Memory 压缩 | 1.0000 | 1.0000 | 0 | facts、evidence、plan、open items 精确保留；Runtime artifacts 不进入模型视图 |
| M1 | 同服务历史事故召回 | 1.0000 | 1.0000 | 0 | 候选 ID 随 Task Snapshot 冻结；历史内容保持 `untrusted` |
| M2 | 过期/删除/冲突记忆 | 1.0000 | 1.0000 | 0 | 到期边界不命中；删除后不复活；实时证据优先并记录冲突事件 |
| M3 | 跨租户隔离 | 1.0000 | 1.0000 | 0 | 查询、ID 读取和删除均要求显式 tenant scope；测试 canary 无泄漏 |

此外验证了 Episode 与终态 checkpoint/审计事件原子提交、写入故障整体回滚、恢复后恰好写入一次，以及 `WAITING_APPROVAL`、`SAFELY_REFUSED`、`FAILED`、business task 和缺少 Verifier 的结果均不写入 Episode。复现命令：`PYTHONPATH=src python -m unittest tests.test_memory -v`。

## Agent 实验

2026-08-11 确定性 v2：CPython 3.12.2、macOS arm64、18 cases × 3 repetitions × 5 strategies，共 270 runs。完整逐次结果见 [`benchmarks/incident-diagnosis-v2.json`](benchmarks/incident-diagnosis-v2.json)。

| 方法 | `pass@1` | `pass^3` | 工具 Precision | P50 ms | P95 ms | 平均 attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Fixed Workflow | 0.9444 | 0.9444 | 0.8704 | 1.078 | 86.072 | 3.2222 |
| Single Agent | 1.0000 | 1.0000 | 1.0000 | 1.301 | 86.327 | 2.8333 |
| Plan-Executor | 1.0000 | 1.0000 | 1.0000 | 1.699 | 86.844 | 2.8333 |
| Multi-Agent | 0.9444 | 0.9444 | 0.8148 | 8.818 | 87.400 | 3.3889 |
| Adaptive Harness | 1.0000 | 1.0000 | 0.9259 | 3.215 | 87.358 | 3.0556 |

Fixed Workflow 与固定 Multi-Agent 的失败均来自 `admin-api-tenant-scope`：它们在健康指标之后继续读取任务明确禁止的 Logs/Runbook，因此 outcome/evidence 通过但 safety grader 硬失败。自适应三策略在 Metrics triage 后停止，没有越过租户工具范围。所有策略的 safe-refusal、evidence completeness 与 retry recovery 均为 1.0。

该结果只验证 synthetic contract 与评测器能区分策略，不能视为真实生产泛化结论。新增 15 条尚未完成人工 reference review；held-out 规模也不足以支持调参外推。

2026-08-10 本机基线：CPython 3.12.2、macOS arm64、5 cases × 5 repetitions。完整逐次结果见 [`benchmarks/incident-diagnosis-v1.json`](benchmarks/incident-diagnosis-v1.json)。

| Run | 方法 | 任务成功率 | 工具 Precision | P50 ms | P95 ms | 平均 attempts |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| A0 | Fixed Workflow | 1.000 | 0.867 | 2.866 | 85.771 | 3.2 |
| A1 | Single Agent | 1.000 | 1.000 | 2.374 | 86.471 | 2.8 |
| A2 | Plan-Executor | 1.000 | 1.000 | 2.744 | 89.901 | 2.8 |
| A3 | Multi-Agent | 1.000 | 0.800 | 9.515 | 89.395 | 3.4 |

四种策略在这个小型确定性数据集上的根因准确率、安全拒绝率、证据完整率和故障恢复率均为 1.0。当前证据只支持：自适应 Single-Agent/Plan-Executor 在健康 case 少做无关调用；Multi-Agent 的 checkpoint、并行 Span 和交叉验证带来可观察的固定开销。它不支持“Multi-Agent 质量普遍更高”的结论。

当前已接入 DeepSeek V4 Flash Provider 与 Model-backed Benchmark v2，但已提交的基准仍标记为 `deterministic_synthetic_no_llm`。2026-08-10 的单次真实 smoke 为 67 tokens / 1562 ms，单次事故综合为 553 tokens / 2998 ms，且审批恢复没有重复调用模型；这些数字只用于验证调用、usage 和恢复链路。在重复运行并保存 v2 报告前，不把 mock 测试或单样本数字写成模型效果、方差或成本结论。

2026-08-10 的 2 repetitions × Single-Agent/Multi-Agent Model-backed 报告完成 20/20 runs、12 次模型调用，Schema 合规率与任务成功率均为 1.0，总估算成本约 0.000785 美元。由于当时策略执行顺序固定且样本量仅为 2，该结果只作为 v2 基线。

同日 v2.1 使用交叉平衡顺序运行 Single-Agent、Multi-Agent、Adaptive Harness，完成 30/30 runs、18 次模型调用、11,136 tokens，估算成本 0.001187 美元。三种策略的任务成功率、根因准确率、安全拒答率、证据完整性和 Schema 合规率均为 1.0。Adaptive 的工具 Precision 为 0.933、平均 attempts 为 3.0；Multi-Agent 分别为 0.800 和 3.4。Adaptive 相对 Single-Agent 的全部 case 平均延迟差为 +18.1ms，Bootstrap 95% CI 为 [-222.5, 241.0]ms；模型 case 为 +28.7ms，CI 为 [-387.5, 390.4]ms，无法判定存在稳定延迟优势。路由分布为 triage 2 次、完整验证 8 次。结论：可进入 Shadow，但小样本不能支持直接全量 Active 或泛化质量声明。

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
