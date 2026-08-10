from __future__ import annotations

import json
import hashlib
import platform
import statistics
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from .agents import (
    INCIDENT_SYNTHESIS_PROMPT_VERSION,
    INCIDENT_SYNTHESIS_SYSTEM_PROMPT,
    IncidentSynthesisAgent,
)
from .context import ContextEngine
from .faults import FaultInjector, FaultRule
from .harness import EnterpriseAgentHarness
from .llm import DeepSeekProvider, LLMCallRecord, LLMConfig, LLMProvider
from .models import AgentResult, Evidence, SpanKind, TaskEnvelope, TaskState
from .store import SQLiteEventStore
from .tool_gateway import ToolError, ToolOutput, ToolRuntime
from .tools import build_demo_runtime
from .tracing import SQLiteTraceRecorder


DATASET_VERSION = "incident-diagnosis-v1"
INCIDENT_TOOLS = ("query_service_metrics", "search_logs", "get_runbook", "create_ticket")
PRICING_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing"


@dataclass(frozen=True)
class ModelPricing:
    model: str
    input_cache_hit_per_million_usd: float
    input_cache_miss_per_million_usd: float
    output_per_million_usd: float
    effective_date: str = "2026-08-10"
    source: str = PRICING_SOURCE

    def estimate(self, records: tuple[LLMCallRecord, ...]) -> float:
        hit = sum(record.usage.prompt_cache_hit_tokens for record in records)
        miss = sum(record.usage.prompt_cache_miss_tokens for record in records)
        output = sum(record.usage.completion_tokens for record in records)
        return (
            hit * self.input_cache_hit_per_million_usd
            + miss * self.input_cache_miss_per_million_usd
            + output * self.output_per_million_usd
        ) / 1_000_000


DEEPSEEK_PRICING = {
    "deepseek-v4-flash": ModelPricing("deepseek-v4-flash", 0.0028, 0.14, 0.28),
    "deepseek-v4-pro": ModelPricing("deepseek-v4-pro", 0.003625, 0.435, 0.87),
}


@dataclass(frozen=True)
class ModelBenchmarkConfig:
    provider_factory: Callable[[], LLMProvider] = field(repr=False, compare=False)
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    thinking: str = "disabled"
    max_tokens: int = 1200
    prompt_version: str = INCIDENT_SYNTHESIS_PROMPT_VERSION
    pricing: ModelPricing = field(default_factory=lambda: DEEPSEEK_PRICING["deepseek-v4-flash"])

    @classmethod
    def deepseek(cls, config: LLMConfig) -> ModelBenchmarkConfig:
        return cls(
            provider_factory=lambda: DeepSeekProvider(config),
            provider="deepseek",
            model=config.model,
            thinking=config.thinking,
            max_tokens=config.max_tokens,
            pricing=DEEPSEEK_PRICING[config.model],
        )

    @property
    def prompt_fingerprint(self) -> str:
        return hashlib.sha256(INCIDENT_SYNTHESIS_SYSTEM_PROMPT.encode("utf-8")).hexdigest()

    @property
    def config_fingerprint(self) -> str:
        raw = json.dumps(
            {
                "provider": self.provider,
                "model": self.model,
                "thinking": self.thinking,
                "max_tokens": self.max_tokens,
                "prompt_version": self.prompt_version,
            },
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BenchmarkBudget:
    max_model_calls: int = 16
    max_total_tokens: int = 50_000
    max_estimated_cost_usd: float = 0.10

    def __post_init__(self) -> None:
        if self.max_model_calls <= 0 or self.max_total_tokens <= 0 or self.max_estimated_cost_usd <= 0:
            raise ValueError("Benchmark model budgets must be positive")


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
    model_calls: int = 0
    model_attempts: int = 0
    model_schema_compliant: bool | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    model_latency_ms: float = 0.0
    estimated_cost_usd: float = 0.0


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

    def __init__(self, model_config: ModelBenchmarkConfig | None = None) -> None:
        self.model_config = model_config

    def run(self, case: BenchmarkCase, workspace: Path) -> StrategyOutcome:
        task_id = f"bench-{self.name}-{case.case_id}-{uuid.uuid4().hex[:6]}"
        store = SQLiteEventStore(workspace / "trace.db")
        tracer = SQLiteTraceRecorder(store)
        provider = self.model_config.provider_factory() if self.model_config else None
        if provider is not None:
            provider.set_record_sink(tracer.record_model_call)
        runtime = build_demo_runtime(
            workspace / "tools.db",
            tracer.record_tool_call,
            fault_injector=FaultInjector(case.fault_rules),
        )
        started = time.monotonic()
        try:
            outcome = self._execute(case, task_id, runtime, tracer, provider)
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
        self._populate_model_metrics(outcome, provider)
        return outcome

    def _execute(
        self,
        case: BenchmarkCase,
        task_id: str,
        runtime: ToolRuntime,
        tracer: SQLiteTraceRecorder,
        provider: LLMProvider | None,
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
        provider: LLMProvider | None,
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
            root_cause = "database_pool_exhaustion" if correlated else None
            if correlated and provider is not None:
                return self._model_outcome(
                    case,
                    task_id,
                    parent_span_id,
                    provider,
                    metrics,
                    logs,
                    runbook,
                )
            return self._outcome(case, task_id, evidence, root_cause)
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

    def _model_outcome(
        self,
        case: BenchmarkCase,
        task_id: str,
        parent_span_id: str,
        provider: LLMProvider,
        metrics: ToolOutput,
        logs: ToolOutput,
        runbook: ToolOutput,
    ) -> StrategyOutcome:
        evidence = [*metrics.evidence, *logs.evidence, *runbook.evidence]
        task = TaskEnvelope(
            task_id=task_id,
            objective=case.objective,
            allowed_tools=INCIDENT_TOOLS,
            metadata={"scenario": "incident", "llm_provider": provider.name, "llm_model": provider.model},
        )
        state = TaskState(task_id)
        results = [
            AgentResult(
                "metrics_investigator",
                "completed",
                "benchmark metrics",
                data=dict(metrics.data),
                evidence=list(metrics.evidence),
            ),
            AgentResult(
                "log_investigator",
                "completed",
                "benchmark logs",
                data={**logs.data, "codes": [entry["code"] for entry in logs.data["entries"]]},
                evidence=list(logs.evidence),
            ),
            AgentResult(
                "runbook_researcher",
                "completed",
                "benchmark runbook",
                data=dict(runbook.data),
                evidence=list(runbook.evidence),
            ),
            AgentResult(
                "verifier",
                "completed",
                "benchmark evidence verified",
                data={"evidence_count": len(evidence), "failed_agents": []},
                evidence=evidence,
            ),
        ]
        context = ContextEngine().build(task, state, [])
        synthesis = IncidentSynthesisAgent(provider).run(
            task,
            state,
            context,
            results,
            trace_parent_id=parent_span_id,
        )
        outcome = self._outcome(
            case,
            task_id,
            evidence,
            "database_pool_exhaustion" if synthesis.status == "completed" else None,
        )
        outcome.model_schema_compliant = synthesis.status == "completed"
        if synthesis.data.get("model_error"):
            outcome.error = synthesis.data["model_error"]
        return outcome

    def _populate_model_metrics(self, outcome: StrategyOutcome, provider: LLMProvider | None) -> None:
        if provider is None:
            return
        records = tuple(getattr(provider, "invocations", ()))
        call_ids = {record.call_id for record in records}
        outcome.model_calls = len(call_ids)
        outcome.model_attempts = len(records)
        outcome.prompt_tokens = sum(record.usage.prompt_tokens for record in records)
        outcome.completion_tokens = sum(record.usage.completion_tokens for record in records)
        outcome.total_tokens = sum(record.usage.total_tokens for record in records)
        outcome.prompt_cache_hit_tokens = sum(record.usage.prompt_cache_hit_tokens for record in records)
        outcome.prompt_cache_miss_tokens = sum(record.usage.prompt_cache_miss_tokens for record in records)
        outcome.model_latency_ms = sum(record.duration_ms for record in records)
        if outcome.model_schema_compliant is None and records:
            outcome.model_schema_compliant = not bool(outcome.error)
        if self.model_config is not None:
            outcome.estimated_cost_usd = self.model_config.pricing.estimate(records)


class FixedWorkflowStrategy(_DirectStrategy):
    name = "fixed_workflow"

    def _execute(
        self,
        case: BenchmarkCase,
        task_id: str,
        runtime: ToolRuntime,
        tracer: SQLiteTraceRecorder,
        provider: LLMProvider | None,
    ) -> StrategyOutcome:
        with tracer.start_span(
            trace_id=task_id,
            task_id=task_id,
            name="strategy.fixed_workflow",
            kind=SpanKind.RUNTIME,
        ) as span:
            return self._execute_tools(case, task_id, runtime, span.span_id, adaptive=False, provider=provider)


class SingleAgentStrategy(_DirectStrategy):
    name = "single_agent"

    def _execute(
        self,
        case: BenchmarkCase,
        task_id: str,
        runtime: ToolRuntime,
        tracer: SQLiteTraceRecorder,
        provider: LLMProvider | None,
    ) -> StrategyOutcome:
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
                outcome = self._execute_tools(
                    case, task_id, runtime, agent.span_id, adaptive=True, provider=provider
                )
                agent.set_attribute("result_status", outcome.status)
                return outcome


class PlanExecutorStrategy(_DirectStrategy):
    name = "plan_executor"

    def _execute(
        self,
        case: BenchmarkCase,
        task_id: str,
        runtime: ToolRuntime,
        tracer: SQLiteTraceRecorder,
        provider: LLMProvider | None,
    ) -> StrategyOutcome:
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
                outcome = self._execute_tools(
                    case, task_id, runtime, executor.span_id, adaptive=True, provider=provider
                )
                executor.set_attribute("result_status", outcome.status)
                return outcome


class MultiAgentStrategy:
    name = "multi_agent"

    def __init__(self, model_config: ModelBenchmarkConfig | None = None) -> None:
        self.model_config = model_config

    def run(self, case: BenchmarkCase, workspace: Path) -> StrategyOutcome:
        store = SQLiteEventStore(workspace / "agent.db")
        provider = self.model_config.provider_factory() if self.model_config else None
        harness = EnterpriseAgentHarness(
            store=store,
            fault_injector=FaultInjector(case.fault_rules),
            llm_provider=provider,
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
        outcome = StrategyOutcome(
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
            model_schema_compliant=(
                None
                if result.state.artifacts.get("model_call") is None and result.state.artifacts.get("model_error") is None
                else result.state.artifacts.get("model_error") is None
            ),
        )
        if provider is not None:
            records = tuple(getattr(provider, "invocations", ()))
            outcome.model_calls = len({record.call_id for record in records})
            outcome.model_attempts = len(records)
            outcome.prompt_tokens = sum(record.usage.prompt_tokens for record in records)
            outcome.completion_tokens = sum(record.usage.completion_tokens for record in records)
            outcome.total_tokens = sum(record.usage.total_tokens for record in records)
            outcome.prompt_cache_hit_tokens = sum(record.usage.prompt_cache_hit_tokens for record in records)
            outcome.prompt_cache_miss_tokens = sum(record.usage.prompt_cache_miss_tokens for record in records)
            outcome.model_latency_ms = sum(record.duration_ms for record in records)
            if self.model_config is not None:
                outcome.estimated_cost_usd = self.model_config.pricing.estimate(records)
            if result.state.artifacts.get("model_error"):
                outcome.error = result.state.artifacts["model_error"]
        return outcome


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
        model_config: ModelBenchmarkConfig | None = None,
        budget: BenchmarkBudget | None = None,
    ) -> None:
        self.cases = cases or default_cases()
        self.strategy_names = strategy_names or tuple(STRATEGIES)
        self.model_config = model_config
        self.budget = budget or BenchmarkBudget()
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
        stop_reason: str | None = None
        try:
            for repetition in range(repetitions):
                for case in self.cases:
                    if self.model_config is not None:
                        remaining_calls = self.budget.max_model_calls - sum(item.model_calls for item in outcomes)
                        calls_needed = len(self.strategy_names) if case.expected_root_cause is not None else 0
                        if remaining_calls < calls_needed:
                            stop_reason = "max_model_calls"
                            break
                    for strategy_name in self.strategy_names:
                        run_directory = root / f"r{repetition}-{case.case_id}-{strategy_name}"
                        run_directory.mkdir(parents=True, exist_ok=True)
                        outcomes.append(STRATEGIES[strategy_name](self.model_config).run(case, run_directory))
                    stop_reason = self._budget_stop_reason(outcomes)
                    if stop_reason:
                        break
                if stop_reason:
                    break
        finally:
            if temporary is not None:
                temporary.cleanup()
        return self._report(outcomes, repetitions, stop_reason)

    def _report(
        self,
        outcomes: list[StrategyOutcome],
        repetitions: int,
        stop_reason: str | None,
    ) -> dict[str, Any]:
        case_map = {case.case_id: case for case in self.cases}
        run_rows = [self._score(outcome, case_map[outcome.case_id]) for outcome in outcomes]
        planned_run_count = repetitions * len(self.cases) * len(self.strategy_names)
        if len(run_rows) == planned_run_count:
            stop_reason = None
        aggregates = {
            strategy: self._aggregate([row for row in run_rows if row["strategy"] == strategy])
            for strategy in self.strategy_names
        }
        return {
            "schema_version": 2,
            "dataset_version": DATASET_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "execution_mode": "model_backed" if self.model_config else "deterministic_synthetic_no_llm",
            "environment": {
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "platform": platform.platform(),
            },
            "repetitions": repetitions,
            "case_count": len(self.cases),
            "cases": [asdict(case) for case in self.cases],
            "strategies": list(self.strategy_names),
            "model": self._model_report_config(),
            "budget": {
                **asdict(self.budget),
                "stopped_early": stop_reason is not None,
                "stop_reason": stop_reason,
            },
            "planned_run_count": planned_run_count,
            "completed_run_count": len(run_rows),
            "aggregates": aggregates,
            "runs": run_rows,
        }

    def _budget_stop_reason(self, outcomes: list[StrategyOutcome]) -> str | None:
        if self.model_config is None:
            return None
        if sum(item.model_calls for item in outcomes) >= self.budget.max_model_calls:
            return "max_model_calls"
        if sum(item.total_tokens for item in outcomes) >= self.budget.max_total_tokens:
            return "max_total_tokens"
        if sum(item.estimated_cost_usd for item in outcomes) >= self.budget.max_estimated_cost_usd:
            return "max_estimated_cost_usd"
        return None

    def _model_report_config(self) -> dict[str, Any] | None:
        if self.model_config is None:
            return None
        return {
            "provider": self.model_config.provider,
            "model": self.model_config.model,
            "thinking": self.model_config.thinking,
            "max_tokens": self.model_config.max_tokens,
            "prompt_version": self.model_config.prompt_version,
            "prompt_fingerprint_sha256": self.model_config.prompt_fingerprint,
            "config_fingerprint_sha256": self.model_config.config_fingerprint,
            "pricing": asdict(self.model_config.pricing),
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
            "predicted_root_cause": outcome.root_cause,
            "confidence": outcome.confidence,
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
            "model_calls": outcome.model_calls,
            "model_attempts": outcome.model_attempts,
            "model_schema_compliant": outcome.model_schema_compliant,
            "prompt_tokens": outcome.prompt_tokens,
            "completion_tokens": outcome.completion_tokens,
            "total_tokens": outcome.total_tokens,
            "prompt_cache_hit_tokens": outcome.prompt_cache_hit_tokens,
            "prompt_cache_miss_tokens": outcome.prompt_cache_miss_tokens,
            "model_latency_ms": round(outcome.model_latency_ms, 3),
            "estimated_cost_usd": round(outcome.estimated_cost_usd, 9),
            "error": outcome.error,
        }

    @staticmethod
    def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
        recoverable = [row for row in rows if row["recoverable_fault"]]
        latencies = sorted(row["latency_ms"] for row in rows)
        model_rows = [row for row in rows if row["model_calls"] > 0]
        schema_rows = [row for row in rows if row["model_schema_compliant"] is not None]
        cache_tokens = sum(row["prompt_cache_hit_tokens"] + row["prompt_cache_miss_tokens"] for row in rows)
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
            "latency_stddev_ms": round(statistics.pstdev(latencies), 3) if len(latencies) > 1 else 0.0,
            "retry_recovery_rate": round(
                sum(bool(row["recovered"]) for row in recoverable) / len(recoverable), 4
            ) if recoverable else None,
            "model_call_count": sum(row["model_calls"] for row in rows),
            "model_attempt_count": sum(row["model_attempts"] for row in rows),
            "model_schema_compliance_rate": round(
                sum(bool(row["model_schema_compliant"]) for row in schema_rows) / len(schema_rows), 4
            ) if schema_rows else None,
            "prompt_tokens": sum(row["prompt_tokens"] for row in rows),
            "completion_tokens": sum(row["completion_tokens"] for row in rows),
            "total_tokens": sum(row["total_tokens"] for row in rows),
            "prompt_cache_hit_rate": round(
                sum(row["prompt_cache_hit_tokens"] for row in rows) / cache_tokens, 4
            ) if cache_tokens else None,
            "avg_model_latency_ms": round(
                sum(row["model_latency_ms"] for row in model_rows) / len(model_rows), 3
            ) if model_rows else None,
            "model_latency_stddev_ms": round(
                statistics.pstdev([row["model_latency_ms"] for row in model_rows]), 3
            ) if len(model_rows) > 1 else 0.0 if model_rows else None,
            "estimated_cost_usd": round(sum(row["estimated_cost_usd"] for row in rows), 9),
            "prediction_consistency_rate": BenchmarkRunner._prediction_consistency(rows),
        }

    @staticmethod
    def _prediction_consistency(rows: list[dict[str, Any]]) -> float | None:
        if not rows:
            return None
        by_case: dict[str, list[str | None]] = {}
        for row in rows:
            by_case.setdefault(row["case_id"], []).append(row["predicted_root_cause"])
        scores = []
        for predictions in by_case.values():
            counts = {value: predictions.count(value) for value in set(predictions)}
            scores.append(max(counts.values()) / len(predictions))
        return round(sum(scores) / len(scores), 4)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    rank = max(1, int(len(values) * percentile + 0.999999999))
    return round(values[min(rank, len(values)) - 1], 3)


def write_report(report: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
