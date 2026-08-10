from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from enterprise_agent.context import ContextEngine
from enterprise_agent.harness import EnterpriseAgentHarness, SimulatedInterruption
from enterprise_agent.models import Event, TaskEnvelope, TaskState, TaskStatus
from enterprise_agent.state_machine import InvalidStateTransition, transition
from enterprise_agent.store import ConcurrentCheckpointError, SQLiteEventStore
from enterprise_agent.tools import ToolError, build_demo_runtime


class HarnessTests(unittest.TestCase):
    def test_multi_agent_analysis_requires_approval_for_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = EnterpriseAgentHarness(store=SQLiteEventStore(Path(directory) / "agent.db")).run(
                "分析华东区域本月订单下降原因，并在确认库存问题后生成补货工单"
            )
            names = [item.agent_name for item in result.agent_results]
            self.assertIn("knowledge_researcher", names)
            self.assertIn("data_analyst", names)
            self.assertIn("verifier", names)
            self.assertIn("synthesis_agent", names)
            self.assertEqual(result.state.status, "needs_approval")
            self.assertIn("metric-华东-2026-07", result.state.evidence_refs)
            self.assertIn("doc-ops-stock-001", result.state.evidence_refs)

    def test_explicit_approval_creates_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = EnterpriseAgentHarness(store=SQLiteEventStore(Path(directory) / "agent.db")).run(
                "分析华东区域本月订单下降原因，并在确认库存问题后生成补货工单",
                approval_token="APPROVED_BY_USER",
            )
            self.assertEqual(result.state.status, "completed")
            self.assertEqual(result.state.artifacts["ticket"]["id"], "INC-0001")

    def test_tool_allowlist_is_enforced(self) -> None:
        runtime = build_demo_runtime()
        with self.assertRaises(ToolError):
            runtime.invoke("create_ticket", allowed_tools=("search_docs",), arguments={"title": "x", "description": "y"})

    def test_context_compaction_preserves_structured_state(self) -> None:
        engine = ContextEngine(max_context_chars=200, keep_recent_events=2)
        task = TaskEnvelope("task-1", "测试上下文压缩", ("必须保留用户约束",))
        state = TaskState("task-1", confirmed_facts=["fact-1"], evidence_refs=["evidence-1"])
        events = [Event(i, "tool_result", "agent", {"large": "x" * 100}) for i in range(1, 8)]
        snapshot = engine.build(task, state, events)
        self.assertTrue(snapshot.compacted)
        self.assertIn("fact-1", snapshot.compact_summary)
        self.assertIn("evidence-1", snapshot.compact_summary)
        self.assertEqual(len(snapshot.recent_events), 2)

    def test_resume_after_specialists_uses_checkpoint_without_repeating_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.db"
            first = EnterpriseAgentHarness(store=SQLiteEventStore(database))
            with self.assertRaises(SimulatedInterruption) as interrupted:
                first.run("分析华东区域本月订单下降原因", failpoint="after_specialists")

            task_id = interrupted.exception.task_id
            checkpoint = SQLiteEventStore(database).load(task_id)
            self.assertEqual(checkpoint.state.status, TaskStatus.VERIFYING)
            self.assertIn("metric-华东-2026-07", checkpoint.state.evidence_refs)

            # A new harness simulates a process restart.
            resumed = EnterpriseAgentHarness(store=SQLiteEventStore(database)).resume(task_id)
            self.assertEqual(resumed.state.status, TaskStatus.COMPLETED)
            researcher_events = [
                event
                for event in resumed.events
                if event.event_type == "agent_completed" and event.actor == "knowledge_researcher"
            ]
            analyst_events = [
                event
                for event in resumed.events
                if event.event_type == "agent_completed" and event.actor == "data_analyst"
            ]
            self.assertEqual(len(researcher_events), 1)
            self.assertEqual(len(analyst_events), 1)
            self.assertTrue(any(event.event_type == "task_resumed" for event in resumed.events))

    def test_persisted_approval_survives_second_restart(self) -> None:
        objective = "分析华东区域本月订单下降原因，并在确认库存问题后生成补货工单"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.db"
            pending = EnterpriseAgentHarness(store=SQLiteEventStore(database)).run(objective)
            self.assertEqual(pending.state.status, TaskStatus.WAITING_APPROVAL)

            with self.assertRaises(SimulatedInterruption):
                EnterpriseAgentHarness(store=SQLiteEventStore(database)).resume(
                    pending.task.task_id,
                    approval_token="APPROVED_BY_USER",
                    failpoint="after_approval",
                )

            approved_checkpoint = SQLiteEventStore(database).load(pending.task.task_id)
            self.assertEqual(approved_checkpoint.state.status, TaskStatus.EXECUTING_ACTION)
            self.assertTrue(approved_checkpoint.state.artifacts["approval"]["approved"])

            # The approval decision is persisted, so the new process can continue
            # without persisting or resubmitting the original approval token.
            completed = EnterpriseAgentHarness(store=SQLiteEventStore(database)).resume(pending.task.task_id)
            self.assertEqual(completed.state.status, TaskStatus.COMPLETED)
            self.assertEqual(completed.state.artifacts["ticket"]["id"], "INC-0001")

            event_count = len(completed.events)
            loaded_again = EnterpriseAgentHarness(store=SQLiteEventStore(database)).resume(pending.task.task_id)
            self.assertEqual(len(loaded_again.events), event_count)
            self.assertEqual(loaded_again.state.artifacts["ticket"]["id"], "INC-0001")

    def test_state_machine_rejects_invalid_transition(self) -> None:
        state = TaskState("task-invalid")
        with self.assertRaises(InvalidStateTransition):
            transition(state, TaskStatus.COMPLETED)

    def test_safe_refusal_is_a_terminal_state(self) -> None:
        state = TaskState("task-refused", status=TaskStatus.SYNTHESIZING)
        transition(state, TaskStatus.SAFELY_REFUSED)
        with self.assertRaises(InvalidStateTransition):
            transition(state, TaskStatus.COMPLETED)

    def test_adaptive_orchestration_rejects_non_incident_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with EnterpriseAgentHarness(
                store=SQLiteEventStore(Path(directory) / "agent.db")
            ) as harness:
                with self.assertRaises(ValueError):
                    harness.run(
                        "分析华东区域订单下降原因",
                        scenario="business",
                        orchestration="adaptive",
                    )

    def test_checkpoint_uses_optimistic_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "agent.db")
            pending = EnterpriseAgentHarness(store=store).run(
                "分析华东区域本月订单下降原因，并生成补货工单"
            )
            first_copy = store.load(pending.task.task_id)
            stale_copy = store.load(pending.task.task_id)
            first_copy.state.open_items.append("first writer")
            store.checkpoint(first_copy)
            stale_copy.state.open_items.append("stale writer")
            with self.assertRaises(ConcurrentCheckpointError):
                store.checkpoint(stale_copy)

    def test_write_action_is_idempotent_across_crash_before_checkpoint(self) -> None:
        objective = "分析华东区域本月订单下降原因，并在确认库存问题后生成补货工单"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.db"
            pending = EnterpriseAgentHarness(store=SQLiteEventStore(database)).run(objective)

            with self.assertRaises(SimulatedInterruption):
                EnterpriseAgentHarness(store=SQLiteEventStore(database)).resume(
                    pending.task.task_id,
                    approval_token="APPROVED_BY_USER",
                    failpoint="after_action_before_checkpoint",
                )

            interrupted = SQLiteEventStore(database).load(pending.task.task_id)
            self.assertEqual(interrupted.state.status, TaskStatus.EXECUTING_ACTION)
            with sqlite3.connect(database) as connection:
                ticket_count_before_resume = connection.execute("SELECT COUNT(*) FROM demo_tickets").fetchone()[0]
            self.assertEqual(ticket_count_before_resume, 1)

            completed = EnterpriseAgentHarness(store=SQLiteEventStore(database)).resume(pending.task.task_id)
            self.assertEqual(completed.state.status, TaskStatus.COMPLETED)
            self.assertTrue(completed.state.artifacts["ticket"]["replayed"])
            with sqlite3.connect(database) as connection:
                ticket_count_after_resume = connection.execute("SELECT COUNT(*) FROM demo_tickets").fetchone()[0]
            self.assertEqual(ticket_count_after_resume, 1)


if __name__ == "__main__":
    unittest.main()
