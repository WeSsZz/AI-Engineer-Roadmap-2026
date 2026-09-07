# RAG & Memory Design

## 目标

RAG 为事故诊断提供可回溯的企业知识证据；Memory 为当前任务和后续相似事故保存受控上下文。两者都是 Agent Runtime 的输入层，不能绕过 Tool Gateway、Verifier、租户权限、Trace 或评测合同。

## 当前状态

- 已有结构化 Working Memory：保存当前任务的事实、证据、计划、未决项和冻结的历史候选 ID，模型视图不包含 Runtime artifacts。
- 已有版本化合成语料，以及从固定 Git 对象离线重建的 20 篇公开许可 Markdown 文档；严格 Schema、内容哈希、确定性切分和稳定 chunk ID 均已落地。
- 已有 Hashing Dense、BM25、RRF Hybrid 三组 baseline、30 条 retrieval smoke set 和独立评测 CLI。
- 已有可选 Sentence Transformers Provider；只接受不可变 revision 与本地快照，逐文件校验 Hugging Face tree 元数据并在报告中记录模型 artifact SHA-256、文件数、维度、设备、索引版本和构建耗时。
- 已有 44 条公开语料 draft case、语料/suite SHA-256 强绑定、文档级指标和默认脱敏报告。
- 已有 `search_knowledge` Tool Gateway 工具；`get_runbook` 是其兼容适配器。
- 已有 SQLite Episodic Memory v1：只写已完成且验证通过的高置信事故，支持租户隔离、provenance、有效期、软删除和原子终态提交。
- 尚无 PDF/站点模板语义解析、持久化向量索引、Reranker、语义 Memory Search 或 Episode 版本替换。

### 当前检索对比

| 方法 | Recall@5 | MRR | NDCG@5 | Serving P95 |
| --- | ---: | ---: | ---: | ---: |
| Hashing Dense | 0.8000 | 0.6050 | 0.6541 | 10.484 ms |
| BM25 | 0.9750 | 0.8854 | 0.9081 | 3.176 ms |
| Hashing Hybrid RRF | 0.9250 | 0.7654 | 0.8053 | 14.546 ms |
| BGE-M3 Dense | 1.0000 | 0.9875 | 0.9908 | 36.481 ms |
| BGE-M3 Hybrid RRF | 1.0000 | 0.9750 | 0.9815 | 39.359 ms |

BGE-M3 使用 revision `5617a9f61b028005a4858fdac845db406aefb181`、artifact SHA-256 `8676d69f6b957059fa23267d55e5dd4d01fd07af1a62fb29b122ac34994ed18e`、1024 维归一化向量和 CUDA；本机为 Windows + NVIDIA RTX 5070 Ti Laptop GPU。完整报告位于 `benchmarks/retrieval-public-ops-*-v2.json`。

这些数字来自 Kubernetes/Prometheus 的 20 篇官方文档、40 条来源导出正例和 4 条范围隔离负例，只运行一次且延迟依赖本机环境。全部方法的范围隔离准确率均为 1.0，但当前没有同 tenant、同 product 的语义 hard negative，所以 `semantic_no_answer_accuracy` 为 `null`。问题尚未独立出题或盲审，也没有 pooled judgment/held-out 集；只能说明 BGE-M3 Dense 在这套 draft smoke set 上排名最高，不能外推为生产质量结论。

## RAG 合同

### Document

每份文档至少保存：

- `document_id`、`title`、`source_uri`、`source_type`；
- `tenant_id`、服务名和访问范围；
- `document_version`、`effective_at`、`expires_at`；
- 许可、敏感性和摄取时间。

### Chunk

每个 chunk 保留 `chunk_id`、父文档、章节路径、原文范围和稳定内容哈希。重新切分或更新文档时产生新的索引版本，不能静默覆盖评测所依赖的版本。

### Retrieval Result

检索结果包含 query、chunk、Dense/BM25/融合/重排分数、rank、索引版本和耗时。进入 Agent 前转换为现有 `Evidence`，Verifier 只允许引用实际进入本次上下文的 evidence ID。

## 检索实现顺序

1. 文档解析、元数据与确定性切分。
2. Dense Top-K baseline。
3. BM25 baseline。
4. RRF 融合。
5. Cross-Encoder Reranker。
6. 引用生成、忠实度和空结果安全拒答。

每一步都使用同一版本检索集报告 Recall@5/10、MRR、NDCG、P50/P95 延迟和索引成本。没有实验数据时不宣称 Hybrid 或 Reranker 更优。

## Memory 分层

### Working Memory

只服务当前 task，包含：

- 已确认事实与 evidence ID；
- 当前计划与未决问题；
- 最近工具/模型结果摘要；
- 上下文压缩前后的版本和预算。

原始 Event Log 不删除；摘要只是模型可见视图。checkpoint/resume 恢复相同的结构化事实，而不是重新让模型总结并产生漂移。

### Episodic Memory

只保存 `COMPLETED` 且通过 Verifier 的事故摘要：症状、根因、处置、证据引用、时间、租户和审核状态。它用于提示相似历史事故，不代表本次事故真实状态。

写入规则：

- `SAFELY_REFUSED`、`FAILED` 和未审批结论默认不写入；
- 写入绑定 task ID、outcome、证据和 memory version；
- v1 支持 TTL 和显式软删除；版本替换保留到独立 Memory suite 稳定后实现；
- 查询必须携带 tenant scope；
- 与实时 Metrics/Logs 冲突时，实时证据优先并记录冲突事件。

当前实现把首次召回得到的 memory ID 随 Task Snapshot 持久化。后续 Context 构建只重新校验这些候选的 tenant、有效期和 tombstone，不会在任务中途加入新记忆；被删除或过期的候选会立即停止进入模型上下文。Episode、`task_finished`、`episodic_memory_written` 和终态 Snapshot 使用同一 SQLite 事务提交。

## 安全与评测

- 文档和历史记忆都按不可信输入处理，防止 Prompt Injection。
- 检索工具遵循 per-agent allowlist 和 tenant identity。
- Trace 记录语料、索引、Embedding、Reranker 和 Memory 版本，但不记录密钥或完整敏感正文。
- RAG 评测与 Agent 任务评测分离，避免用最终答案分数掩盖检索缺陷。
- Memory 测试覆盖正确召回、错误召回、过期、冲突、删除和跨租户访问。

## 停止条件

第一版只实现一个语料源、一个向量索引和一个 Episodic Memory Store。达到可复现评测、引用回溯、租户隔离与删除合同后停止扩展，不增加 GraphRAG、多向量数据库或复杂用户画像。
