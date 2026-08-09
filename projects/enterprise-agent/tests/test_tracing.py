from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from enterprise_agent.harness import EnterpriseAgentHarness, SimulatedInterruption
from enterprise_agent.models import SpanKind, SpanStatus, TaskStatus, TraceSpan
from enterprise_agent.store import SQLiteEventStore


OBJECTIVE = "分析华东区域本月订单下降原因，并在确认库存问题后生成补货工单"


class TraceTests(unittest.TestCase):
    def test_runtime_agent_and_tool_spans_form_a_parent_child_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "agent.db")
            with EnterpriseAgentHarness(store=store) as harness:
                result = harness.run(OBJECTIVE)

            spans = store.list_spans(result.task.task_id)
            by_name = {span.name: span for span in spans}
            root = by_name["runtime.run"]
            self.assertIsNone(root.parent_span_id)
            self.assertEqual(root.status, SpanStatus.COMPLETED)
            for name in ("knowledge_researcher", "data_analyst", "verifier", "synthesis_agent"):
                self.assertEqual(by_name[f"agent.{name}"].parent_span_id, root.span_id)
            self.assertEqual(
                by_name["tool.search_docs"].parent_span_id,
                by_name["agent.knowledge_researcher"].span_id,
            )
            self.assertEqual(
                by_name["tool.query_metrics"].parent_span_id,
                by_name["agent.data_analyst"].span_id,
            )
            self.assertTrue(all(span.trace_id == result.task.task_id for span in spans))

    def test_resume_adds_a_new_root_and_traces_the_write_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.db"
            store = SQLiteEventStore(database)
            with EnterpriseAgentHarness(store=store) as harness:
                pending = harness.run(OBJECTIVE)
            with EnterpriseAgentHarness(store=SQLiteEventStore(database)) as harness:
                completed = harness.resume(pending.task.task_id, approval_token="APPROVED_BY_USER")

            self.assertEqual(completed.state.status, TaskStatus.COMPLETED)
            spans = SQLiteEventStore(database).list_spans(pending.task.task_id)
            resume_root = next(span for span in spans if span.name == "runtime.resume")
            action = next(span for span in spans if span.name == "agent.action_executor")
            tool = next(span for span in spans if span.name == "tool.create_ticket")
            self.assertEqual(action.parent_span_id, resume_root.span_id)
            self.assertEqual(tool.parent_span_id, action.span_id)

    def test_interruption_marks_root_error_and_resume_completes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.db"
            with EnterpriseAgentHarness(store=SQLiteEventStore(database)) as harness:
                with self.assertRaises(SimulatedInterruption) as interrupted:
                    harness.run("分析华东区域本月订单下降原因", failpoint="after_specialists")
            task_id = interrupted.exception.task_id
            root = next(span for span in SQLiteEventStore(database).list_spans(task_id) if span.name == "runtime.run")
            self.assertEqual(root.status, SpanStatus.ERROR)
            self.assertEqual(root.error_type, "SimulatedInterruption")

            with EnterpriseAgentHarness(store=SQLiteEventStore(database)) as harness:
                result = harness.resume(task_id)
            self.assertEqual(result.state.status, TaskStatus.COMPLETED)
            resume = next(span for span in SQLiteEventStore(database).list_spans(task_id) if span.name == "runtime.resume")
            self.assertEqual(resume.status, SpanStatus.COMPLETED)

    def test_summary_reports_nearest_rank_percentiles_and_error_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "agent.db")
            for index, duration in enumerate((10.0, 20.0, 30.0, 40.0)):
                store.record_span(
                    TraceSpan(
                        span_id=f"span-{index}",
                        trace_id="task-metrics",
                        task_id="task-metrics",
                        parent_span_id=None,
                        name="tool.test",
                        kind=SpanKind.TOOL,
                        status=SpanStatus.ERROR if index == 3 else SpanStatus.COMPLETED,
                        started_at=datetime.now(UTC).isoformat(),
                        finished_at=datetime.now(UTC).isoformat(),
                        duration_ms=duration,
                    )
                )
            summary = store.trace_summary("task-metrics")
            self.assertEqual(summary["overall"]["p50_ms"], 20.0)
            self.assertEqual(summary["overall"]["p95_ms"], 40.0)
            self.assertEqual(summary["overall"]["error_rate"], 0.25)


if __name__ == "__main__":
    unittest.main()
