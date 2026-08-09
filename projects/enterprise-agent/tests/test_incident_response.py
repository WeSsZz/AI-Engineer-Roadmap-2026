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
