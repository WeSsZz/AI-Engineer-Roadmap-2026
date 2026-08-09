from __future__ import annotations

import json
import platform
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .faults import FaultInjector, FaultRule
from .harness import EnterpriseAgentHarness
from .models import Evidence, SpanKind
from .store import SQLiteEventStore
from .tool_gateway import ToolError, ToolOutput, ToolRuntime
from .tools import build_demo_runtime
from .tracing import SQLiteTraceRecorder


DATASET_VERSION = "incident-diagnosis-v1"
INCIDENT_TOOLS = ("query_service_metrics", "search_logs", "get_runbook", "create_ticket")


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    objective: str
    service: str
    expected_root_cause: str | None
    expected_evidence_types: tuple[str, ...]
    expected_tools: tuple[str, ...]
    fault_rules: tuple[FaultRule, ...] = ()
    recoverable_fault: bool = False


@dataclass
class StrategyOutcome:
    strategy: str
    case_id: str
    task_id: str
    status: str
    root_cause: str | None
    confidence: str
    evidence: list[Evidence] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    tool_attempts: int = 0
    latency_ms: float = 0.0
    safely_refused: bool = False
    recovered: bool = False
    error: str | None = None


class BenchmarkStrategy(Protocol):
    name: str

    def run(self, case: BenchmarkCase, workspace: Path) -> StrategyOutcome: ...


def default_cases() -> tuple[BenchmarkCase, ...]:
    payment_objective = "诊断 payment-api 错误率和延迟突增的根因，并生成故障工单"
    return (
        BenchmarkCase(
            "payment-db-pool",
            payment_objective,
            "payment-api",
            "database_pool_exhaustion",
            ("service_metrics", "log_query", "runbook"),
            ("query_service_metrics", "search_logs", "get_runbook"),
        ),
        BenchmarkCase(
            "payment-metrics-transient",
            payment_objective,
            "payment-api",
            "database_pool_exhaustion",
            ("service_metrics", "log_query", "runbook"),
            ("query_service_metrics", "search_logs", "get_runbook"),
            (FaultRule("query_service_metrics", "transient"),),
            True,
        ),
        BenchmarkCase(
            "payment-runbook-timeout",
            payment_objective,
            "payment-api",
            "database_pool_exhaustion",
            ("service_metrics", "log_query", "runbook"),
            ("query_service_metrics", "search_logs", "get_runbook"),
            (FaultRule("get_runbook", "timeout", delay_seconds=0.08),),
            True,
        ),
        BenchmarkCase(
            "payment-logs-permanent",
            payment_objective,
            "payment-api",
            None,
            ("service_metrics",),
            ("query_service_metrics", "search_logs"),
            (FaultRule("search_logs", "permanent"),),
        ),
        BenchmarkCase(
            "order-api-healthy",
            "检查 order-api 告警是否构成事故，并在必要时生成故障工单",
            "order-api",
            None,
            ("service_metrics",),
            ("query_service_metrics",),
        ),
    )


class _DirectStrategy:
    name = "base"

    def run(self, case: BenchmarkCase, workspace: Path) -> StrategyOutcome:
        task_id = f"bench-{self.name}-{case.case_id}-{uuid.uuid4().hex[:6]}"
        store = SQLiteEventStore(workspace / "trace.db")
        tracer = SQLiteTraceRecorder(store)
        runtime = build_demo_runtime(
            workspace / "tools.db",
            tracer.record_tool_call,
            fault_injector=FaultInjector(case.fault_rules),
        )
        started = time.monotonic()
        try:
            outcome = self._execute(case, task_id, runtime, tracer)
        except Exception as exc:
            outcome = StrategyOutcome(
                self.name,
                case.case_id,
                task_id,
                "error",
                None,
                "insufficient",
                safely_refused=True,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            runtime.close()
        outcome.latency_ms = (time.monotonic() - started) * 1000
        records = [record for record in runtime.invocations if record.task_id == task_id]
        outcome.tool_names = list(dict.fromkeys(record.tool_name for record in records))
        outcome.tool_attempts = len(records)
        outcome.recovered = bool(
            case.recoverable_fault
            and outcome.root_cause == case.expected_root_cause
            and any(record.status == "failed" for record in records)
            and any(record.status == "completed" for record in records)
        )
        return outcome

    def _execute(
        self,
        case: BenchmarkCase,
        task_id: str,
        runtime: ToolRuntime,
        tracer: SQLiteTraceRecorder,
    ) -> StrategyOutcome:
        raise NotImplementedError

    @staticmethod
    def _call(runtime: ToolRuntime, task_id: str, parent_span_id: str, name: str, arguments: dict[str, Any]) -> ToolOutput:
        return runtime.invoke(
            name,
            allowed_tools=INCIDENT_TOOLS,
            arguments=arguments,
            task_id=task_id,
            trace_id=task_id,
            parent_span_id=parent_span_id,
        )

    def _execute_tools(
        self,
        case: BenchmarkCase,
        task_id: str,
        runtime: ToolRuntime,
        parent_span_id: str,
        *,
        adaptive: bool,
    ) -> StrategyOutcome:
        evidence: list[Evidence] = []
        try:
            metrics = self._call(
                runtime,
                task_id,
                parent_span_id,
                "query_service_metrics",
                {"service": case.service, "window_minutes": 15},
            )
            evidence.extend(metrics.evidence)
            anomalous = metrics.data["error_rate"] >= 0.05 and metrics.data["db_pool_utilization"] >= 0.90
            if adaptive and not anomalous:
                return self._outcome(case, task_id, evidence, None)
            logs = self._call(
                runtime,
                task_id,
                parent_span_id,
                "search_logs",
                {"service": case.service, "severity": "error", "limit": 20},
            )
            evidence.extend(logs.evidence)
            if adaptive and not logs.data["entries"]:
                return self._outcome(case, task_id, evidence, None)
            runbook = self._call(
                runtime,
                task_id,
                parent_span_id,
                "get_runbook",
                {"service": case.service, "symptom": "database_pool_exhaustion"},
            )
            evidence.extend(runbook.evidence)
            correlated = bool(
                anomalous
                and any(entry["code"] == "DB_POOL_TIMEOUT" for entry in logs.data["entries"])
                and runbook.data["runbook"]
            )
            return self._outcome(
                case,
                task_id,
                evidence,
                "database_pool_exhaustion" if correlated else None,
            )
        except ToolError as exc:
            outcome = self._outcome(case, task_id, evidence, None)
            outcome.error = f"{type(exc).__name__}: {exc}"
            return outcome

    def _outcome(
        self,
        case: BenchmarkCase,
        task_id: str,
        evidence: list[Evidence],
        root_cause: str | None,
    ) -> StrategyOutcome:
        return StrategyOutcome(
            strategy=self.name,
            case_id=case.case_id,
            task_id=task_id,
            status="completed" if root_cause else "refused",
            root_cause=root_cause,
            confidence="high" if root_cause else "insufficient",
            evidence=evidence,
            safely_refused=root_cause is None,
        )


class FixedWorkflowStrategy(_DirectStrategy):
    name = "fixed_workflow"

    def _execute(self, case: BenchmarkCase, task_id: str, runtime: ToolRuntime, tracer: SQLiteTraceRecorder) -> StrategyOutcome:
        with tracer.start_span(
            trace_id=task_id,
            task_id=task_id,
            name="strategy.fixed_workflow",
            kind=SpanKind.RUNTIME,
        ) as span:
            return self._execute_tools(case, task_id, runtime, span.span_id, adaptive=False)


class SingleAgentStrategy(_DirectStrategy):
    name = "single_agent"

    def _execute(self, case: BenchmarkCase, task_id: str, runtime: ToolRuntime, tracer: SQLiteTraceRecorder) -> StrategyOutcome:
        with tracer.start_span(
            trace_id=task_id,
            task_id=task_id,
            name="strategy.single_agent",
            kind=SpanKind.RUNTIME,
        ) as root:
            with tracer.start_span(
                trace_id=task_id,
                task_id=task_id,
                parent_span_id=root.span_id,
                name="agent.incident_generalist",
                kind=SpanKind.AGENT,
            ) as agent:
                outcome = self._execute_tools(case, task_id, runtime, agent.span_id, adaptive=True)
                agent.set_attribute("result_status", outcome.status)
                return outcome


class PlanExecutorStrategy(_DirectStrategy):
    name = "plan_executor"

    def _execute(self, case: BenchmarkCase, task_id: str, runtime: ToolRuntime, tracer: SQLiteTraceRecorder) -> StrategyOutcome:
        with tracer.start_span(
            trace_id=task_id,
            task_id=task_id,
            name="strategy.plan_executor",
            kind=SpanKind.RUNTIME,
        ) as root:
            with tracer.start_span(
                trace_id=task_id,
                task_id=task_id,
                parent_span_id=root.span_id,
                name="agent.planner",
                kind=SpanKind.AGENT,
                attributes={"plan": ["metrics", "logs_if_anomalous", "runbook_if_correlated"]},
            ):
                pass
            with tracer.start_span(
                trace_id=task_id,
                task_id=task_id,
                parent_span_id=root.span_id,
                name="agent.executor",
                kind=SpanKind.AGENT,
            ) as executor:
                outcome = self._execute_tools(case, task_id, runtime, executor.span_id, adaptive=True)
                executor.set_attribute("result_status", outcome.status)
                return outcome


class MultiAgentStrategy:
    name = "multi_agent"

    def run(self, case: BenchmarkCase, workspace: Path) -> StrategyOutcome:
        store = SQLiteEventStore(workspace / "agent.db")
        harness = EnterpriseAgentHarness(
            store=store,
            fault_injector=FaultInjector(case.fault_rules),
        )
        started = time.monotonic()
        try:
            result = harness.run(case.objective, scenario="incident")
            records = harness.runtime.invocations
        finally:
            harness.close()
        latency_ms = (time.monotonic() - started) * 1000
        root_cause = (
            "database_pool_exhaustion"
            if result.state.artifacts.get("root_cause_confidence") == "high"
            else None
        )
        task_records = [record for record in records if record.task_id == result.task.task_id]
        return StrategyOutcome(
            strategy=self.name,
            case_id=case.case_id,
            task_id=result.task.task_id,
            status=result.state.status.value,
            root_cause=root_cause,
            confidence=result.state.artifacts.get("root_cause_confidence", "insufficient"),
            evidence=[evidence for item in result.agent_results for evidence in item.evidence],
            tool_names=list(dict.fromkeys(record.tool_name for record in task_records)),
            tool_attempts=len(task_records),
            latency_ms=latency_ms,
            safely_refused=root_cause is None and "pending_action" not in result.state.artifacts,
            recovered=bool(
                case.recoverable_fault
                and root_cause == case.expected_root_cause
                and any(record.status == "failed" for record in task_records)
                and any(record.status == "completed" for record in task_records)
            ),
            error="; ".join(result.state.errors) or None,
        )


STRATEGIES: dict[str, type[BenchmarkStrategy]] = {
    "fixed_workflow": FixedWorkflowStrategy,
    "single_agent": SingleAgentStrategy,
    "plan_executor": PlanExecutorStrategy,
    "multi_agent": MultiAgentStrategy,
}


class BenchmarkRunner:
    def __init__(
        self,
        cases: tuple[BenchmarkCase, ...] | None = None,
        strategy_names: tuple[str, ...] | None = None,
    ) -> None:
        self.cases = cases or default_cases()
        self.strategy_names = strategy_names or tuple(STRATEGIES)
        unknown = sorted(set(self.strategy_names) - set(STRATEGIES))
        if unknown:
            raise ValueError(f"Unknown benchmark strategies: {', '.join(unknown)}")

    def run(self, *, repetitions: int = 1, workspace: str | Path | None = None) -> dict[str, Any]:
        if repetitions <= 0:
            raise ValueError("repetitions must be positive")
        owns_workspace = workspace is None
        temporary = tempfile.TemporaryDirectory() if owns_workspace else None
        root = Path(temporary.name if temporary else workspace)  # type: ignore[arg-type, union-attr]
        root.mkdir(parents=True, exist_ok=True)
        outcomes: list[StrategyOutcome] = []
        try:
            for repetition in range(repetitions):
                for case in self.cases:
                    for strategy_name in self.strategy_names:
                        run_directory = root / f"r{repetition}-{case.case_id}-{strategy_name}"
                        run_directory.mkdir(parents=True, exist_ok=True)
                        outcomes.append(STRATEGIES[strategy_name]().run(case, run_directory))
        finally:
            if temporary is not None:
                temporary.cleanup()
        return self._report(outcomes, repetitions)

    def _report(self, outcomes: list[StrategyOutcome], repetitions: int) -> dict[str, Any]:
        case_map = {case.case_id: case for case in self.cases}
        run_rows = [self._score(outcome, case_map[outcome.case_id]) for outcome in outcomes]
        aggregates = {
            strategy: self._aggregate([row for row in run_rows if row["strategy"] == strategy])
            for strategy in self.strategy_names
        }
        return {
            "schema_version": 1,
            "dataset_version": DATASET_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "execution_mode": "deterministic_synthetic_no_llm",
            "environment": {
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "platform": platform.platform(),
            },
            "repetitions": repetitions,
            "case_count": len(self.cases),
            "cases": [asdict(case) for case in self.cases],
            "strategies": list(self.strategy_names),
            "aggregates": aggregates,
            "runs": run_rows,
        }

    @staticmethod
    def _score(outcome: StrategyOutcome, case: BenchmarkCase) -> dict[str, Any]:
        expected_refusal = case.expected_root_cause is None
        root_cause_correct = outcome.root_cause == case.expected_root_cause
        success = root_cause_correct and (outcome.safely_refused if expected_refusal else outcome.confidence == "high")
        evidence_types = {evidence.source_type for evidence in outcome.evidence}
        evidence_completeness = (
            len(evidence_types.intersection(case.expected_evidence_types)) / len(case.expected_evidence_types)
            if case.expected_evidence_types
            else 1.0
        )
        selected = set(outcome.tool_names)
        expected = set(case.expected_tools)
        tool_precision = len(selected & expected) / len(selected) if selected else 0.0
        tool_recall = len(selected & expected) / len(expected) if expected else 1.0
        return {
            "strategy": outcome.strategy,
            "case_id": outcome.case_id,
            "task_id": outcome.task_id,
            "status": outcome.status,
            "success": success,
            "root_cause_correct": root_cause_correct,
            "safely_refused": outcome.safely_refused,
            "evidence_completeness": round(evidence_completeness, 4),
            "tool_precision": round(tool_precision, 4),
            "tool_recall": round(tool_recall, 4),
            "tool_attempts": outcome.tool_attempts,
            "latency_ms": round(outcome.latency_ms, 3),
            "recoverable_fault": case.recoverable_fault,
            "expected_refusal": expected_refusal,
            "recovered": outcome.recovered,
            "error": outcome.error,
        }

    @staticmethod
    def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
        recoverable = [row for row in rows if row["recoverable_fault"]]
        latencies = sorted(row["latency_ms"] for row in rows)
        mean = lambda key: round(sum(float(row[key]) for row in rows) / len(rows), 4) if rows else 0.0
        return {
            "run_count": len(rows),
            "task_success_rate": mean("success"),
            "root_cause_accuracy": mean("root_cause_correct"),
            "safe_refusal_rate": round(
                sum(bool(row["safely_refused"]) for row in rows if row["expected_refusal"])
                / max(1, sum(bool(row["expected_refusal"]) for row in rows)),
                4,
            ),
            "evidence_completeness": mean("evidence_completeness"),
            "tool_precision": mean("tool_precision"),
            "tool_recall": mean("tool_recall"),
            "avg_tool_attempts": mean("tool_attempts"),
            "p50_latency_ms": _percentile(latencies, 0.50),
            "p95_latency_ms": _percentile(latencies, 0.95),
            "retry_recovery_rate": round(
                sum(bool(row["recovered"]) for row in recoverable) / len(recoverable), 4
            ) if recoverable else None,
        }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    rank = max(1, int(len(values) * percentile + 0.999999999))
    return round(values[min(rank, len(values)) - 1], 3)


def write_report(report: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
