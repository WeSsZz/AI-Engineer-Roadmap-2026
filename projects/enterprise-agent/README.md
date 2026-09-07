# Enterprise Knowledge & Operations Agent Runtime

## 项目目标

构建一个面向企业知识库、业务查询和运维故障分析的可靠 Agent Runtime，重点展示：

- 可评测的混合检索、重排与证据引用
- 结构化 Working Memory 与受控 Episodic Memory
- Multi-Agent 编排与证据验证
- 可恢复的任务执行
- 可靠工具调用与人工审批
- 上下文管理、Trace 和故障恢复

项目重点不是“多个模型互相聊天”，而是让 Agent 获得正确、可追溯的上下文，并让一次任务具备清晰的执行状态、权限边界、证据链、checkpoint、进程重启恢复和写操作幂等语义。RAG 与 Memory 都必须经过 Tool Gateway、租户范围、Trace 和评测约束，不能成为绕过安全边界的旁路。

## 当前可运行基线

当前版本使用确定性 Demo 数据，不依赖外部模型或密钥，已经实现：

- Incident Supervisor 并行调度 Metrics、Logs 和 Runbook 三个 Specialist
- 保留 Knowledge Researcher + Data Analyst 业务分析场景作为对照基线
- Verifier 在合成答案前检查证据和失败子任务
- SQLite 持久化 Task Snapshot 和 append-only Event Log
- 有限状态机约束任务生命周期
- `resume(task_id)` 从最近 checkpoint 恢复
- checkpoint 乐观锁，阻止两个 Worker 静默覆盖状态
- 高风险动作先持久化，再绑定 `action_id` 等待人工审批
- 写工具使用持久化 idempotency key
- Tool Gateway 使用显式参数 Schema，拒绝缺失、未知、类型错误和非法参数
- 按瞬时错误、永久错误、超时、权限和容量错误执行不同策略
- 工具级超时、指数退避重试、并发上限、固定窗口限流和熔断
- 每次工具尝试记录 call ID、task ID、耗时、错误类型和幂等键
- Runtime、Agent、Tool 统一持久化为带父子关系的 SQLite Span
- `run` 与每次 `resume` 分别形成根 Span，进程重启后仍归属于同一 task trace
- 任务、Span 类型和操作名维度输出错误率、P50、P95 延迟
- Context Engine 保留结构化事实、证据引用和最近事件
- failpoint 模拟进程在关键故障窗口中断
- Fault Injector 可确定性模拟 transient、permanent 和 timeout 数据源故障
- Metrics、Logs、Runbook 证据交叉一致后才输出高置信根因
- Fixed Workflow、Single-Agent、Plan-Executor、Multi-Agent、Adaptive Harness 统一评测器
- 版本化事故数据集、隔离运行目录和可复现 JSON benchmark report
- DeepSeek V4 Flash 结构化综合适配器，保留 deterministic 模式
- Model attempt Span 记录模型、延迟、token 和 prompt cache 命中
- Model-backed Benchmark v2 记录 token、估算成本、Schema 合规率、缓存命中率和重复运行方差
- 模型调用数、总 token 和估算成本三重预算，防止评测失控
- Adaptive Harness 根据指标风险在单 Agent triage 和多 Specialist 验证之间做可审计升级
- Benchmark v2.1 交叉平衡执行顺序，并输出成对差异与 Bootstrap 95% 置信区间
- 生产 Runtime 支持 `fixed_multi`、`adaptive_shadow`、`adaptive` 三种持久化编排模式
- Adaptive Router 决策进入 checkpoint、Event Log 和 Trace；信号缺失时 fail-closed 到完整验证链路
- 安全拒答使用一等终态 `safely_refused`，不与依赖、模型合约或 Runtime 故障混为 `failed`
- 正式定义 Task、Trial、Transcript、Outcome、Grader 与 EvaluationSuite 合同
- Benchmark v3 报告 `pass@1`、连续可靠性 `pass^3`、全通过 case 比例和逐 case 稳定性
- JSON/JSONL suite loader 执行版本、字段类型、未知字段和跨 suite task ID 校验
- `incident-diagnosis-v2` 包含 20 条分层 case：development 6、regression 4、security 4、recovery 4、held-out 2
- 原有 5 条标记为人工审核；新增 15 条 synthetic draft 明确标记为待人工复核
- outcome、evidence、safety、recovery 四类 grader 独立评分，Benchmark v4 逐 run 保存 dimension、违规和反馈
- 版本化 `incident-runbooks:1.0.0` 合成语料、稳定 chunk ID 和严格字段校验
- 依赖无关的 Hashing Dense、BM25 与 RRF Hybrid 三组基线，明确不把 hashing 冒充学习型语义 Embedding
- 可选的 Sentence Transformers 学习型 Embedding，已用固定 revision 的 BGE-M3 在 GPU 上完成 Dense 与 Hybrid 可复现实验
- 30 条合成 retrieval smoke set，以及 20 篇公开许可文档和 44 条来源导出 draft case
- 固定 Git 提交、逐文件哈希、离线重建、语料/suite 强绑定与模型快照指纹
- 文档级 Recall@5、MRR、NDCG、范围隔离、语义拒答和 serving/exhaustive 延迟分离报告
- `search_knowledge` 通过 Tool Gateway 执行租户/元数据过滤，并将索引版本、分数和来源写入 Evidence
- 原 `get_runbook` 已成为版本化检索索引的兼容适配器，现有 Agent suite 与故障注入合同保持稳定
- Context Engine 输出不含审批、工单等 Runtime artifacts 的结构化 Working Memory
- 每个任务在首次执行前冻结同租户、同服务的历史候选 ID，resume 不会静默换一批记忆
- 仅将 `COMPLETED + Verifier 通过 + high-confidence root cause` 的事故写入 Episodic Memory
- 终态 Task、审计事件和 Episode 在同一 SQLite 事务中提交，崩溃恢复不会产生半写状态
- Episodic Memory 保存 task/checkpoint、证据 provenance、有效期和 tombstone，并提供租户隔离的查询与删除 CLI
- 历史记忆始终标记为 `historical + untrusted`；与本次实时证据冲突时实时证据优先并记录 Trace

## 当前能力边界

RAG 的确定性 R0–R2、公开 Markdown 摄取与学习型 Embedding 对比已经实现，但以下能力仍属于下一阶段，不能作为当前成果宣传：

- PDF/站点模板语义解析与持久化向量索引
- Cross-Encoder Reranker 与经过真实数据验证的 Hybrid 链路
- 80–100 条人工审核的开发/保留检索集与引用忠实度评测
- Episodic Memory 的语义相似度召回、版本替换、独立多轮评测集和人工审核数据

公开实验使用 Kubernetes 与 Prometheus 的 20 篇官方 Markdown 文档，以及 40 条来源导出正例和 4 条范围隔离负例。问题由文档内容编写，尚未盲审、未做 pooled judgment，也没有同域语义 hard negative，因此它只是可复现 smoke set，不是 held-out 质量结论。BGE-M3 Dense 在这套草案上排名最高；Hybrid 没有超过 Dense，`scope_isolation_accuracy=1.0` 也不代表安全拒答能力。Runtime 仍默认使用无需模型依赖的 BM25。

Episodic Memory v1 只按可信 tenant identity、service 和时间倒序召回，不能等同于已经完成语义 Memory Search。

当前演示任务：

> 诊断 payment-api 错误率和延迟突增的根因，并生成故障工单。

Demo 会识别数据库连接池耗尽，给出证据绑定的 Runbook 建议，并在创建工单前停在人工审批点。

## 快速运行

```bash
PYTHONPATH=src python -m enterprise_agent --db work/enterprise_agent.db run --tenant-id demo
```

查看或显式删除当前有效的历史事故记忆：

```bash
PYTHONPATH=src python -m enterprise_agent --db work/enterprise_agent.db \
  memory-list --tenant-id demo --service payment-api

PYTHONPATH=src python -m enterprise_agent --db work/enterprise_agent.db \
  memory-forget episode:task-xxxxxxxx --tenant-id demo
```

注入一次瞬时 Metrics 故障，验证重试与 Trace：

```bash
PYTHONPATH=src python -m enterprise_agent \
  --db work/fault-demo.db run \
  --inject-fault query_service_metrics:transient
```

格式为 `TOOL:MODE[:TIMES]`，mode 支持 `transient`、`permanent`、`timeout`。

配置 DeepSeek V4 Flash（密钥只从环境读取）：

```bash
export DEEPSEEK_API_KEY="your-key"

PYTHONPATH=src python -m enterprise_agent model-smoke

PYTHONPATH=src python -m enterprise_agent \
  --db work/deepseek-agent.db run \
  --llm-provider deepseek \
  --model deepseek-v4-flash \
  --thinking disabled
```

模型当前只综合已经通过 Verifier 的证据；它不能直接调用企业写工具、授予审批或扩展 evidence allowlist。默认仍为 `deterministic`，因此普通测试和默认 benchmark 不需要密钥。

先以 Shadow 模式验证 Adaptive Router，不改变实际 Specialist 调度：

```bash
PYTHONPATH=src python -m enterprise_agent \
  --db work/adaptive-shadow.db run \
  --scenario incident \
  --orchestration adaptive_shadow
```

确认路由准确率和安全指标后启用 Active 模式：

```bash
PYTHONPATH=src python -m enterprise_agent \
  --db work/adaptive-active.db run \
  --scenario incident \
  --orchestration adaptive
```

Active 模式先运行 Metrics Investigator。健康信号走 `single_agent_triage`；事故信号或指标不可用走 `verified_multi_agent`。`resume TASK_ID` 会读取任务中已持久化的模式和路由决定，无需再次传参。默认仍是 `fixed_multi`。

默认停在 `needs_approval`，输出包含 `task_id` 和已经固定的 `pending_action`。审批并恢复：

```bash
PYTHONPATH=src python -m enterprise_agent \
  --db work/enterprise_agent.db \
  resume TASK_ID --approve --trace
```

查看任务：

```bash
PYTHONPATH=src python -m enterprise_agent --db work/enterprise_agent.db list
```

查询持久化 Trace 与聚合指标：

```bash
PYTHONPATH=src python -m enterprise_agent \
  --db work/enterprise_agent.db trace TASK_ID
```

运行五种架构的对比评测：

```bash
PYTHONPATH=src python -m enterprise_agent benchmark \
  --repetitions 5 \
  --output work/benchmark-report.json
```

运行当前确定性 Dense 检索基线：

```bash
PYTHONPATH=src python -m enterprise_agent retrieval-benchmark \
  --strategy bm25 \
  --output work/retrieval-baseline.json
```

`--strategy` 支持 `dense`、`bm25` 和 `hybrid`。默认加载 10 份项目自编合成文档和 30 条 retrieval smoke case。报告会记录语料、suite、Embedding 和索引版本；也可以用 `--corpus`、`--suite` 和 `--top-k` 加载外部版本化数据。

构建首批公开语料（首次运行会从两个 HTTPS Git 仓库获取固定提交）：

```bash
PYTHONPATH=src python -m enterprise_agent corpus-build \
  --manifest src/enterprise_agent/corpus_manifests/public-ops-v1.json \
  --cache-dir work/corpus-cache \
  --output work/corpora/public-ops-v1.json

PYTHONPATH=src python -m enterprise_agent corpus-build \
  --manifest src/enterprise_agent/corpus_manifests/public-ops-v1.json \
  --cache-dir work/corpus-cache \
  --output work/corpora/public-ops-v1.json \
  --offline --overwrite
```

完整第三方正文和裸 Git 缓存只保存在已忽略的 `work/`；仓库只跟踪 manifest、评测集和默认脱敏的报告。许可、署名、NOTICE 与再分发边界见 [公开语料说明](docs/public-corpus.md)。

安装可选依赖后，可用固定模型 revision 复现实验；`--embedding-local-path` 可指向已下载模型，从而避免运行时联网：

```bash
python -m pip install -e '.[rag]'

PYTHONPATH=src python -m enterprise_agent retrieval-benchmark \
  --strategy hybrid \
  --embedding-model BAAI/bge-m3 \
  --embedding-revision 5617a9f61b028005a4858fdac845db406aefb181 \
  --embedding-local-path work/models/bge-m3 \
  --embedding-device cuda \
  --output work/retrieval-bge-m3-hybrid.json
```

公开语料报告见 [BGE-M3 Dense](benchmarks/retrieval-public-ops-bge-m3-dense-v2.json)、[BGE-M3 Hybrid](benchmarks/retrieval-public-ops-bge-m3-hybrid-v2.json)、[BM25](benchmarks/retrieval-public-ops-bm25-v2.json)、[Hashing Dense](benchmarks/retrieval-public-ops-hashing-dense-v2.json) 和 [Hashing Hybrid](benchmarks/retrieval-public-ops-hashing-hybrid-v2.json)。报告默认删除结果正文；只有显式传入 `--include-result-text` 才会写入片段。

只运行核心策略可使用 `--strategies single_agent multi_agent adaptive_harness`。每个 case/strategy/repetition 使用独立数据库，防止故障计数、熔断状态和幂等数据串扰。

也可以加载一个外部 suite 文件或目录；新增 case 不需要修改策略代码：

```bash
PYTHONPATH=src python -m enterprise_agent benchmark \
  --suite src/enterprise_agent/suites/regression \
  --strategies single_agent adaptive_harness \
  --output work/regression-report.json
```

JSON suite 使用一个带 `tasks` 数组的对象；JSONL suite 使用一条 `record_type: suite` header，后接若干 `record_type: task` 记录。默认 benchmark 加载 development、regression、security 与 recovery 共 18 条，不自动加载 2 条 held-out，避免保留集意外进入日常策略调试。

用同一模型合约比较核心三种架构（会产生真实 API 费用）：

```bash
PYTHONPATH=src python -m enterprise_agent benchmark \
  --llm-provider deepseek \
  --model deepseek-v4-flash \
  --thinking disabled \
  --strategies single_agent multi_agent adaptive_harness \
  --repetitions 3 \
  --max-model-calls 27 \
  --max-total-tokens 25000 \
  --max-estimated-cost-usd 0.02 \
  --workspace work/deepseek-benchmark-runs \
  --output work/deepseek-benchmark-v2.json
```

Benchmark 不传递审批 token，也不执行 `create_ticket`；预算在完整 case 边界检查。报告只保存非敏感配置指纹，不保存 API key、完整 prompt 或原始响应。

执行期间会实时输出当前 repetition、strategy、case、累计模型调用、token、成本和耗时。按 `Ctrl+C` 会保存已经完成的 runs 到指定报告，标记 `stop_reason: user_interrupt`，并以状态码 130 退出。模型预算耗尽后，系统会跳过后续需要模型的正样本，但仍完成健康或证据不足等零模型 case。

默认使用 `counterbalanced` 顺序，让策略轮换先后位置，降低模型服务时间漂移带来的顺序偏差；可用 `--execution-order fixed` 重现旧顺序。报告把底层 Runtime 状态统一映射为 `completed`、`safely_refused`、`failed`，同时保留原始状态用于审计。

运行测试：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## 状态机

```text
CREATED
  -> RUNNING_SPECIALISTS
  -> VERIFYING
  -> SYNTHESIZING
       -> SAFELY_REFUSED
       -> COMPLETED
       -> WAITING_APPROVAL
          -> EXECUTING_ACTION
          -> COMPLETED

任意非终态 -> FAILED / CANCELLED
```

`SAFELY_REFUSED`、`COMPLETED`、`FAILED` 和 `CANCELLED` 都是终态。每个阶段完成后先写 checkpoint，再进入下一阶段，因此服务重启后，已经完成并持久化的 Specialist 不会重复执行。

## 目录

```text
enterprise-agent/
├── src/enterprise_agent/
│   ├── harness.py         # 分阶段执行循环、resume 和 failpoint
│   ├── state_machine.py   # 合法状态转换
│   ├── store.py           # SQLite Snapshot、Event Log、乐观锁
│   ├── tracing.py         # Span 生命周期、工具调用转换和父子链路
│   ├── tool_gateway.py    # Schema、策略、重试、限流、熔断和调用记录
│   ├── faults.py          # 确定性工具故障注入
│   ├── routing.py         # 共享风险路由策略与 fail-closed 决策合同
│   ├── memory.py          # Working Memory、原子 Episode 投影、TTL 与删除
│   ├── retrieval.py       # 语料合同、切分、Dense baseline 与检索评测
│   ├── corpora/           # 版本化知识语料
│   ├── retrieval_suites/  # 独立检索评测集
│   ├── benchmark.py       # 数据集、五种策略、配对评分与报告
 │   ├── evals.py           # Task/Trial/Outcome/Grader/Suite 合同与可靠性指标
 │   ├── suite_loader.py    # JSON/JSONL suite 读取与版本化 Schema 校验
 │   ├── graders.py         # outcome/evidence/safety/recovery 独立 grader
 │   ├── suites/            # development/regression/security/recovery/held-out 数据
 │   ├── llm.py             # DeepSeek/OpenAI-compatible JSON Provider 和调用记录
│   ├── tools.py           # 企业工具实现与策略注册
│   ├── context.py         # 上下文预算和压缩视图
│   ├── agents.py          # Specialist、Verifier、Synthesis
│   └── models.py          # Task、State、Evidence、Event 合同
├── tests/
├── benchmarks/            # 已执行的版本化基准报告
├── docs/
├── architecture.md
└── evaluation.md
```

## 文档

- [`architecture.md`](architecture.md)：整体模块边界与数据流
- [`docs/durable-runtime.md`](docs/durable-runtime.md)：checkpoint、恢复、审批和幂等语义
- [`docs/tool-gateway.md`](docs/tool-gateway.md)：可靠工具调用与错误处理语义
- [`docs/observability.md`](docs/observability.md)：持久化 Trace、Span 层级与延迟指标
- [`docs/incident-response.md`](docs/incident-response.md)：运维证据链、故障注入与安全处置
- [`docs/adaptive-routing.md`](docs/adaptive-routing.md)：Shadow/Active 路由、恢复语义与上线门槛
- [`docs/benchmarking.md`](docs/benchmarking.md)：策略定义、指标契约与结果解释
- [`docs/model-provider.md`](docs/model-provider.md)：DeepSeek 配置、结构化契约和失败语义
- [`docs/rag-memory.md`](docs/rag-memory.md)：RAG、Working Memory、Episodic Memory 的范围、合同与实施顺序
- [`docs/public-corpus.md`](docs/public-corpus.md)：公开语料来源、可复现构建、许可与评测限制
- [`evaluation.md`](evaluation.md)：数据集、指标与实验记录

## 开发原则

- 先建立可运行、可恢复的 baseline，再增加复杂模块。
- 所有优化同时报告质量、延迟、成本和故障恢复指标。
- Multi-Agent 必须与单 Agent/Workflow baseline 做实验比较。
- RAG 必须比较 Dense、BM25、Hybrid 与 Rerank baseline，并保留语料和索引版本。
- Memory 只保存有来源、可隔离、可过期和可删除的信息；历史记忆不能覆盖本次实时证据。
- 工具默认最小权限；只读和写入工具分级管理。
- 评测结果和简历数字必须由可复现实验产生。

## 下一阶段

1. 对首批公开语料的 40 条正例做独立人工复核，并增加同域语义 hard negative 与 held-out 集。
2. 在冻结的公开语料上增加 Reranker、引用忠实度和多相关文档 pooled judgment。
3. 将 Memory 合同扩展为独立多轮 suite，增加版本替换、更多服务和人工审核的冲突样本。
4. 人工复核新增 15 条 synthetic draft，并将 Agent、检索和 Memory 评测集分别扩展，避免用同一数据同时调策略和宣称效果。
5. 数据合同稳定后增加 PostgreSQL、Worker lease、OpenTelemetry、离线 replay 和真实连接器。

完整升级边界、里程碑与停止条件见 [`PROJECT_PLAN.md`](PROJECT_PLAN.md)。

Agentic RL 已拆分为独立的 `coding-agent-rl-lab` 项目。本仓库只保留可选的 trajectory/policy adapter，不包含 Coding Environment 或训练循环。
