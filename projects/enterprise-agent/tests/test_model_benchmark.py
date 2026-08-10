from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enterprise_agent.benchmark import (
    BenchmarkBudget,
    BenchmarkRunner,
    DEEPSEEK_PRICING,
    ModelBenchmarkConfig,
    STRATEGIES,
    default_cases,
)
from enterprise_agent.llm import DeepSeekProvider, LLMConfig
from enterprise_agent.models import SpanKind
from enterprise_agent.store import SQLiteEventStore


def _response(content: dict[str, object]) -> dict[str, object]:
    return {
        "model": "deepseek-v4-flash",
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 80,
            "total_tokens": 200,
            "prompt_cache_hit_tokens": 100,
            "prompt_cache_miss_tokens": 20,
        },
    }


def _valid_synthesis() -> dict[str, object]:
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


def _model_config(content: dict[str, object] | None = None) -> ModelBenchmarkConfig:
    payload = content or _valid_synthesis()

    def factory() -> DeepSeekProvider:
        return DeepSeekProvider(
            LLMConfig(api_key="test-key"),
            transport=lambda *args: _response(payload),
        )

    return ModelBenchmarkConfig(
        provider_factory=factory,
        provider="deepseek",
        model="deepseek-v4-flash",
        thinking="disabled",
        max_tokens=1200,
        pricing=DEEPSEEK_PRICING["deepseek-v4-flash"],
    )


class ModelBenchmarkTests(unittest.TestCase):
    def test_all_strategies_use_real_synthesis_contract_and_report_usage(self) -> None:
        case = default_cases()[0]
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner(cases=(case,), model_config=_model_config()).run(
                workspace=directory
            )
            direct_run = next(row for row in report["runs"] if row["strategy"] == "single_agent")
            trace_db = Path(directory) / f"r0-{case.case_id}-single_agent" / "trace.db"
            spans = SQLiteEventStore(trace_db).list_spans(direct_run["task_id"])

        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["execution_mode"], "model_backed")
        self.assertEqual(report["completed_run_count"], 5)
        self.assertNotIn("test-key", json.dumps(report))
        self.assertNotIn("You are the evidence-bound", json.dumps(report))
        self.assertEqual(len(report["model"]["prompt_fingerprint_sha256"]), 64)
        self.assertEqual(len(report["model"]["config_fingerprint_sha256"]), 64)
        self.assertTrue(all(row["success"] for row in report["runs"]))
        self.assertTrue(all(row["model_calls"] == 1 for row in report["runs"]))
        self.assertTrue(all(row["model_schema_compliant"] for row in report["runs"]))
        self.assertTrue(any(span.kind == SpanKind.MODEL for span in spans))

        aggregate = report["aggregates"]["single_agent"]
        self.assertEqual(aggregate["total_tokens"], 200)
        self.assertEqual(aggregate["prompt_cache_hit_rate"], 0.8333)
        self.assertAlmostEqual(aggregate["estimated_cost_usd"], 0.00002548, places=9)
        self.assertEqual(aggregate["model_schema_compliance_rate"], 1.0)

    def test_invalid_model_evidence_fails_closed(self) -> None:
        invalid = _valid_synthesis()
        invalid["evidence_refs"] = ["invented-evidence"]
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner(
                cases=(default_cases()[0],),
                strategy_names=("single_agent",),
                model_config=_model_config(invalid),
            ).run(workspace=directory)
        run = report["runs"][0]
        self.assertFalse(run["success"])
        self.assertTrue(run["safely_refused"])
        self.assertFalse(run["model_schema_compliant"])
        self.assertIn("outside the verified evidence set", run["error"])

    def test_negative_case_skips_model_call(self) -> None:
        healthy = next(case for case in default_cases() if case.case_id == "order-api-healthy")
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner(cases=(healthy,), model_config=_model_config()).run(
                workspace=directory
            )
        self.assertTrue(all(row["model_calls"] == 0 for row in report["runs"]))
        self.assertTrue(all(row["model_schema_compliant"] is None for row in report["runs"]))

    def test_call_budget_stops_at_a_complete_case_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner(
                cases=(default_cases()[0],),
                strategy_names=("fixed_workflow", "single_agent", "plan_executor", "multi_agent"),
                model_config=_model_config(),
                budget=BenchmarkBudget(
                    max_model_calls=4,
                    max_total_tokens=10_000,
                    max_estimated_cost_usd=1.0,
                ),
            ).run(repetitions=2, workspace=directory)
        self.assertEqual(report["planned_run_count"], 8)
        self.assertEqual(report["completed_run_count"], 4)
        self.assertTrue(report["budget"]["stopped_early"])
        self.assertEqual(report["budget"]["stop_reason"], "max_model_calls")

        with tempfile.TemporaryDirectory() as directory:
            complete = BenchmarkRunner(
                cases=(default_cases()[0],),
                strategy_names=("fixed_workflow", "single_agent", "plan_executor", "multi_agent"),
                model_config=_model_config(),
                budget=BenchmarkBudget(
                    max_model_calls=4,
                    max_total_tokens=10_000,
                    max_estimated_cost_usd=1.0,
                ),
            ).run(workspace=directory)
        self.assertEqual(complete["completed_run_count"], complete["planned_run_count"])
        self.assertFalse(complete["budget"]["stopped_early"])
        self.assertIsNone(complete["budget"]["stop_reason"])

    def test_zero_model_cases_continue_after_model_budget_is_exhausted(self) -> None:
        positive = default_cases()[0]
        healthy = next(case for case in default_cases() if case.case_id == "order-api-healthy")
        events = []
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner(
                cases=(positive, healthy),
                strategy_names=("single_agent",),
                model_config=_model_config(),
                budget=BenchmarkBudget(
                    max_model_calls=1,
                    max_total_tokens=10_000,
                    max_estimated_cost_usd=1.0,
                ),
            ).run(repetitions=2, workspace=directory, progress_callback=events.append)
        self.assertEqual(report["planned_run_count"], 4)
        self.assertEqual(report["completed_run_count"], 3)
        self.assertEqual(sum(row["case_id"] == healthy.case_id for row in report["runs"]), 2)
        self.assertEqual(report["budget"]["stop_reason"], "max_model_calls")
        skipped = [event for event in events if event.event == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].case_id, positive.case_id)

    def test_progress_reports_started_and_completed_totals(self) -> None:
        events = []
        with tempfile.TemporaryDirectory() as directory:
            BenchmarkRunner(
                cases=(default_cases()[0],),
                strategy_names=("single_agent",),
                model_config=_model_config(),
            ).run(workspace=directory, progress_callback=events.append)
        self.assertEqual([event.event for event in events], ["started", "completed"])
        self.assertEqual(events[0].run_index, 1)
        self.assertEqual(events[1].completed_run_count, 1)
        self.assertEqual(events[1].model_calls, 1)
        self.assertEqual(events[1].total_tokens, 200)

    def test_keyboard_interrupt_returns_a_partial_report(self) -> None:
        class InterruptingStrategy:
            def __init__(self, model_config=None) -> None:
                pass

            def run(self, case, workspace):
                raise KeyboardInterrupt

        events = []
        with patch.dict(STRATEGIES, {"interrupting": InterruptingStrategy}):
            with tempfile.TemporaryDirectory() as directory:
                report = BenchmarkRunner(
                    cases=(default_cases()[0],),
                    strategy_names=("interrupting",),
                ).run(workspace=directory, progress_callback=events.append)
        self.assertEqual(report["completed_run_count"], 0)
        self.assertTrue(report["budget"]["stopped_early"])
        self.assertEqual(report["budget"]["stop_reason"], "user_interrupt")
        self.assertEqual(events[-1].event, "interrupted")

    def test_budget_values_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            BenchmarkBudget(max_model_calls=0)


if __name__ == "__main__":
    unittest.main()
