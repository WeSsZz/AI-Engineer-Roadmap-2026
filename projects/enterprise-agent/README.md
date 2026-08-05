# Enterprise Knowledge Agent

## 项目目标

构建一个面向企业知识库和业务查询的 Agent，重点展示可评测的检索优化、可靠工具调用和工程落地能力。

## MVP 场景

用户可以针对公开企业/技术文档提问；Agent 根据问题检索知识库，必要时调用只读结构化查询工具，并返回带来源的答案。

## 计划能力

- 文档导入、解析、切分与索引
- Dense Retrieval、BM25、Hybrid Search 与 Rerank
- 可追踪的 Tool Calling 与参数校验
- Planner/Executor 和短期会话记忆
- Retrieval、Answer、Agent、Latency/Cost 四类评测
- FastAPI AI 服务；按需要接入 Spring Boot 业务层

## 开发原则

- 先建立简单 baseline，再增加复杂模块。
- 所有优化必须同时报告质量、延迟和成本。
- Multi-Agent 只有在单 Agent/Workflow baseline 明确不足时才加入。
- 工具默认最小权限；演示 SQL 工具只允许白名单只读查询。

## 文档

- [`architecture.md`](architecture.md)：模块边界与关键数据流
- [`evaluation.md`](evaluation.md)：数据集、指标与实验结果

## 预计目录

```text
enterprise-agent/
├── apps/              # API/演示应用
├── agent/             # loop、planner、memory、tools
├── ingestion/         # 文档处理与索引
├── retrieval/         # sparse、dense、fusion、rerank
├── evaluation/        # 数据集与评测脚本
├── tests/
└── docs/
```

代码加入后，应在这里补充环境要求、配置样例、启动命令、演示和已知限制。
