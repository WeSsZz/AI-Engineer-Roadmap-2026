from __future__ import annotations

import hashlib
import json
import platform
import random
import statistics
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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
class BenchmarkProgress:
    event: str
    run_index: int
    planned_run_count: int
    repetition: int
    repetitions: int
    case_id: str
    strategy: str
    completed_run_count: int
    model_calls: int
    total_tokens: int
    estimated_cost_usd: float
    elapsed_seconds: float
    status: str | None = None
    reason: str | None = None


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
    route: str | None = None
    routing_reason: str | None = None
    repetition: int = 0
    execution_order_position: int = 0


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
        outcome.route = outcome.route or self.name
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


class AdaptiveHarnessStrategy(_DirectStrategy):
    name = "adaptive_harness"

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
            name="strategy.adaptive_harness",
            kind=SpanKind.RUNTIME,
        ) as root:
            try:
                with tracer.start_span(
                    trace_id=task_id,
                    task_id=task_id,
                    parent_span_id=root.span_id,
                    name="agent.risk_router",
                    kind=SpanKind.AGENT,
                ) as router:
                    metrics = self._call(
                        runtime,
                        task_id,
                        router.span_id,
                        "query_service_metrics",
                        {"service": case.service, "window_minutes": 15},
                    )
                    anomalous = bool(
                        metrics.data["error_rate"] >= 0.05
                        and metrics.data["db_pool_utilization"] >= 0.90
                    )
                    write_intent = "工单" in case.objective or "ticket" in case.objective.lower()
                    risk_score = int(anomalous) * 2 + int(write_intent)
                    router.set_attribute("anomalous", anomalous)
                    router.set_attribute("write_intent", write_intent)
                    router.set_attribute("risk_score", risk_score)
            except ToolError as exc:
                outcome = self._outcome(case, task_id, [], None)
                outcome.route = "single_agent_triage"
                outcome.routing_reason = "metrics_unavailable"
                outcome.error = f"{type(exc).__name__}: {exc}"
                return outcome

            if not anomalous:
                root.set_attribute("selected_route", "single_agent_triage")
                outcome = self._outcome(case, task_id, list(metrics.evidence), None)
                outcome.route = "single_agent_triage"
                outcome.routing_reason = "metrics_below_incident_threshold"
                return outcome

            root.set_attribute("selected_route", "verified_multi_agent")
            routing_reason = (
                "high_risk_anomaly_with_write_intent"
                if write_intent
                else "anomaly_requires_evidence_verification"
            )
            outputs: dict[str, ToolOutput] = {}
            errors: list[str] = []

            def investigate(
                agent_name: str,
                tool_name: str,
                arguments: dict[str, Any],
            ) -> ToolOutput:
                with tracer.start_span(
                    trace_id=task_id,
                    task_id=task_id,
                    parent_span_id=root.span_id,
                    name=f"agent.{agent_name}",
                    kind=SpanKind.AGENT,
                ) as specialist:
                    return self._call(
                        runtime,
                        task_id,
                        specialist.span_id,
                        tool_name,
                        arguments,
                    )

            investigations = {
                "logs": (
                    "log_investigator",
                    "search_logs",
                    {"service": case.service, "severity": "error", "limit": 20},
                ),
                "runbook": (
                    "runbook_researcher",
                    "get_runbook",
                    {"service": case.service, "symptom": "database_pool_exhaustion"},
                ),
            }
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {
                    executor.submit(investigate, *arguments): key
                    for key, arguments in investigations.items()
                }
                for future in as_completed(futures):
                    key = futures[future]
                    try:
                        outputs[key] = future.result()
                    except ToolError as exc:
                        errors.append(f"{type(exc).__name__}: {exc}")

            evidence = list(metrics.evidence)
            for key in ("logs", "runbook"):
                if key in outputs:
                    evidence.extend(outputs[key].evidence)
            if errors:
                outcome = self._outcome(case, task_id, evidence, None)
                outcome.route = "verified_multi_agent"
                outcome.routing_reason = routing_reason
                outcome.error = "; ".join(errors)
                return outcome

            logs = outputs["logs"]
            runbook = outputs["runbook"]
            correlated = bool(
                any(entry["code"] == "DB_POOL_TIMEOUT" for entry in logs.data["entries"])
                and runbook.data["runbook"]
            )
            with tracer.start_span(
                trace_id=task_id,
                task_id=task_id,
                parent_span_id=root.span_id,
                name="agent.verifier",
                kind=SpanKind.AGENT,
                attributes={"correlated": correlated, "evidence_count": len(evidence)},
            ):
                pass
            if correlated and provider is not None:
                with tracer.start_span(
                    trace_id=task_id,
                    task_id=task_id,
                    parent_span_id=root.span_id,
                    name="agent.incident_synthesis_agent",
                    kind=SpanKind.AGENT,
                ) as synthesis:
                    outcome = self._model_outcome(
                        case,
                        task_id,
                        synthesis.span_id,
                        provider,
                        metrics,
                        logs,
                        runbook,
                    )
            else:
                outcome = self._outcome(
                    case,
                    task_id,
                    evidence,
                    "database_pool_exhaustion" if correlated else None,
                )
            outcome.route = "verified_multi_agent"
            outcome.routing_reason = routing_reason
            if outcome.root_cause is not None and write_intent:
                outcome.status = "needs_approval"
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
            route="fixed_multi_agent",
            routing_reason="strategy_forced",
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
    "adaptive_harness": AdaptiveHarnessStrategy,
}


class BenchmarkRunner:
    def __init__(
        self,
        cases: tuple[BenchmarkCase, ...] | None = None,
        strategy_names: tuple[str, ...] | None = None,
        model_config: ModelBenchmarkConfig | None = None,
        budget: BenchmarkBudget | None = None,
        execution_order: str = "counterbalanced",
    ) -> None:
        self.cases = cases or default_cases()
        self.strategy_names = strategy_names or tuple(STRATEGIES)
        self.model_config = model_config
        self.budget = budget or BenchmarkBudget()
        self.execution_order = execution_order
        unknown = sorted(set(self.strategy_names) - set(STRATEGIES))
        if unknown:
            raise ValueError(f"Unknown benchmark strategies: {', '.join(unknown)}")
        if execution_order not in {"fixed", "counterbalanced"}:
            raise ValueError("execution_order must be 'fixed' or 'counterbalanced'")

    def run(
        self,
        *,
        repetitions: int = 1,
        workspace: str | Path | None = None,
        progress_callback: Callable[[BenchmarkProgress], None] | None = None,
    ) -> dict[str, Any]:
        if repetitions <= 0:
            raise ValueError("repetitions must be positive")
        owns_workspace = workspace is None
        temporary = tempfile.TemporaryDirectory() if owns_workspace else None
        root = Path(temporary.name if temporary else workspace)  # type: ignore[arg-type, union-attr]
        root.mkdir(parents=True, exist_ok=True)
        outcomes: list[StrategyOutcome] = []
        stop_reason: str | None = None
        planned_run_count = repetitions * len(self.cases) * len(self.strategy_names)
        benchmark_started = time.monotonic()
        current = (0, "", "", 0)
        try:
            for repetition in range(repetitions):
                for case_index, case in enumerate(self.cases):
                    ordered_strategies = self._strategy_order(repetition, case_index)
                    skip_reason: str | None = None
                    if self.model_config is not None and case.expected_root_cause is not None:
                        skip_reason = self._budget_stop_reason(outcomes)
                        remaining_calls = self.budget.max_model_calls - sum(item.model_calls for item in outcomes)
                        calls_needed = len(self.strategy_names)
                        if remaining_calls < calls_needed:
                            skip_reason = skip_reason or "max_model_calls"
                    if skip_reason:
                        stop_reason = stop_reason or skip_reason
                        for strategy_index, strategy_name in enumerate(ordered_strategies):
                            run_index = self._run_index(repetition, case_index, strategy_index)
                            self._emit_progress(
                                progress_callback,
                                "skipped",
                                run_index,
                                planned_run_count,
                                repetition,
                                repetitions,
                                case.case_id,
                                strategy_name,
                                outcomes,
                                benchmark_started,
                                reason=skip_reason,
                            )
                        continue
                    for strategy_index, strategy_name in enumerate(ordered_strategies):
                        run_index = self._run_index(repetition, case_index, strategy_index)
                        current = (repetition, case.case_id, strategy_name, run_index)
                        self._emit_progress(
                            progress_callback,
                            "started",
                            run_index,
                            planned_run_count,
                            repetition,
                            repetitions,
                            case.case_id,
                            strategy_name,
                            outcomes,
                            benchmark_started,
                        )
                        run_directory = root / f"r{repetition}-{case.case_id}-{strategy_name}"
                        run_directory.mkdir(parents=True, exist_ok=True)
                        outcome = STRATEGIES[strategy_name](self.model_config).run(case, run_directory)
                        outcome.repetition = repetition + 1
                        outcome.execution_order_position = strategy_index + 1
                        outcomes.append(outcome)
                        self._emit_progress(
                            progress_callback,
                            "completed",
                            run_index,
                            planned_run_count,
                            repetition,
                            repetitions,
                            case.case_id,
                            strategy_name,
                            outcomes,
                            benchmark_started,
                            status=outcome.status,
                        )
                    stop_reason = stop_reason or self._budget_stop_reason(outcomes)
        except KeyboardInterrupt:
            stop_reason = "user_interrupt"
            repetition, case_id, strategy_name, run_index = current
            self._emit_progress(
                progress_callback,
                "interrupted",
                run_index,
                planned_run_count,
                repetition,
                repetitions,
                case_id,
                strategy_name,
                outcomes,
                benchmark_started,
                reason=stop_reason,
            )
        finally:
            if temporary is not None:
                temporary.cleanup()
        return self._report(outcomes, repetitions, stop_reason)

    def _strategy_order(self, repetition: int, case_index: int) -> tuple[str, ...]:
        if self.execution_order == "fixed" or len(self.strategy_names) < 2:
            return self.strategy_names
        offset = (repetition * len(self.cases) + case_index) % len(self.strategy_names)
        return self.strategy_names[offset:] + self.strategy_names[:offset]

    def _run_index(self, repetition: int, case_index: int, strategy_index: int) -> int:
        runs_per_repetition = len(self.cases) * len(self.strategy_names)
        return (
            repetition * runs_per_repetition
            + case_index * len(self.strategy_names)
            + strategy_index
            + 1
        )

    @staticmethod
    def _emit_progress(
        callback: Callable[[BenchmarkProgress], None] | None,
        event: str,
        run_index: int,
        planned_run_count: int,
        repetition: int,
        repetitions: int,
        case_id: str,
        strategy: str,
        outcomes: list[StrategyOutcome],
        benchmark_started: float,
        *,
        status: str | None = None,
        reason: str | None = None,
    ) -> None:
        if callback is None:
            return
        callback(
            BenchmarkProgress(
                event=event,
                run_index=run_index,
                planned_run_count=planned_run_count,
                repetition=repetition + 1,
                repetitions=repetitions,
                case_id=case_id,
                strategy=strategy,
                completed_run_count=len(outcomes),
                model_calls=sum(item.model_calls for item in outcomes),
                total_tokens=sum(item.total_tokens for item in outcomes),
                estimated_cost_usd=sum(item.estimated_cost_usd for item in outcomes),
                elapsed_seconds=time.monotonic() - benchmark_started,
                status=status,
                reason=reason,
            )
        )

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
        paired_comparisons = self._paired_comparisons(run_rows)
        return {
            "schema_version": 2,
            "benchmark_version": "2.1",
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
            "execution_order": self.execution_order,
            "model": self._model_report_config(),
            "budget": {
                **asdict(self.budget),
                "stopped_early": stop_reason is not None,
                "stop_reason": stop_reason,
            },
            "planned_run_count": planned_run_count,
            "completed_run_count": len(run_rows),
            "aggregates": aggregates,
            "paired_comparisons": paired_comparisons,
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
        if success and expected_refusal:
            outcome_class = "safe_refusal"
            normalized_status = "safely_refused"
        elif success:
            outcome_class = "success"
            normalized_status = "completed"
        elif outcome.model_schema_compliant is False:
            outcome_class = "model_contract_failure"
            normalized_status = "failed"
        else:
            outcome_class = "execution_failure"
            normalized_status = "failed"
        return {
            "strategy": outcome.strategy,
            "case_id": outcome.case_id,
            "task_id": outcome.task_id,
            "repetition": outcome.repetition,
            "execution_order_position": outcome.execution_order_position,
            "status": outcome.status,
            "normalized_status": normalized_status,
            "outcome_class": outcome_class,
            "route": outcome.route,
            "routing_reason": outcome.routing_reason,
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
        route_distribution = {
            route: sum(row["route"] == route for row in rows)
            for route in sorted({row["route"] for row in rows if row["route"] is not None})
        }
        outcome_distribution = {
            outcome_class: sum(row["outcome_class"] == outcome_class for row in rows)
            for outcome_class in sorted({row["outcome_class"] for row in rows})
        }
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
            "route_distribution": route_distribution,
            "outcome_distribution": outcome_distribution,
        }

    def _paired_comparisons(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if "single_agent" not in self.strategy_names:
            return {}
        comparisons = {}
        for candidate in self.strategy_names:
            if candidate == "single_agent":
                continue
            baseline_rows = {
                (row["case_id"], row["repetition"]): row
                for row in rows
                if row["strategy"] == "single_agent"
            }
            candidate_rows = {
                (row["case_id"], row["repetition"]): row
                for row in rows
                if row["strategy"] == candidate
            }
            keys = sorted(set(baseline_rows).intersection(candidate_rows))
            pairs = [(baseline_rows[key], candidate_rows[key]) for key in keys]
            model_pairs = [pair for pair in pairs if pair[0]["model_calls"] or pair[1]["model_calls"]]
            comparisons[f"{candidate}_vs_single_agent"] = {
                "all_cases": self._paired_statistics(pairs),
                "model_cases": self._paired_statistics(model_pairs),
            }
        return comparisons

    @staticmethod
    def _paired_statistics(
        pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> dict[str, Any]:
        latency_deltas = [candidate["latency_ms"] - baseline["latency_ms"] for baseline, candidate in pairs]
        tool_deltas = [candidate["tool_attempts"] - baseline["tool_attempts"] for baseline, candidate in pairs]
        token_deltas = [candidate["total_tokens"] - baseline["total_tokens"] for baseline, candidate in pairs]
        cost_deltas = [
            candidate["estimated_cost_usd"] - baseline["estimated_cost_usd"]
            for baseline, candidate in pairs
        ]
        success_deltas = [int(candidate["success"]) - int(baseline["success"]) for baseline, candidate in pairs]
        return {
            "pair_count": len(pairs),
            "mean_success_delta": _mean(success_deltas),
            "mean_latency_delta_ms": _mean(latency_deltas, digits=3),
            "median_latency_delta_ms": round(statistics.median(latency_deltas), 3) if latency_deltas else None,
            "bootstrap_95_ci_mean_latency_delta_ms": _bootstrap_mean_ci(latency_deltas),
            "mean_tool_attempt_delta": _mean(tool_deltas),
            "mean_total_token_delta": _mean(token_deltas),
            "mean_estimated_cost_delta_usd": _mean(cost_deltas, digits=9),
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


def _mean(values: list[float | int], *, digits: int = 4) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), digits)


def _bootstrap_mean_ci(values: list[float], *, samples: int = 2_000) -> list[float] | None:
    if not values:
        return None
    if len(values) == 1:
        value = round(values[0], 3)
        return [value, value]
    generator = random.Random(20260810)
    means = sorted(
        sum(generator.choice(values) for _ in values) / len(values)
        for _ in range(samples)
    )
    lower = means[int((samples - 1) * 0.025)]
    upper = means[int((samples - 1) * 0.975)]
    return [round(lower, 3), round(upper, 3)]


def write_report(report: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
