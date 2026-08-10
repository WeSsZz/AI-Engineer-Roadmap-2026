from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from enterprise_agent.faults import FaultInjector, FaultRule
from enterprise_agent.harness import EnterpriseAgentHarness, SimulatedInterruption
from enterprise_agent.models import SpanStatus, TaskStatus
from enterprise_agent.store import SQLiteEventStore


OBJECTIVE = "诊断 payment-api 错误率和延迟突增的根因，并生成故障工单"


class IncidentResponseTests(unittest.TestCase):
    def test_adaptive_active_triages_healthy_incident_with_metrics_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.db"
            store = SQLiteEventStore(database)
            with EnterpriseAgentHarness(store=store) as harness:
                result = harness.run(
                    "检查 order-api 告警是否构成事故，并在必要时生成故障工单",
                    scenario="incident",
                    orchestration="adaptive",
                )
                tool_names = [record.tool_name for record in harness.runtime.invocations]
            with EnterpriseAgentHarness(store=SQLiteEventStore(database)) as resumed_harness:
                resumed = resumed_harness.resume(result.task.task_id)
                resumed_tools = list(resumed_harness.runtime.invocations)
            runtime_span = next(
                span for span in store.list_spans(result.task.task_id) if span.name == "runtime.run"
            )
        self.assertEqual(tool_names, ["query_service_metrics"])
        self.assertEqual(result.task.metadata["orchestration"], "adaptive")
        self.assertEqual(result.state.status, TaskStatus.SAFELY_REFUSED)
        self.assertEqual(result.state.artifacts["outcome_class"], "safe_refusal")
        self.assertIn("未达到事故阈值", result.answer)
        self.assertIn("保持监控", result.recommendation)
        decision = result.state.artifacts["routing_decision"]
        self.assertEqual(decision["selected_route"], "single_agent_triage")
        self.assertEqual(decision["actual_route"], "single_agent_triage")
        self.assertFalse(decision["shadow"])
        self.assertNotIn("pending_action", result.state.artifacts)
        self.assertEqual(resumed.state.status, TaskStatus.SAFELY_REFUSED)
        self.assertEqual(resumed_tools, [])
        self.assertEqual(runtime_span.status, SpanStatus.COMPLETED)
        self.assertEqual(runtime_span.attributes["final_status"], "safely_refused")
        self.assertEqual(runtime_span.attributes["outcome_class"], "safe_refusal")

    def test_adaptive_active_escalates_anomaly_and_requires_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "agent.db")
            with EnterpriseAgentHarness(store=store) as harness:
                result = harness.run(
                    OBJECTIVE,
                    scenario="incident",
                    orchestration="adaptive",
                )
                tool_names = [record.tool_name for record in harness.runtime.invocations]
            router_span = next(
                span
                for span in store.list_spans(result.task.task_id)
                if span.name == "agent.risk_router"
            )
            runtime_span = next(
                span
                for span in store.list_spans(result.task.task_id)
                if span.name == "runtime.run"
            )
        self.assertEqual(result.state.status, TaskStatus.WAITING_APPROVAL)
        self.assertEqual(result.state.artifacts["outcome_class"], "awaiting_approval")
        self.assertEqual(
            set(tool_names),
            {"query_service_metrics", "search_logs", "get_runbook"},
        )
        decision = result.state.artifacts["routing_decision"]
        self.assertEqual(decision["selected_route"], "verified_multi_agent")
        self.assertEqual(decision["reason"], "high_risk_anomaly_with_write_intent")
        self.assertTrue(any(event.event_type == "routing_decision" for event in result.events))
        self.assertEqual(router_span.attributes["risk_score"], 3)
        self.assertEqual(runtime_span.attributes["outcome_class"], "awaiting_approval")

    def test_shadow_mode_records_decision_but_keeps_fixed_multi_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with EnterpriseAgentHarness(
                store=SQLiteEventStore(Path(directory) / "agent.db")
            ) as harness:
                result = harness.run(
                    "检查 order-api 告警是否构成事故，并在必要时生成故障工单",
                    scenario="incident",
                    orchestration="adaptive_shadow",
                )
                tool_names = [record.tool_name for record in harness.runtime.invocations]
        self.assertEqual(
            set(tool_names),
            {"query_service_metrics", "search_logs", "get_runbook"},
        )
        decision = result.state.artifacts["routing_decision"]
        self.assertEqual(decision["selected_route"], "single_agent_triage")
        self.assertEqual(decision["actual_route"], "fixed_multi_agent")
        self.assertTrue(decision["shadow"])
        self.assertTrue(decision["would_reduce_specialists"])
        self.assertEqual(result.state.status, TaskStatus.SAFELY_REFUSED)
        self.assertEqual(result.state.artifacts["outcome_class"], "safe_refusal")

    def test_adaptive_router_fails_closed_when_metrics_are_unavailable(self) -> None:
        injector = FaultInjector((FaultRule("query_service_metrics", "permanent"),))
        with tempfile.TemporaryDirectory() as directory:
            with EnterpriseAgentHarness(
                store=SQLiteEventStore(Path(directory) / "agent.db"),
                fault_injector=injector,
            ) as harness:
                result = harness.run(
                    OBJECTIVE,
                    scenario="incident",
                    orchestration="adaptive",
                )
                tool_names = [record.tool_name for record in harness.runtime.invocations]
        decision = result.state.artifacts["routing_decision"]
        self.assertEqual(decision["selected_route"], "verified_multi_agent")
        self.assertTrue(decision["fail_closed"])
        self.assertEqual(
            set(tool_names),
            {"query_service_metrics", "search_logs", "get_runbook"},
        )
        self.assertEqual(result.state.status, TaskStatus.FAILED)
        self.assertEqual(result.state.artifacts["outcome_class"], "execution_failure")
        self.assertNotIn("pending_action", result.state.artifacts)

    def test_adaptive_resume_reuses_checkpointed_routing_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.db"
            with EnterpriseAgentHarness(store=SQLiteEventStore(database)) as harness:
                with self.assertRaises(SimulatedInterruption) as interrupted:
                    harness.run(
                        OBJECTIVE,
                        scenario="incident",
                        orchestration="adaptive",
                        failpoint="after_routing_decision",
                    )
            checkpoint = SQLiteEventStore(database).load(interrupted.exception.task_id)
            self.assertEqual(checkpoint.state.status, TaskStatus.RUNNING_SPECIALISTS)
            self.assertEqual(
                checkpoint.state.artifacts["routing_decision"]["selected_route"],
                "verified_multi_agent",
            )
            with EnterpriseAgentHarness(store=SQLiteEventStore(database)) as harness:
                result = harness.resume(interrupted.exception.task_id)
                resumed_tools = [record.tool_name for record in harness.runtime.invocations]
            self.assertEqual(set(resumed_tools), {"search_logs", "get_runbook"})
            self.assertEqual(result.state.status, TaskStatus.WAITING_APPROVAL)
            metric_events = [
                event
                for event in result.events
                if event.event_type == "agent_completed" and event.actor == "metrics_investigator"
            ]
            routing_events = [event for event in result.events if event.event_type == "routing_decision"]
            self.assertEqual(len(metric_events), 1)
            self.assertEqual(len(routing_events), 1)

    def test_three_specialists_cross_validate_incident_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "agent.db")
            with EnterpriseAgentHarness(store=store) as harness:
                result = harness.run(OBJECTIVE, scenario="incident")

            self.assertEqual(result.state.status, TaskStatus.WAITING_APPROVAL)
            self.assertEqual(result.state.artifacts["root_cause_confidence"], "high")
            self.assertIn("数据库连接池耗尽", result.answer)
            names = {item.agent_name for item in result.agent_results}
            self.assertTrue(
                {"metrics_investigator", "log_investigator", "runbook_researcher", "verifier"}.issubset(names)
            )
            self.assertIn("metrics-payment-api-15m", result.state.evidence_refs)
            self.assertIn("logs-payment-api-db_pool_timeout", result.state.evidence_refs)
            self.assertIn("runbook-payment-db-pool-v3", result.state.evidence_refs)
            self.assertEqual(
                result.state.artifacts["pending_action"]["arguments"]["title"],
                "payment-api P1 故障处置",
            )

    def test_approved_incident_action_creates_idempotent_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with EnterpriseAgentHarness(store=SQLiteEventStore(Path(directory) / "agent.db")) as harness:
                result = harness.run(OBJECTIVE, scenario="incident", approval_token="APPROVED_BY_USER")
            self.assertEqual(result.state.status, TaskStatus.COMPLETED)
            self.assertEqual(result.state.artifacts["ticket"]["id"], "INC-0001")

    def test_transient_metrics_failure_recovers_and_is_visible_in_trace(self) -> None:
        injector = FaultInjector((FaultRule("query_service_metrics", "transient"),))
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "agent.db")
            with EnterpriseAgentHarness(store=store, fault_injector=injector) as harness:
                result = harness.run(OBJECTIVE, scenario="incident")

            self.assertEqual(result.state.status, TaskStatus.WAITING_APPROVAL)
            attempts = [span for span in store.list_spans(result.task.task_id) if span.name == "tool.query_service_metrics"]
            self.assertEqual([span.status for span in attempts], [SpanStatus.ERROR, SpanStatus.COMPLETED])
            self.assertEqual(attempts[0].error_type, "ToolTransientError")
            self.assertEqual(injector.triggered, (("query_service_metrics", "transient"),))

    def test_permanent_log_failure_blocks_root_cause_and_write_action(self) -> None:
        injector = FaultInjector((FaultRule("search_logs", "permanent"),))
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "agent.db")
            with EnterpriseAgentHarness(store=store, fault_injector=injector) as harness:
                result = harness.run(OBJECTIVE, scenario="incident")

            self.assertEqual(result.state.status, TaskStatus.FAILED)
            self.assertEqual(result.state.artifacts["outcome_class"], "execution_failure")
            self.assertEqual(result.state.artifacts["root_cause_confidence"], "insufficient")
            self.assertNotIn("pending_action", result.state.artifacts)
            log_result = next(item for item in result.agent_results if item.agent_name == "log_investigator")
            self.assertEqual(log_result.status, "failed")
            self.assertEqual(log_result.data["error_type"], "ToolPermanentError")
            log_span = next(span for span in store.list_spans(result.task.task_id) if span.name == "agent.log_investigator")
            self.assertEqual(log_span.status, SpanStatus.ERROR)
            self.assertEqual(log_span.error_type, "AgentResultFailed")

    def test_timeout_fault_retries_without_losing_trace_parent(self) -> None:
        injector = FaultInjector((FaultRule("get_runbook", "timeout", delay_seconds=0.08),))
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "agent.db")
            with EnterpriseAgentHarness(store=store, fault_injector=injector) as harness:
                result = harness.run(OBJECTIVE, scenario="incident")

            self.assertEqual(result.state.status, TaskStatus.WAITING_APPROVAL)
            spans = store.list_spans(result.task.task_id)
            agent = next(span for span in spans if span.name == "agent.runbook_researcher")
            attempts = [span for span in spans if span.name == "tool.get_runbook"]
            self.assertEqual(len(attempts), 2)
            self.assertEqual({span.parent_span_id for span in attempts}, {agent.span_id})
            self.assertEqual([span.status for span in attempts], [SpanStatus.ERROR, SpanStatus.COMPLETED])

    def test_incident_specialists_are_not_repeated_after_checkpoint_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.db"
            with EnterpriseAgentHarness(store=SQLiteEventStore(database)) as harness:
                with self.assertRaises(SimulatedInterruption) as interrupted:
                    harness.run(OBJECTIVE, scenario="incident", failpoint="after_specialists")
            with EnterpriseAgentHarness(store=SQLiteEventStore(database)) as harness:
                result = harness.resume(interrupted.exception.task_id)
            for name in ("metrics_investigator", "log_investigator", "runbook_researcher"):
                events = [event for event in result.events if event.event_type == "agent_completed" and event.actor == name]
                self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
