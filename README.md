# AI Engineer Roadmap 2026

面向 **LLM Application Engineer / Agent Engineer / AI Engineer** 实习的学习、项目和求职记录。

## 目标

在 2026 年秋季开始投递实习，用可复现的项目证明三类能力：

- LLM 与 Transformer 基础：MiniMind、SFT、LoRA、模型评测
- Agent 与 RAG：检索优化、工具调用、任务规划、记忆与评测
- 工程落地：服务拆分、异步任务、可观测性和容器化部署

## 当前主线

1. 学习 Hello-Agents，理解 ReAct、Tool Calling、Planning 与 Memory。
2. 学习 MiniMind，跑通语言模型训练、推理与微调流程。
3. 开发一个集成可评测 RAG、受控 Memory 和可靠执行 Runtime 的企业 Agent，而不是只做聊天 Demo。
4. 从第 5 周开始投递，并根据岗位反馈迭代项目与面试准备。

## 仓库导航

| 目录 | 内容 |
| --- | --- |
| [`TODO.md`](TODO.md) | 8 周执行清单与验收标准 |
| [`roadmap/`](roadmap/) | 每周复盘与进度记录 |
| [`projects/enterprise-agent/`](projects/enterprise-agent/) | 企业知识 Agent 主项目 |
| [`projects/minimind-lora/`](projects/minimind-lora/) | MiniMind 与 LoRA 实验 |
| [`notes/`](notes/) | LLM、RAG 与 Agent 知识笔记 |
| [`papers/`](papers/) | 论文阅读模板与索引 |
| [`interview/`](interview/) | 面试题、项目讲解和投递复盘 |

## 代表项目

### Enterprise Knowledge & Operations Agent

面向企业知识库、业务系统和运维故障响应的可靠 Agent，整体能力分为“知识与记忆”和“可靠执行”两条互相验证的链路：

- 文档解析、切分、Embedding、BM25 + Dense 混合召回与 Reranker
- 带来源引用的 Evidence，以及 Recall、MRR、NDCG、忠实度和延迟评测
- 结构化 Working Memory 与经过验证的历史事故 Episodic Memory
- Tool Calling、证据验证、人工审批与最小权限控制
- checkpoint、resume、幂等副作用、Trace 和故障恢复
- Fixed Workflow、Single-Agent、Multi-Agent 与 Adaptive Harness 对比评测

当前已完成可靠 Runtime、Tool Gateway、Trace 和 Agent Evaluation 基线；真实 RAG 与 Episodic Memory 是接下来的主开发阶段，不能在完成前作为已实现能力宣传。

项目文档见 [`projects/enterprise-agent/README.md`](projects/enterprise-agent/README.md)。

### MiniMind & LoRA Lab

通过 MiniMind 理解 Decoder-only Transformer，并完成一次可复现的领域 SFT/LoRA 实验。项目文档见 [`projects/minimind-lora/README.md`](projects/minimind-lora/README.md)。

## 使用方式

- 每天只推进 `TODO.md` 中当前周的 1–3 个任务。
- 每周在 `roadmap/WEEKLY_LOG.md` 记录结果、数据、失败案例和下一周计划。
- 所有效果提升必须保留 baseline、数据集、指标和实验条件。
- 每个阶段结束后更新项目 README，让仓库始终可被招聘者快速阅读。

## 完成定义

开学投递前至少完成：

- [ ] 可运行的基础 RAG
- [ ] 一份包含 baseline 的检索优化报告
- [ ] 可解释、可删除且有租户边界的 Working/Episodic Memory 最小闭环
- [ ] 至少两个可靠的 Agent 工具
- [ ] 一次 MiniMind 或开源小模型微调实验
- [ ] 3 分钟项目介绍和 30 分钟深挖材料
- [ ] 可复现的启动说明与演示截图/视频
