# DeepSeek V4 Flash Model Provider

## 当前接入

项目使用 DeepSeek 官方 OpenAI-compatible `POST /chat/completions` 接口，默认模型 ID 为 `deepseek-v4-flash`，base URL 为 `https://api.deepseek.com`。官方文档列出的当前模型 ID 是 `deepseek-v4-flash` 和 `deepseek-v4-pro`；旧的 `deepseek-chat` / `deepseek-reasoner` 已进入停用节点。

- [DeepSeek Models API](https://api-docs.deepseek.com/api/list-models)
- [Chat Completion API](https://api-docs.deepseek.com/api/create-chat-completion)
- [JSON Output Guide](https://api-docs.deepseek.com/guides/json_mode/)
- [Models and Pricing](https://api-docs.deepseek.com/quick_start/pricing)

适配器使用 Python 标准库 HTTP client，不增加 SDK 依赖。配置项见 [`.env.example`](../.env.example)，程序不会自动加载 `.env`；生产环境应使用 shell、容器 Secret 或企业密钥管理器注入。

## 使用

```bash
export DEEPSEEK_API_KEY="your-key"

PYTHONPATH=src python -m enterprise_agent model-smoke

PYTHONPATH=src python -m enterprise_agent \
  --db work/deepseek-agent.db run \
  --llm-provider deepseek \
  --model deepseek-v4-flash \
  --thinking disabled
```

默认关闭 thinking，以便第一次集成时控制延迟和输出行为；可通过 `--thinking enabled` 开启。无 `--llm-provider deepseek` 时继续使用确定性综合，现有测试和 benchmark 不调用外网。

## 结构化输出契约

Provider 设置 `response_format={"type":"json_object"}`，并按照官方要求在 prompt 中显式要求 JSON。模型必须返回：

```json
{
  "root_cause": "database_pool_exhaustion",
  "confidence": "high",
  "answer": "...",
  "recommendation": "...",
  "evidence_refs": ["metrics-...", "logs-...", "runbook-..."]
}
```

本地 policy 会再次验证：根因和置信度符合已交叉验证的事实；引用只能来自 evidence allowlist；Metrics、Logs、Runbook 三种证据必须同时被引用；文本必须非空且有长度上限。模型不返回 action token，是否准备工单仍由 Harness 根据任务目标、Verifier 状态和本地 policy 决定。

## 失败和重试

以下错误允许指数退避重试：网络失败、HTTP 408/429/5xx、空响应和 `insufficient_system_resource`。鉴权/其他 4xx、JSON 截断、JSON 解析失败和契约违规不会盲目重试。

每个 attempt 持久化为 Model Span，包含模型、状态、耗时、token、cache hit/miss 和错误类型。API key、完整 prompt、原始响应以及 reasoning content 不进入 Span 或 Event Log。Telemetry sink 失败不会改变模型业务结果。

## 当前验证边界

Provider、重试、结构化解析、安全拒绝、证据防幻觉和 Model Trace 已通过 mock transport 测试。当前开发环境没有 `DEEPSEEK_API_KEY`，因此尚未声称完成真实线上调用；配置密钥后应先运行 `model-smoke`，再执行一条不带 `--approve` 的事故诊断任务。
