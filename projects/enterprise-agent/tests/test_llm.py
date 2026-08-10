from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enterprise_agent.faults import FaultInjector, FaultRule
from enterprise_agent.harness import EnterpriseAgentHarness
from enterprise_agent.llm import (
    DeepSeekProvider,
    LLMConfig,
    LLMConfigurationError,
    LLMResponseError,
    LLMTransientError,
)
from enterprise_agent.models import SpanKind, SpanStatus, TaskStatus
from enterprise_agent.store import SQLiteEventStore


OBJECTIVE = "诊断 payment-api 错误率和延迟突增的根因，并生成故障工单"


def response_payload(content: dict[str, object], *, finish_reason: str = "stop") -> dict[str, object]:
    return {
        "model": "deepseek-v4-flash",
        "choices": [{"finish_reason": finish_reason, "message": {"content": json.dumps(content)}}],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 80,
            "total_tokens": 200,
            "prompt_cache_hit_tokens": 100,
            "prompt_cache_miss_tokens": 20,
        },
    }


def valid_synthesis() -> dict[str, object]:
    return {
        "root_cause": "database_pool_exhaustion",
        "confidence": "high",
        "answer": "模型综合结论：payment-api 数据库连接池耗尽。",
        "recommendation": "依据 Runbook 检查慢查询，并在审批后扩容或回滚。",
        "evidence_refs": [
            "metrics-payment-api-15m",
            "logs-payment-api-db_pool_timeout",
            "runbook-payment-db-pool-v3",
        ],
    }


class DeepSeekProviderTests(unittest.TestCase):
    def test_configuration_requires_key_and_hides_it_from_repr(self) -> None:
        with self.assertRaises(LLMConfigurationError):
            LLMConfig(api_key="")
        config = LLMConfig(api_key="secret-value")
        self.assertNotIn("secret-value", repr(config))

    def test_env_configuration_uses_v4_flash_defaults(self) -> None:
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            config = LLMConfig.from_env()
        self.assertEqual(config.model, "deepseek-v4-flash")
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertEqual(config.thinking, "disabled")

    def test_json_request_and_usage_are_parsed(self) -> None:
        captured: dict[str, object] = {}

        def transport(url, headers, payload, timeout):
            captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
            return response_payload({"status": "ok"})

        provider = DeepSeekProvider(LLMConfig(api_key="test-key"), transport=transport)
        response = provider.complete_json(
            system_prompt='Return JSON with schema {"status":"ok"}.',
            user_prompt="Reply in JSON.",
            task_id="task-1",
        )
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        request = captured["payload"]
        self.assertEqual(request["model"], "deepseek-v4-flash")
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["thinking"], {"type": "disabled"})
        self.assertEqual(response.data, {"status": "ok"})
        self.assertEqual(response.usage.total_tokens, 200)
        self.assertEqual(response.usage.prompt_cache_hit_tokens, 100)

    def test_transient_failure_retries_and_records_each_attempt(self) -> None:
        calls = 0
        records = []

        def transport(url, headers, payload, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise LLMTransientError("busy")
            return response_payload({"status": "ok"})

        provider = DeepSeekProvider(
            LLMConfig(api_key="test-key", max_retries=1, retry_backoff_seconds=0),
            transport=transport,
            sleeper=lambda _: None,
            record_sink=records.append,
        )
        response = provider.complete_json(system_prompt="Return JSON.", user_prompt="JSON result.")
        self.assertEqual(response.attempts, 2)
        self.assertEqual([record.status for record in records], ["failed", "completed"])
        self.assertEqual(provider.invocations, tuple(records))
        self.assertEqual(records[0].error_type, "LLMTransientError")

    def test_malformed_or_truncated_json_fails_closed(self) -> None:
        malformed = DeepSeekProvider(
            LLMConfig(api_key="test-key"),
            transport=lambda *args: {
                "choices": [{"finish_reason": "stop", "message": {"content": "not-json"}}]
            },
        )
        with self.assertRaises(LLMResponseError):
            malformed.complete_json(system_prompt="Return JSON.", user_prompt="JSON result.")
        truncated = DeepSeekProvider(
            LLMConfig(api_key="test-key"),
            transport=lambda *args: response_payload({"status": "ok"}, finish_reason="length"),
        )
        with self.assertRaises(LLMResponseError):
            truncated.complete_json(system_prompt="Return JSON.", user_prompt="JSON result.")


class ModelSynthesisTests(unittest.TestCase):
    def test_verified_evidence_is_synthesized_by_model_and_traced(self) -> None:
        provider = DeepSeekProvider(
            LLMConfig(api_key="test-key"),
            transport=lambda *args: response_payload(valid_synthesis()),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "agent.db")
            with EnterpriseAgentHarness(store=store, llm_provider=provider) as harness:
                result = harness.run(OBJECTIVE, scenario="incident")

            self.assertEqual(result.state.status, TaskStatus.WAITING_APPROVAL)
            self.assertTrue(result.answer.startswith("模型综合结论"))
            self.assertEqual(result.state.artifacts["model_call"]["model"], "deepseek-v4-flash")
            self.assertEqual(result.state.artifacts["model_call"]["usage"]["total_tokens"], 200)
            spans = store.list_spans(result.task.task_id)
            model_span = next(span for span in spans if span.kind == SpanKind.MODEL)
            synthesis = next(span for span in spans if span.name == "agent.incident_synthesis_agent")
            self.assertEqual(model_span.parent_span_id, synthesis.span_id)
            self.assertEqual(model_span.status, SpanStatus.COMPLETED)
            self.assertEqual(model_span.attributes["prompt_cache_hit_tokens"], 100)

    def test_hallucinated_evidence_blocks_action(self) -> None:
        invalid = valid_synthesis()
        invalid["evidence_refs"] = ["invented-evidence"]
        provider = DeepSeekProvider(
            LLMConfig(api_key="test-key"),
            transport=lambda *args: response_payload(invalid),
        )
        with tempfile.TemporaryDirectory() as directory:
            with EnterpriseAgentHarness(
                store=SQLiteEventStore(Path(directory) / "agent.db"),
                llm_provider=provider,
            ) as harness:
                result = harness.run(OBJECTIVE, scenario="incident")
        self.assertEqual(result.state.status, TaskStatus.FAILED)
        self.assertNotIn("pending_action", result.state.artifacts)
        self.assertIn("outside the verified evidence set", result.state.artifacts["model_error"])
        self.assertEqual(result.state.artifacts["outcome_class"], "model_contract_failure")

    def test_failed_verifier_prevents_model_call(self) -> None:
        calls = 0

        def transport(*args):
            nonlocal calls
            calls += 1
            return response_payload(valid_synthesis())

        provider = DeepSeekProvider(LLMConfig(api_key="test-key"), transport=transport)
        injector = FaultInjector((FaultRule("search_logs", "permanent"),))
        with tempfile.TemporaryDirectory() as directory:
            with EnterpriseAgentHarness(
                store=SQLiteEventStore(Path(directory) / "agent.db"),
                fault_injector=injector,
                llm_provider=provider,
            ) as harness:
                result = harness.run(OBJECTIVE, scenario="incident")
        self.assertEqual(result.state.status, TaskStatus.FAILED)
        self.assertEqual(result.state.artifacts["outcome_class"], "execution_failure")
        self.assertEqual(calls, 0)
        self.assertIsNone(result.state.artifacts["model_call"])


if __name__ == "__main__":
    unittest.main()
