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
from enterprise_agent.faults import FaultRule
from enterprise_agent.store import SQLiteEventStore


class BenchmarkTests(unittest.TestCase):
    def test_all_strategies_run_on_isolated_versioned_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner().run(workspace=directory)
        self.assertEqual(report["dataset_version"], "incident-diagnosis-v1")
        self.assertEqual(report["case_count"], 5)
        self.assertEqual(report["benchmark_version"], "4.0")
        self.assertEqual(len(report["runs"]), 25)
        for aggregate in report["aggregates"].values():
            self.assertEqual(aggregate["run_count"], 5)
            self.assertEqual(aggregate["task_success_rate"], 1.0)
            self.assertEqual(aggregate["pass_at_1"], 1.0)
            self.assertIsNone(aggregate["pass_power_3"])
            self.assertEqual(aggregate["fully_reliable_case_rate"], 1.0)
            self.assertEqual(aggregate["retry_recovery_rate"], 1.0)
        adaptive = report["aggregates"]["adaptive_harness"]
        fixed_multi = report["aggregates"]["multi_agent"]
        self.assertEqual(adaptive["route_accuracy"], 1.0)
        self.assertEqual(adaptive["escalation_recall"], 1.0)
        self.assertEqual(adaptive["false_downgrade_rate"], 0.0)
        self.assertEqual(adaptive["unnecessary_escalation_rate"], 0.0)
        self.assertEqual(fixed_multi["route_accuracy"], 0.8)
        self.assertEqual(fixed_multi["unnecessary_escalation_rate"], 1.0)
        healthy_multi = next(
            row
            for row in report["runs"]
            if row["strategy"] == "multi_agent" and row["case_id"] == "order-api-healthy"
        )
        self.assertEqual(healthy_multi["status"], "safely_refused")
        self.assertEqual(healthy_multi["normalized_status"], "safely_refused")

    def test_adaptive_harness_routes_healthy_and_incident_cases_differently(self) -> None:
        positive = default_cases()[0]
        healthy = next(case for case in default_cases() if case.case_id == "order-api-healthy")
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner(
                cases=(positive, healthy),
                strategy_names=("adaptive_harness",),
            ).run(workspace=directory)
            positive_run = next(row for row in report["runs"] if row["case_id"] == positive.case_id)
            database = Path(directory) / f"r0-{positive.case_id}-adaptive_harness" / "trace.db"
            spans = SQLiteEventStore(database).list_spans(positive_run["task_id"])
        rows = {row["case_id"]: row for row in report["runs"]}
        self.assertEqual(rows[positive.case_id]["route"], "verified_multi_agent")
        self.assertEqual(rows[positive.case_id]["status"], "needs_approval")
        self.assertEqual(
            rows[positive.case_id]["routing_reason"],
            "high_risk_anomaly_with_write_intent",
        )
        self.assertEqual(rows[positive.case_id]["tool_attempts"], 3)
        self.assertEqual(rows[positive.case_id]["normalized_status"], "completed")
        self.assertEqual(rows[healthy.case_id]["route"], "single_agent_triage")
        self.assertEqual(rows[healthy.case_id]["tool_attempts"], 1)
        self.assertEqual(rows[healthy.case_id]["normalized_status"], "safely_refused")
        self.assertEqual(rows[healthy.case_id]["outcome_class"], "safe_refusal")
        by_name = {span.name: span for span in spans}
        root = by_name["strategy.adaptive_harness"]
        self.assertEqual(by_name["agent.risk_router"].parent_span_id, root.span_id)
        self.assertEqual(by_name["agent.log_investigator"].parent_span_id, root.span_id)
        self.assertEqual(by_name["agent.runbook_researcher"].parent_span_id, root.span_id)
        self.assertEqual(by_name["agent.verifier"].parent_span_id, root.span_id)

    def test_adaptive_benchmark_fails_closed_to_full_investigation(self) -> None:
        baseline = default_cases()[0]
        case = BenchmarkCase(
            "metrics-permanent",
            baseline.objective,
            baseline.service,
            baseline.expected_root_cause,
            baseline.expected_evidence_types,
            baseline.expected_tools,
            (FaultRule("query_service_metrics", "permanent"),),
            expected_route="verified_multi_agent",
        )
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner(
                cases=(case,),
                strategy_names=("adaptive_harness",),
            ).run(workspace=directory)
        run = report["runs"][0]
        self.assertEqual(run["route"], "verified_multi_agent")
        self.assertEqual(run["routing_reason"], "routing_signal_unavailable_fail_closed")
        self.assertEqual(run["tool_attempts"], 3)
        self.assertEqual(run["outcome_class"], "execution_failure")

    def test_counterbalanced_order_rotates_strategy_position(self) -> None:
        case = default_cases()[0]
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner(
                cases=(case,),
                strategy_names=("single_agent", "multi_agent"),
            ).run(repetitions=2, workspace=directory)
        positions = {
            (row["repetition"], row["strategy"]): row["execution_order_position"]
            for row in report["runs"]
        }
        self.assertEqual(positions[(1, "single_agent")], 1)
        self.assertEqual(positions[(1, "multi_agent")], 2)
        self.assertEqual(positions[(2, "multi_agent")], 1)
        self.assertEqual(positions[(2, "single_agent")], 2)
        comparison = report["paired_comparisons"]["multi_agent_vs_single_agent"]
        self.assertEqual(comparison["all_cases"]["pair_count"], 2)
        self.assertEqual(len(comparison["all_cases"]["bootstrap_95_ci_mean_latency_delta_ms"]), 2)

    def test_adaptive_strategies_avoid_unneeded_healthy_service_tools(self) -> None:
        healthy = next(case for case in default_cases() if case.case_id == "order-api-healthy")
        with tempfile.TemporaryDirectory() as directory:
            report = BenchmarkRunner(
                cases=(healthy,),
                strategy_names=(
                    "fixed_workflow",
                    "single_agent",
                    "plan_executor",
                    "multi_agent",
                    "adaptive_harness",
                ),
            ).run(workspace=directory)
        rows = {row["strategy"]: row for row in report["runs"]}
        self.assertEqual(rows["single_agent"]["tool_attempts"], 1)
        self.assertEqual(rows["plan_executor"]["tool_attempts"], 1)
        self.assertEqual(rows["fixed_workflow"]["tool_attempts"], 3)
        self.assertEqual(rows["multi_agent"]["tool_attempts"], 3)
        self.assertEqual(rows["adaptive_harness"]["tool_attempts"], 1)

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
        self.assertEqual(loaded["schema_version"], 4)
        self.assertEqual(set(loaded["runs"][0]["grader_scores"]), {"outcome", "evidence", "safety", "recovery"})
        self.assertEqual(loaded["execution_mode"], "deterministic_synthetic_no_llm")
        self.assertEqual(loaded["strategies"], ["single_agent"])
        self.assertEqual(len(loaded["runs"]), 1)

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BenchmarkRunner(strategy_names=("unknown",))
        with self.assertRaises(ValueError):
            BenchmarkRunner(execution_order="random")
        with self.assertRaises(ValueError):
            BenchmarkRunner(strategy_names=("single_agent",)).run(repetitions=0)


if __name__ == "__main__":
    unittest.main()
