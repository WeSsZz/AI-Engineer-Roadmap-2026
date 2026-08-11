# Reliable Enterprise Agent：RAG、Memory、Runtime 与 Evaluation 升级规划

## 1. 项目定位

本项目不扩展为泛化的“企业 Multi-Agent 平台”，而是聚焦一个可验证的问题：

> Agent 如何从企业知识与历史经验中获得正确、可追溯的上下文，并在模型不稳定、工具失败、Worker 崩溃或任务涉及副作用时仍然可恢复、可审计、可评测？

目标用户是构建企业 Agent、AI Platform、Evals & Environments 与 Post-training Infrastructure 的工程团队。当前 Git 主线是唯一开发基线；历史阶段由提交和 tag 保存，不再创建 `work/phase*` 代码副本。

## 2. 产品边界

### 核心能力

1. Evaluated RAG：文档摄取、元数据、Dense/BM25、融合、重排、引用与版本化检索评测。
2. Controlled Memory：当前任务 Working Memory，以及只保存已验证事故的租户隔离 Episodic Memory。
3. Durable Runtime：状态机、checkpoint、resume、deadline、cancellation、lease 与幂等副作用。
4. Tool Runtime：Schema、权限、审批、身份、超时、重试、熔断、隔离与审计。
5. Evaluation Harness：Task、Trial、Transcript、Outcome、Grader、Suite 与多次运行可靠性。
6. Observability & Replay：Trace、语料/索引/记忆版本指纹、OpenTelemetry、离线回放与策略对比。

### Learning-ready 接口

项目只负责稳定地产生、评分和回放轨迹：

- trajectory export；
- evaluator/reward hooks；
- prompt/model/policy version；
- shadow/canary comparison；
- trainer adapter boundary。

项目不实现 PPO、GRPO 或通用 RL Trainer，也不把少量运维合成数据包装成训练数据集。

### 暂不做

- 为展示数量增加更多 Specialist；
- GraphRAG、多个向量数据库、无限增长的聊天记录或复杂用户画像；
- 未经验证自动写入长期记忆，或允许历史记忆覆盖实时证据；
- 复杂前端；
- 自研大模型训练；
- 在没有可靠数据、奖励和 held-out 评测前宣称 Agentic RL 效果。

## 3. 目标架构

```text
Environment: Incident Response
        │ task / initial state
        ▼
Knowledge & Memory Layer
        ├── Versioned Corpus → BM25 + Dense → RRF → Reranker → Evidence
        ├── Working Memory → facts / evidence / plan / unresolved items
        └── Episodic Memory → verified incidents / tenant / retention
        │
        ▼
Durable Runtime ─── Worker lease / checkpoint / cancellation
        │
        ├── Tool Runtime ─── identity / allowlist / approval / sandbox
        │
        ├── Trace & Event Store ─── spans / versions / audit log
        │
        └── Outcome Store ─── final environment truth / side effects
        │
        ▼
Evaluation Harness
Task → Trial → Transcript + Outcome → Graders → Suite Report
        │
        └── trajectory export / replay / shadow comparison
```

安全规则、审批、状态转换、租户范围和幂等键始终位于策略之外，不能由模型、检索结果、历史记忆或学习策略覆盖。

## 4. 正式评测合同

- `Task`：输入、初始状态、成功条件、来源和风险标签。
- `Trial`：Task 的一次独立运行，绑定 seed、策略、prompt 与 model 版本。
- `Transcript`：模型、工具、状态转换和人工节点的可审计轨迹。
- `Outcome`：数据库、工单和环境的最终真实状态，而不是 Agent 自述。
- `Grader`：分别评分 outcome、证据、安全、恢复和成本。
- `EvaluationSuite`：明确标记 capability、regression、held-out、security 或 recovery。

主指标：

- `pass@1`：单次运行成功率；
- `pass^3`：任意连续三次都成功的窗口比例；
- fully reliable case rate：所有 Trial 都通过的 case 比例；
- safe-refusal、false downgrade、evidence completeness；
- recovery success、duplicate side effects、policy violations；
- P50/P95 延迟、token、成本和工具 attempts。
- retrieval Recall@K、MRR、NDCG、引用忠实度与空结果拒答；
- memory precision、过期信息命中率、跨租户泄漏和历史记忆覆盖实时证据次数。

`pass@k` 只用于“多次尝试至少一次成功”的能力分析，不能替代生产可靠性指标。

## 5. 数据集规划

第一目标是 60 条人工审核的高质量 Eval Task，而不是几千条模板变体：

| Suite | 数量 | 重点 |
| --- | ---: | --- |
| capability | 20 | 常见事故、健康检查、证据充分的根因分析 |
| regression | 15 | 已发现 badcase 与修复后的固定回归 |
| security | 10 | Prompt Injection、越权工具、未审批写操作 |
| recovery | 10 | timeout、响应丢失、崩溃、重复执行、多 Worker 竞争 |
| held-out | 5 | 不参与策略和 grader 调试的困难保留集 |

每条任务保存 provenance、许可/敏感性标记、reference outcome、grader 版本和人工复核状态。合成数据、公开事故改写和真实脱敏数据分别报告。

RAG 使用独立的 80–100 条检索标注问题，保存相关文档/chunk、时间有效性和不可回答标签；Memory 使用独立的多轮事故序列，覆盖正确召回、过期记忆、冲突证据、跨租户访问与删除后不可召回。检索集、Agent suite 和 held-out 的用途必须分开报告。

## 6. 实施里程碑

### M1：Eval Contracts 与可靠性报告（已完成）

- 新增正式合同与 suite 类型；
- Benchmark v3 报告 `pass@1`、`pass^3`、case 稳定性和全通过率；
- 使用测试固定 score 边界与安全 grader 的硬失败语义。

验收：全部测试通过；单次运行时 `pass^3` 明确为 `null`，不伪造统计量。

### M2：数据集分层与 Grader 解耦

- JSON/JSONL suite loader + Schema 校验（已完成）；
- outcome、evidence、safety、recovery grader 独立实现（已完成）；
- development/regression/security/recovery/held-out 目录与数据 provenance（已完成）；
- 20 条目录已完成：5 条已审核，15 条 synthetic draft 待人工复核；复核后再扩至 60 条。

验收：新增 case 不改策略代码；每个失败能定位到 grader dimension。

### M3：Evaluated RAG Baseline

状态：进行中。已完成版本化合成语料、确定性切分、Hashing Dense、BM25、RRF、30 条 smoke set、评测 CLI 和 Tool Gateway 集成；公开语料、学习型 Embedding、Reranker 与引用忠实度尚未完成。当前数据上 BM25 优于 RRF，Runtime 暂选 BM25，待 Embedding 升级后复评。

- 冻结公开可分享的 Runbook、事故复盘和运维 SOP 语料；
- 文档解析、chunk metadata、Dense baseline 与带引用 Evidence；
- 增加 BM25、RRF 和可选 Reranker；
- 建立独立检索标注集和 Recall@K、MRR、NDCG、引用忠实度报告；
- 将 `get_runbook` 从确定性数据替换为经过 Tool Gateway 的 `search_knowledge`。

验收：新增文档不改 Agent 代码；每个答案引用可回溯到语料和索引版本；至少完成 Dense、BM25、Hybrid 三组可复现实验。

### M4：Controlled Memory

状态：已完成最小闭环。Working Memory、已验证事故 Episode、原子终态写入、tenant/TTL/tombstone/provenance 合同与确定性回归测试已落地；语义相似召回、Episode 版本替换和独立人工审核多轮 suite 留到下一轮。

- 在 Context Engine 上固化结构化 Working Memory：事实、证据、计划、未决问题和最近事件；
- 仅在任务完成且验证通过后写入 Episodic Memory；
- 增加 tenant scope、provenance、时间有效性、retention 和删除合同；
- 历史记忆作为候选 Evidence，不能覆盖本次 Metrics/Logs 等实时事实；
- 增加冲突、过期、删除和跨租户回归测试。

验收：跨租户泄漏为 0；删除后不可召回；旧事故与实时证据冲突时以实时证据为准并留下 Trace。

### M5：Worker Control Plane

- PostgreSQL store；
- `FOR UPDATE SKIP LOCKED` 领取任务；
- lease、heartbeat、过期抢占、deadline 和 cancellation；
- 双 Worker 竞争、kill-and-resume 与取消传播测试。

验收：任意关键 checkpoint 窗口 kill Worker 后可恢复；两个 Worker 不重复完成同一写动作。

### M6：安全 Tool Runtime

- read/write 风险分级、per-agent allowlist、per-task identity；
- scoped credential、审批与 action 参数绑定；
- 不可信工具输出标记与 Prompt Injection 回归集；
- 进程/容器超时、工作目录和网络边界。

验收：未审批写入、跨租户访问和被篡改 action 始终被阻止并产生审计事件。

### M7：Observability、Replay 与真实连接器

- OpenTelemetry exporter 与版本化 span attributes；
- deterministic tool replay、shadow policy diff；
- 一个真实 Metrics/Logs 数据源和一个工单系统适配器；
- Docker Compose 一键启动。

验收：可从 trace 还原任务决策和副作用；新旧策略在同一 suite 上生成可比较报告。

### M8：Learning-ready Boundary

- 稳定 JSONL trajectory schema；
- reward/evaluator hook 与 policy registry；
- 导出数据脱敏和 retention policy；
- 为独立 `coding-agent-rl-lab` 提供可选 adapter，只共享 Task、Trajectory、Reward 和 PolicyManifest 合同；
- Coding Environment、Verifier、rollout 和训练循环不进入本仓库。

验收：外部训练/优化系统无需修改 Runtime 核心即可消费轨迹并回灌一个新策略版本。

## 7. 双项目边界

本仓库只解决生产 Agent 的可追溯知识获取、受控记忆、可靠执行与评测。Agentic RL 作为独立项目 `coding-agent-rl-lab` 维护，负责 Coding Task、sandbox、executable verifier、rollout、reward 和策略优化。

两者未来只通过版本化 JSON 合同可选对接，不能产生运行时依赖，也不共享业务实现。这样可以分别验证系统工程能力与学习闭环，避免运维合成数据和训练逻辑污染 Durable Runtime 的产品边界。

## 8. 项目完成标准

满足以下条件后停止加功能，转向发布、求职和开源协作：

- 60 条审核任务可重复运行并生成版本化报告；
- 至少 3 次重复评测能报告 `pass^3` 与失败分布；
- 独立检索集完成 Dense、BM25、Hybrid 和 Rerank 对比，答案引用可回溯到语料与索引版本；
- Working Memory 压缩后保留必需事实，Episodic Memory 满足租户隔离、过期和删除合同；
- 进程崩溃、工具超时、响应丢失和双 Worker 竞争均有恢复测试；
- 重复高风险副作用为 0，未审批写操作阻断率为 100%；
- 至少一个真实观测数据源和一个真实写连接器；
- Docker Compose 可复现，Trace 能解释失败与策略版本；
- README 中的每个效果数字都能追溯到提交的报告。

## 9. 当前下一步

按优先级执行：

1. 冻结首批公开运维语料及其许可、来源、文档版本和元数据合同；
2. 用公开语料和学习型 Embedding 复评 Dense、BM25、RRF，再增加 Reranker 与引用忠实度；
3. 为 Memory 建立独立多轮 suite，加入语义相似召回、Episode 版本替换和更多人工审核冲突样本；
4. 同步人工复核现有 15 条 Agent synthetic draft，保持 Agent、检索和 Memory 评测集相互独立；
5. 数据合同稳定后开始 PostgreSQL、Worker lease、OpenTelemetry、离线 replay 和真实连接器。
