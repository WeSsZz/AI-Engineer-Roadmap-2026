from __future__ import annotations

import concurrent.futures
import uuid
from dataclasses import dataclass, field
from typing import Any

from .agents import DataAnalyst, KnowledgeResearcher, SynthesisAgent, Verifier
from .context import ContextEngine
from .models import AgentResult, Budget, Event, SpanKind, TaskEnvelope, TaskState, TaskStatus
from .state_machine import TERMINAL_STATUSES, transition
from .store import ConcurrentCheckpointError, SQLiteEventStore, StoredExecution
from .tools import ToolRuntime, build_demo_runtime
from .tracing import SQLiteTraceRecorder


class SimulatedInterruption(RuntimeError):
    """A deterministic failpoint used to prove checkpoint/resume behavior."""

    def __init__(self, task_id: str, failpoint: str):
        super().__init__(f"Task {task_id} interrupted at failpoint '{failpoint}'")
        self.task_id = task_id
        self.failpoint = failpoint


@dataclass
class TaskRunResult:
    task: TaskEnvelope
    state: TaskState
    answer: str
    recommendation: str
    agent_results: list[AgentResult] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    context_compacted: bool = False


class EnterpriseAgentHarness:
    """Durable supervisor-style multi-agent runtime.

    Each state transition is checkpointed before the next stage starts. Read-only
    specialist stages are safe to run again if a process dies before their checkpoint;
    completed stages are restored from SQLite and are not repeated.
    """

    def __init__(
        self,
        runtime: ToolRuntime | None = None,
        context_engine: ContextEngine | None = None,
        store: SQLiteEventStore | None = None,
    ):
        self.store = store or SQLiteEventStore()
        self.tracer = SQLiteTraceRecorder(self.store)
        self._owns_runtime = runtime is None
        self.runtime = runtime or build_demo_runtime(self.store.path, self.tracer.record_tool_call)
        if runtime is not None:
            self.runtime.set_record_sink(self.tracer.record_tool_call)
        self.context_engine = context_engine or ContextEngine(max_context_chars=12_000)
        self.researcher = KnowledgeResearcher()
        self.analyst = DataAnalyst()
        self.verifier = Verifier()
        self.synthesis = SynthesisAgent()

    def close(self) -> None:
        if self._owns_runtime:
            self.runtime.close()

    def __enter__(self) -> EnterpriseAgentHarness:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def run(
        self,
        objective: str,
        *,
        constraints: tuple[str, ...] = ("必须提供证据引用", "默认只读，写操作需要人工审批"),
        approval_token: str | None = None,
        max_context_chars: int | None = None,
        failpoint: str | None = None,
    ) -> TaskRunResult:
        task = TaskEnvelope(
            task_id=f"task-{uuid.uuid4().hex[:8]}",
            objective=objective,
            constraints=constraints,
            allowed_tools=("search_docs", "query_metrics", "create_ticket"),
            budget=Budget(),
            risk_level="medium" if "工单" in objective or "创建" in objective else "low",
        )
        if max_context_chars is not None:
            self.context_engine.max_context_chars = max_context_chars
        execution = StoredExecution(task=task, state=TaskState(task_id=task.task_id), agent_results=[], events=[])
        self._emit(execution, "task_created", "supervisor", {"objective": objective, "risk_level": task.risk_level})
        self.store.create(execution)
        with self.tracer.start_span(
            trace_id=task.task_id,
            task_id=task.task_id,
            name="runtime.run",
            kind=SpanKind.RUNTIME,
            attributes={"initial_status": execution.state.status.value},
        ) as span:
            result = self._execute(
                execution,
                approval_token=approval_token,
                failpoint=failpoint,
                parent_span_id=span.span_id,
            )
            span.set_attribute("final_status", result.state.status.value)
            return result

    def resume(
        self,
        task_id: str,
        *,
        approval_token: str | None = None,
        failpoint: str | None = None,
    ) -> TaskRunResult:
        execution = self.store.load(task_id)
        if execution.state.status in TERMINAL_STATUSES:
            return self._result(execution)
        self._emit(
            execution,
            "task_resumed",
            "supervisor",
            {"status": execution.state.status.value, "checkpoint_version": execution.state.version},
        )
        self._checkpoint(execution)
        with self.tracer.start_span(
            trace_id=task_id,
            task_id=task_id,
            name="runtime.resume",
            kind=SpanKind.RUNTIME,
            attributes={"initial_status": execution.state.status.value},
        ) as span:
            result = self._execute(
                execution,
                approval_token=approval_token,
                failpoint=failpoint,
                parent_span_id=span.span_id,
            )
            span.set_attribute("final_status", result.state.status.value)
            return result

    def get_task(self, task_id: str) -> TaskRunResult:
        return self._result(self.store.load(task_id))

    def _execute(
        self,
        execution: StoredExecution,
        *,
        approval_token: str | None,
        failpoint: str | None,
        parent_span_id: str,
    ) -> TaskRunResult:
        try:
            while execution.state.status not in TERMINAL_STATUSES:
                status = execution.state.status

                if status == TaskStatus.CREATED:
                    transition(execution.state, TaskStatus.RUNNING_SPECIALISTS)
                    self._emit(
                        execution,
                        "state_transition",
                        "supervisor",
                        {"to": TaskStatus.RUNNING_SPECIALISTS.value},
                    )
                    self._checkpoint(execution)
                    self._interrupt_if_requested(execution, failpoint, "after_task_started")
                    continue

                if status == TaskStatus.RUNNING_SPECIALISTS:
                    self._run_specialists(execution, parent_span_id)
                    transition(execution.state, TaskStatus.VERIFYING)
                    self._emit(
                        execution,
                        "state_transition",
                        "supervisor",
                        {"to": TaskStatus.VERIFYING.value},
                    )
                    self._checkpoint(execution)
                    self._interrupt_if_requested(execution, failpoint, "after_specialists")
                    continue

                if status == TaskStatus.VERIFYING:
                    context = self.context_engine.build(execution.task, execution.state, execution.events)
                    specialists = [
                        result
                        for result in execution.agent_results
                        if result.agent_name in {self.researcher.name, self.analyst.name}
                    ]
                    with self._agent_span(execution, self.verifier.name, parent_span_id):
                        verify = self.verifier.run(execution.task, execution.state, context, specialists)
                    execution.agent_results.append(verify)
                    self._emit(
                        execution,
                        "agent_completed",
                        verify.agent_name,
                        {"status": verify.status, "warnings": verify.warnings},
                    )
                    transition(execution.state, TaskStatus.SYNTHESIZING)
                    self._emit(
                        execution,
                        "state_transition",
                        "supervisor",
                        {"to": TaskStatus.SYNTHESIZING.value},
                    )
                    self._checkpoint(execution)
                    self._interrupt_if_requested(execution, failpoint, "after_verification")
                    continue

                if status == TaskStatus.SYNTHESIZING:
                    context = self.context_engine.build(execution.task, execution.state, execution.events)
                    with self._agent_span(execution, self.synthesis.name, parent_span_id):
                        synthesis = self.synthesis.run(
                            execution.task,
                            execution.state,
                            context,
                            execution.agent_results,
                        )
                    execution.agent_results.append(synthesis)
                    self._emit(
                        execution,
                        "agent_completed",
                        synthesis.agent_name,
                        {"status": synthesis.status, "summary": synthesis.summary},
                    )
                    if synthesis.status == "failed":
                        target = TaskStatus.FAILED
                    elif synthesis.next_action == "prepare_ticket":
                        execution.state.artifacts["pending_action"] = {
                            "action_id": f"{execution.task.task_id}:create_ticket:1",
                            "tool_name": "create_ticket",
                            "risk_level": "high",
                            "arguments": {
                                "title": "华东区域库存异常补货",
                                "description": synthesis.data["recommendation"],
                            },
                        }
                        target = TaskStatus.WAITING_APPROVAL
                    else:
                        target = TaskStatus.COMPLETED
                    transition(execution.state, target)
                    self._emit(execution, "state_transition", "supervisor", {"to": target.value})
                    self._checkpoint(execution)
                    self._interrupt_if_requested(execution, failpoint, "after_synthesis")
                    if target == TaskStatus.COMPLETED:
                        self._emit(execution, "task_finished", "supervisor", {"status": target.value})
                        self._checkpoint(execution)
                    continue

                if status == TaskStatus.WAITING_APPROVAL:
                    if approval_token != "APPROVED_BY_USER":
                        return self._result(execution)
                    pending_action = execution.state.artifacts["pending_action"]
                    execution.state.artifacts["approval"] = {
                        "approved": True,
                        "action_id": pending_action["action_id"],
                    }
                    transition(execution.state, TaskStatus.EXECUTING_ACTION)
                    self._emit(
                        execution,
                        "approval_granted",
                        "human",
                        {"action_id": pending_action["action_id"]},
                    )
                    self._checkpoint(execution)
                    self._interrupt_if_requested(execution, failpoint, "after_approval")
                    continue

                if status == TaskStatus.EXECUTING_ACTION:
                    self._execute_action(execution, parent_span_id)
                    self._interrupt_if_requested(execution, failpoint, "after_action_before_checkpoint")
                    transition(execution.state, TaskStatus.COMPLETED)
                    self._emit(
                        execution,
                        "task_finished",
                        "supervisor",
                        {"status": TaskStatus.COMPLETED.value},
                    )
                    self._checkpoint(execution)
                    self._interrupt_if_requested(execution, failpoint, "after_action")
                    continue

                raise RuntimeError(f"Unhandled task status: {status.value}")

        except (SimulatedInterruption, ConcurrentCheckpointError):
            raise
        except Exception as exc:
            execution.state.errors.append(f"{type(exc).__name__}: {exc}")
            if execution.state.status not in TERMINAL_STATUSES:
                transition(execution.state, TaskStatus.FAILED)
            self._emit(
                execution,
                "task_failed",
                "supervisor",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            self._checkpoint(execution)
        return self._result(execution)

    def _run_specialists(self, execution: StoredExecution, parent_span_id: str) -> None:
        context = self.context_engine.build(execution.task, execution.state, execution.events)
        self._emit(
            execution,
            "parallel_dispatch",
            "supervisor",
            {"agents": [self.researcher.name, self.analyst.name]},
        )
        def run_specialist(agent: KnowledgeResearcher | DataAnalyst) -> AgentResult:
            with self._agent_span(execution, agent.name, parent_span_id) as span:
                return agent.run(execution.task, execution.state, context, self.runtime, span.span_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(run_specialist, self.researcher),
                pool.submit(run_specialist, self.analyst),
            ]
            results = [future.result() for future in futures]
        for result in results:
            execution.state.completed_steps.append(
                "knowledge_research" if result.agent_name == self.researcher.name else "metrics_analysis"
            )
            execution.state.evidence_refs.extend(evidence.evidence_id for evidence in result.evidence)
            execution.state.confirmed_facts.extend(result.data.get("facts", []))
            execution.agent_results.append(result)
            self._emit(
                execution,
                "agent_completed",
                result.agent_name,
                {"status": result.status, "summary": result.summary, "tool_call": result.data.get("tool_call")},
            )

    def _execute_action(self, execution: StoredExecution, parent_span_id: str) -> None:
        approval = execution.state.artifacts.get("approval", {})
        pending_action = execution.state.artifacts.get("pending_action", {})
        if approval.get("approved") is not True or approval.get("action_id") != pending_action.get("action_id"):
            raise PermissionError("Persisted human approval is required before a write action")
        with self._agent_span(execution, "action_executor", parent_span_id) as span:
            ticket_output = self.runtime.invoke(
                pending_action["tool_name"],
                allowed_tools=execution.task.allowed_tools,
                arguments={
                    **pending_action["arguments"],
                    "approval_token": "APPROVED_BY_USER",
                    "idempotency_key": pending_action["action_id"],
                },
                task_id=execution.task.task_id,
                trace_id=execution.task.task_id,
                parent_span_id=span.span_id,
            )
        if ticket_output.status != "completed":
            raise RuntimeError(f"Action executor returned unexpected status: {ticket_output.status}")
        execution.state.artifacts["ticket"] = ticket_output.data
        self._emit(
            execution,
            "action_result",
            "action_executor",
            {"status": ticket_output.status, "data": ticket_output.data, "tool_call": ticket_output.metadata},
        )

    def _checkpoint(self, execution: StoredExecution) -> None:
        self.store.checkpoint(execution)

    def _agent_span(self, execution: StoredExecution, agent_name: str, parent_span_id: str):
        return self.tracer.start_span(
            trace_id=execution.task.task_id,
            task_id=execution.task.task_id,
            parent_span_id=parent_span_id,
            name=f"agent.{agent_name}",
            kind=SpanKind.AGENT,
        )

    @staticmethod
    def _emit(execution: StoredExecution, event_type: str, actor: str, payload: dict[str, Any]) -> None:
        sequence = execution.events[-1].sequence + 1 if execution.events else 1
        execution.events.append(Event(sequence, event_type, actor, payload))

    @staticmethod
    def _interrupt_if_requested(execution: StoredExecution, requested: str | None, current: str) -> None:
        if requested == current:
            raise SimulatedInterruption(execution.task.task_id, current)

    def _result(self, execution: StoredExecution) -> TaskRunResult:
        context = self.context_engine.build(execution.task, execution.state, execution.events)
        return TaskRunResult(
            task=execution.task,
            state=execution.state,
            answer=execution.state.artifacts.get("answer", ""),
            recommendation=execution.state.artifacts.get("recommendation", ""),
            agent_results=execution.agent_results,
            events=execution.events,
            context_compacted=context.compacted,
        )
