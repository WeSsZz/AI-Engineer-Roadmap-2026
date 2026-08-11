# RAG & Memory Design

## 目标

RAG 为事故诊断提供可回溯的企业知识证据；Memory 为当前任务和后续相似事故保存受控上下文。两者都是 Agent Runtime 的输入层，不能绕过 Tool Gateway、Verifier、租户权限、Trace 或评测合同。

## 当前状态

- 已有 `Context Engine`：压缩当前任务的事实、证据和最近事件。
- 已有版本化 JSON 语料、严格 Schema、确定性切分和稳定 chunk ID。
- 已有 Hashing Dense、BM25、RRF Hybrid 三组 baseline、30 条 retrieval smoke set 和独立评测 CLI。
- 已有可选 BGE-M3 Dense Provider：固定 Hugging Face revision、批量编码、SQLite 语料向量缓存；查询不走磁盘缓存，默认路径不导入模型依赖。
- 已有 `search_knowledge` Tool Gateway 工具；`get_runbook` 是其兼容适配器。
- Hashing baseline 不是学习型语义 Embedding；BGE-M3 当前是评测用内存精确扫描，不是生产向量索引。尚无公开语料解析、Reranker 或跨任务 Episodic Memory。

Runtime 暂时选择 BM25。该选择只对当前版本数据成立；BGE-M3 的结果也必须在公开许可语料、人工审核 query 和 held-out set 上复验，不能仅凭当前合成 smoke set 切换线上检索器。

当前 negative case 只验证租户和元数据 scope，不覆盖同一合法 scope 内的语义不可回答 query。因此 `no_answer_accuracy` 目前是隔离合同指标，不是完整拒答能力指标；下一版检索集必须补充同租户 hard negative 并单独校准拒答阈值。

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

每一步都使用同一版本检索集报告 Recall@5/10、MRR、NDCG、P50/P95 延迟和索引成本。模型加载、建索引和查询延迟分别报告；模型 ID、精确 revision、精度、max length、Embedding 指纹与缓存命中也写入报告。没有实验数据时不宣称 Hybrid 或 Reranker 更优。

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
- 支持 TTL、显式删除和版本替换；
- 查询必须携带 tenant scope；
- 与实时 Metrics/Logs 冲突时，实时证据优先并记录冲突事件。

## 安全与评测

- 文档和历史记忆都按不可信输入处理，防止 Prompt Injection。
- 检索工具遵循 per-agent allowlist 和 tenant identity。
- Trace 记录语料、索引、Embedding、Reranker 和 Memory 版本，但不记录密钥或完整敏感正文。
- RAG 评测与 Agent 任务评测分离，避免用最终答案分数掩盖检索缺陷。
- Memory 测试覆盖正确召回、错误召回、过期、冲突、删除和跨租户访问。

## 停止条件

第一版只实现一个语料源、一个向量索引和一个 Episodic Memory Store。达到可复现评测、引用回溯、租户隔离与删除合同后停止扩展，不增加 GraphRAG、多向量数据库或复杂用户画像。
