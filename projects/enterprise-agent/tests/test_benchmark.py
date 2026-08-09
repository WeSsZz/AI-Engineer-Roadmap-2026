from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from enterprise_agent.benchmark import (
    BenchmarkCase,
    BenchmarkRunner,
    StrategyOutcome,
    default_cases,
    write_report,
)
from enterprise_agent.store import SQLiteEventStore


class BenchmarkTests(unittest.TestCase):
    def test_all_strategies_run_on_isolated_versioned_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner().run(workspace=directory)
        self.assertEqual(report["dataset_version"], "incident-diagnosis-v1")
        self.assertEqual(report["case_count"], 5)
        self.assertEqual(len(report["runs"]), 20)
        for aggregate in report["aggregates"].values():
            self.assertEqual(aggregate["run_count"], 5)
            self.assertEqual(aggregate["task_success_rate"], 1.0)
            self.assertEqual(aggregate["retry_recovery_rate"], 1.0)

    def test_adaptive_strategies_avoid_unneeded_healthy_service_tools(self) -> None:
        healthy = next(case for case in default_cases() if case.case_id == "order-api-healthy")
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner(
                cases=(healthy,),
                strategy_names=("fixed_workflow", "single_agent", "plan_executor", "multi_agent"),
            ).run(workspace=directory)
        rows = {row["strategy"]: row for row in report["runs"]}
        self.assertEqual(rows["single_agent"]["tool_attempts"], 1)
        self.assertEqual(rows["plan_executor"]["tool_attempts"], 1)
        self.assertEqual(rows["fixed_workflow"]["tool_attempts"], 3)
        self.assertEqual(rows["multi_agent"]["tool_attempts"], 3)

    def test_plan_executor_persists_planner_executor_and_tool_hierarchy(self) -> None:
        case = default_cases()[0]
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner(cases=(case,), strategy_names=("plan_executor",)).run(workspace=directory)
            run = report["runs"][0]
            database = Path(directory) / f"r0-{case.case_id}-plan_executor" / "trace.db"
            spans = SQLiteEventStore(database).list_spans(run["task_id"])
        by_name = {span.name: span for span in spans}
        root = by_name["strategy.plan_executor"]
        planner = by_name["agent.planner"]
        executor = by_name["agent.executor"]
        self.assertEqual(planner.parent_span_id, root.span_id)
        self.assertEqual(executor.parent_span_id, root.span_id)
        self.assertEqual(by_name["tool.query_service_metrics"].parent_span_id, executor.span_id)

    def test_scoring_requires_safe_refusal_for_negative_cases(self) -> None:
        case = BenchmarkCase(
            "negative",
            "healthy service",
            "order-api",
            None,
            ("service_metrics",),
            ("query_service_metrics",),
        )
        unsafe = StrategyOutcome(
            "test",
            case.case_id,
            "task-1",
            "completed",
            None,
            "insufficient",
            safely_refused=False,
        )
        score = BenchmarkRunner._score(unsafe, case)
        self.assertFalse(score["success"])
        self.assertTrue(score["root_cause_correct"])

    def test_report_is_json_serializable_and_written_with_schema(self) -> None:
        case = default_cases()[0]
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner(cases=(case,), strategy_names=("single_agent",)).run()
            target = Path(directory) / "reports" / "benchmark.json"
            write_report(report, target)
            loaded = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(loaded["schema_version"], 1)
        self.assertEqual(loaded["strategies"], ["single_agent"])
        self.assertEqual(len(loaded["runs"]), 1)

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BenchmarkRunner(strategy_names=("unknown",))
        with self.assertRaises(ValueError):
            BenchmarkRunner(strategy_names=("single_agent",)).run(repetitions=0)


if __name__ == "__main__":
    unittest.main()
