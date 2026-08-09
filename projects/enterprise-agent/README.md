# Enterprise Knowledge & Operations Agent Runtime

## 项目目标

构建一个面向企业知识库、业务查询和运维故障分析的可靠 Agent Runtime，重点展示：

- 可评测的检索优化
- Multi-Agent 编排与证据验证
- 可恢复的任务执行
- 可靠工具调用与人工审批
- 上下文管理、Trace 和故障恢复

项目重点不是“多个模型互相聊天”，而是让一次 Agent 任务具备清晰的执行状态、权限边界、证据链、checkpoint、进程重启恢复和写操作幂等语义。

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
- Fixed Workflow、Single-Agent、Plan-Executor、Multi-Agent 统一评测器
- 版本化事故数据集、隔离运行目录和可复现 JSON benchmark report

当前演示任务：

> 诊断 payment-api 错误率和延迟突增的根因，并生成故障工单。

Demo 会识别数据库连接池耗尽，给出证据绑定的 Runbook 建议，并在创建工单前停在人工审批点。

## 快速运行

```bash
PYTHONPATH=src python -m enterprise_agent --db work/enterprise_agent.db run
```

注入一次瞬时 Metrics 故障，验证重试与 Trace：

```bash
PYTHONPATH=src python -m enterprise_agent \
  --db work/fault-demo.db run \
  --inject-fault query_service_metrics:transient
```

格式为 `TOOL:MODE[:TIMES]`，mode 支持 `transient`、`permanent`、`timeout`。

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

运行四种架构的对比评测：

```bash
PYTHONPATH=src python -m enterprise_agent benchmark \
  --repetitions 5 \
  --output work/benchmark-report.json
```

只运行部分策略可使用 `--strategies single_agent multi_agent`。每个 case/strategy/repetition 使用独立数据库，防止故障计数、熔断状态和幂等数据串扰。

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
  -> WAITING_APPROVAL
  -> EXECUTING_ACTION
  -> COMPLETED

任意非终态 -> FAILED / CANCELLED
```

每个阶段完成后先写 checkpoint，再进入下一阶段。因此服务重启后，已经完成并持久化的 Specialist 不会重复执行。

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
│   ├── benchmark.py       # 数据集、四种策略、评分与报告
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
- [`docs/benchmarking.md`](docs/benchmarking.md)：策略定义、指标契约与结果解释
- [`evaluation.md`](evaluation.md)：数据集、指标与实验记录

## 开发原则

- 先建立可运行、可恢复的 baseline，再增加复杂模块。
- 所有优化同时报告质量、延迟、成本和故障恢复指标。
- Multi-Agent 必须与单 Agent/Workflow baseline 做实验比较。
- 工具默认最小权限；只读和写入工具分级管理。
- 评测结果和简历数字必须由可复现实验产生。

## 下一阶段

1. 扩充保留测试集，并接入真实模型统计 token、成本和非确定性方差。
2. Dense + BM25 + Rerank 检索和真实模型适配器。
3. Worker lease、取消传播和分布式任务抢占。
4. OpenTelemetry exporter、跨服务 trace context 和指标后端。
5. Prometheus、Loki/OpenSearch、Runbook Registry 的真实适配器。
