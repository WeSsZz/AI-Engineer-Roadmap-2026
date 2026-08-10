from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from .context import ContextSnapshot
from .llm import LLMError, LLMProvider, LLMResponseError
from .models import AgentResult, Evidence, TaskEnvelope, TaskState
from .tools import ToolError, ToolRuntime


INCIDENT_SYNTHESIS_PROMPT_VERSION = "incident-synthesis-v1"
INCIDENT_SYNTHESIS_SYSTEM_PROMPT = (
    "You are an enterprise incident synthesis agent. Return only a JSON object. "
    "Never invent evidence or claim an action was executed. Required JSON schema: "
    '{"root_cause":"database_pool_exhaustion","confidence":"high",'
    '"answer":"string","recommendation":"string","evidence_refs":["string"]}.'
)


def _tool_failure(agent_name: str, tool_name: str, error: ToolError) -> AgentResult:
    return AgentResult(
        agent_name=agent_name,
        status="failed",
        summary=f"{tool_name} 数据源不可用",
        data={"failed_tool": tool_name, "error_type": type(error).__name__},
        warnings=[str(error)],
    )


def incident_service(task: TaskEnvelope) -> str:
    configured = task.metadata.get("service")
    if isinstance(configured, str) and configured:
        return configured
    match = re.search(r"\b[a-z0-9]+(?:-[a-z0-9]+)*-api\b", task.objective.lower())
    return match.group(0) if match else "payment-api"


class MetricsInvestigator:
    name = "metrics_investigator"

    def run(
        self,
        task: TaskEnvelope,
        state: TaskState,
        context: ContextSnapshot,
        runtime: ToolRuntime,
        trace_parent_id: str | None = None,
    ) -> AgentResult:
        service = incident_service(task)
        try:
            result = runtime.invoke(
                "query_service_metrics",
                allowed_tools=task.allowed_tools,
                arguments={"service": service, "window_minutes": 15},
                task_id=task.task_id,
                trace_id=task.task_id,
                parent_span_id=trace_parent_id,
            )
        except ToolError as error:
            return _tool_failure(self.name, "query_service_metrics", error)
        anomalous = result.data["error_rate"] >= 0.05 or result.data["p95_latency_ms"] >= 1000
        facts = [
            f"{service} 15 分钟错误率 {result.data['error_rate']:.1%}，基线 {result.data['baseline_error_rate']:.1%}",
            f"P95 延迟 {result.data['p95_latency_ms']}ms，连接池利用率 {result.data['db_pool_utilization']:.1%}",
        ]
        return AgentResult(
            self.name,
            "completed" if anomalous else "failed",
            "检测到服务黄金信号异常" if anomalous else "未检测到显著指标异常",
            data={**result.data, "facts": facts, "tool_call": result.metadata, "anomalous": anomalous},
            evidence=list(result.evidence),
            warnings=[] if anomalous else ["指标未达到事故阈值"],
        )


class LogInvestigator:
    name = "log_investigator"

    def run(
        self,
        task: TaskEnvelope,
        state: TaskState,
        context: ContextSnapshot,
        runtime: ToolRuntime,
        trace_parent_id: str | None = None,
    ) -> AgentResult:
        service = incident_service(task)
        try:
            result = runtime.invoke(
                "search_logs",
                allowed_tools=task.allowed_tools,
                arguments={"service": service, "severity": "error", "limit": 20},
                task_id=task.task_id,
                trace_id=task.task_id,
                parent_span_id=trace_parent_id,
            )
        except ToolError as error:
            return _tool_failure(self.name, "search_logs", error)
        entries = result.data["entries"]
        codes = [entry["code"] for entry in entries]
        facts = [f"{service} 高频错误 {entry['code']} 出现 {entry['count']} 次" for entry in entries]
        return AgentResult(
            self.name,
            "completed" if entries else "failed",
            "日志显示数据库连接超时与 HTTP 503 同时出现" if entries else "没有匹配的错误日志",
            data={**result.data, "codes": codes, "facts": facts, "tool_call": result.metadata},
            evidence=list(result.evidence),
            warnings=[] if entries else ["日志证据为空"],
        )


class RunbookResearcher:
    name = "runbook_researcher"

    def run(
        self,
        task: TaskEnvelope,
        state: TaskState,
        context: ContextSnapshot,
        runtime: ToolRuntime,
        trace_parent_id: str | None = None,
    ) -> AgentResult:
        service = incident_service(task)
        try:
            result = runtime.invoke(
                "get_runbook",
                allowed_tools=task.allowed_tools,
                arguments={"service": service, "symptom": "database_pool_exhaustion"},
                task_id=task.task_id,
                trace_id=task.task_id,
                parent_span_id=trace_parent_id,
            )
        except ToolError as error:
            return _tool_failure(self.name, "get_runbook", error)
        runbook = result.data["runbook"]
        return AgentResult(
            self.name,
            "completed" if runbook else "failed",
            "找到已审批的数据库连接池处置手册" if runbook else "没有匹配的处置手册",
            data={**result.data, "facts": [f"适用 Runbook：{runbook['title']}"] if runbook else [], "tool_call": result.metadata},
            evidence=list(result.evidence),
            warnings=[] if runbook else ["不得在缺少 Runbook 时自动执行处置"],
        )


class KnowledgeResearcher:
    name = "knowledge_researcher"

    def run(
        self,
        task: TaskEnvelope,
        state: TaskState,
        context: ContextSnapshot,
        runtime: ToolRuntime,
        trace_parent_id: str | None = None,
    ) -> AgentResult:
        result = runtime.invoke(
            "search_docs",
            allowed_tools=task.allowed_tools,
            arguments={"query": task.objective},
            task_id=task.task_id,
            trace_id=task.task_id,
            parent_span_id=trace_parent_id,
        )
        return AgentResult(
            agent_name=self.name,
            status="completed" if result.data["count"] else "failed",
            summary="检索到相关企业文档" if result.data["count"] else "未找到相关企业文档",
            data={**result.data, "tool_call": result.metadata},
            evidence=list(result.evidence),
            warnings=[] if result.data["count"] else ["需要用户补充领域或关键词"],
        )


class DataAnalyst:
    name = "data_analyst"

    def run(
        self,
        task: TaskEnvelope,
        state: TaskState,
        context: ContextSnapshot,
        runtime: ToolRuntime,
        trace_parent_id: str | None = None,
    ) -> AgentResult:
        region = "华东" if "华东" in task.objective else "华南" if "华南" in task.objective else "华东"
        result = runtime.invoke(
            "query_metrics",
            allowed_tools=task.allowed_tools,
            arguments={"region": region},
            task_id=task.task_id,
            trace_id=task.task_id,
            parent_span_id=trace_parent_id,
        )
        rows = result.data["rows"]
        if len(rows) < 2:
            return AgentResult(self.name, "failed", "缺少可比较的历史指标", data=result.data, evidence=list(result.evidence))
        current, previous = rows[0], rows[1]
        change = (current["orders"] - previous["orders"]) / previous["orders"]
        facts = [
            f"{region}订单从 {previous['orders']} 下降到 {current['orders']}，变化 {change:.1%}",
            f"当前缺货率为 {current['stockout_rate']:.1%}，上期为 {previous['stockout_rate']:.1%}",
        ]
        return AgentResult(
            agent_name=self.name,
            status="completed",
            summary="完成只读指标分析",
            data={
                "region": region,
                "current": current,
                "previous": previous,
                "order_change": change,
                "facts": facts,
                "tool_call": result.metadata,
            },
            evidence=list(result.evidence),
        )


class Verifier:
    name = "verifier"

    def run(
        self,
        task: TaskEnvelope,
        state: TaskState,
        context: ContextSnapshot,
        results: list[AgentResult],
    ) -> AgentResult:
        evidence = [e for result in results for e in result.evidence]
        failures = [result.agent_name for result in results if result.status == "failed"]
        warnings: list[str] = []
        if not evidence:
            warnings.append("没有可引用证据，不能输出确定性结论")
        if failures:
            warnings.append(f"子任务失败：{', '.join(failures)}")
        status = "completed" if not warnings else "failed"
        state.completed_steps.append("verification")
        state.decisions.append("结论必须绑定 evidence_refs")
        return AgentResult(
            agent_name=self.name,
            status=status,
            summary="证据和约束检查通过" if status == "completed" else "验证未通过",
            data={"evidence_count": len(evidence), "failed_agents": failures},
            evidence=evidence,
            warnings=warnings,
        )


class SynthesisAgent:
    name = "synthesis_agent"

    def run(
        self,
        task: TaskEnvelope,
        state: TaskState,
        context: ContextSnapshot,
        results: list[AgentResult],
        trace_parent_id: str | None = None,
    ) -> AgentResult:
        analyst = next((r for r in results if r.agent_name == "data_analyst"), None)
        researcher = next((r for r in results if r.agent_name == "knowledge_researcher"), None)
        verifier = next((r for r in results if r.agent_name == "verifier"), None)
        evidence = [e for result in results for e in result.evidence]
        verification_failed = verifier is None or verifier.status != "completed"
        if verification_failed:
            answer = "证据验证未通过，系统不会生成确定性结论或执行后续动作。"
            recommendation = "补充可引用证据、修复失败的子任务后重试。"
        elif analyst and analyst.status == "completed":
            data = analyst.data
            change = data["order_change"]
            cause = "缺货率上升是优先排查方向" if data["current"]["stockout_rate"] > 0.10 else "需要进一步调查订单取消率和履约延迟"
            answer = (
                f"{data['region']}订单较上期变化 {change:.1%}。当前缺货率为 "
                f"{data['current']['stockout_rate']:.1%}，相较上期明显上升；{cause}。"
            )
            recommendation = "按照库存异常处理规范检查补货计划，并由运营负责人审批补货工单。"
        else:
            answer = "当前证据不足，无法形成可靠结论。"
            recommendation = "补充数据范围或文档来源后重试。"
        state.completed_steps.append("synthesis")
        state.artifacts["answer"] = answer
        state.artifacts["recommendation"] = recommendation
        return AgentResult(
            agent_name=self.name,
            status="completed" if evidence and not verification_failed else "failed",
            summary=answer,
            data={"answer": answer, "recommendation": recommendation, "source_count": len(evidence)},
            evidence=evidence,
            warnings=(researcher.warnings if researcher else []),
            next_action="prepare_ticket" if "工单" in task.objective and not verification_failed else None,
        )


class IncidentSynthesisAgent:
    name = "incident_synthesis_agent"

    def __init__(self, llm_provider: LLMProvider | None = None) -> None:
        self.llm_provider = llm_provider

    def run(
        self,
        task: TaskEnvelope,
        state: TaskState,
        context: ContextSnapshot,
        results: list[AgentResult],
        trace_parent_id: str | None = None,
    ) -> AgentResult:
        metrics = next((result for result in results if result.agent_name == "metrics_investigator"), None)
        logs = next((result for result in results if result.agent_name == "log_investigator"), None)
        runbook = next((result for result in results if result.agent_name == "runbook_researcher"), None)
        verifier = next((result for result in results if result.agent_name == "verifier"), None)
        evidence = [item for result in results for item in result.evidence]
        verified = verifier is not None and verifier.status == "completed"
        correlated = bool(
            verified
            and metrics
            and metrics.data.get("db_pool_utilization", 0) >= 0.90
            and logs
            and "DB_POOL_TIMEOUT" in logs.data.get("codes", [])
            and runbook
            and runbook.data.get("runbook")
        )
        model_metadata: dict[str, Any] | None = None
        model_error: str | None = None
        if correlated and self.llm_provider is not None:
            try:
                response = self.llm_provider.complete_json(
                    system_prompt=INCIDENT_SYNTHESIS_SYSTEM_PROMPT,
                    user_prompt=self._model_input(task, metrics, logs, runbook, evidence),
                    task_id=task.task_id,
                    trace_id=task.task_id,
                    parent_span_id=trace_parent_id,
                )
                answer, recommendation = self._validate_model_output(response.data, evidence)
                model_metadata = {
                    "provider": response.provider,
                    "model": response.model,
                    "call_id": response.call_id,
                    "attempts": response.attempts,
                    "latency_ms": round(response.latency_ms, 3),
                    "finish_reason": response.finish_reason,
                    "usage": asdict(response.usage),
                }
            except (LLMError, ValueError) as exc:
                correlated = False
                model_error = f"{type(exc).__name__}: {exc}"
        if correlated:
            if model_metadata is None:
                service = metrics.data["service"]
                answer = (
                    f"{service} 当前事故的高置信根因是数据库连接池耗尽：连接池利用率 "
                    f"{metrics.data['db_pool_utilization']:.1%}，错误率 {metrics.data['error_rate']:.1%}，"
                    "日志同时出现 DB_POOL_TIMEOUT 与 HTTP_503。"
                )
                recommendation = "按 Runbook 暂停非必要批处理，检查慢查询和最近发布；经值班负责人审批后扩容或回滚。"
            confidence = "high"
        else:
            healthy_triage = bool(metrics and metrics.data.get("anomalous") is False)
            if healthy_triage:
                service = metrics.data["service"]
                answer = f"{service} 当前指标未达到事故阈值，系统不会生成事故根因或处置工单。"
                recommendation = "保持监控；若错误率、P95 延迟或资源饱和度持续升高，再启动完整事故诊断。"
            else:
                answer = "运维证据未通过交叉验证，系统不会给出确定性根因或自动处置。"
                recommendation = "修复失败的数据源或补齐 Metrics、Logs、Runbook 证据后重新诊断。"
            confidence = "insufficient"
        state.completed_steps.append("incident_synthesis")
        state.artifacts["answer"] = answer
        state.artifacts["recommendation"] = recommendation
        state.artifacts["root_cause_confidence"] = confidence
        state.artifacts["model_call"] = model_metadata
        if model_error:
            state.artifacts["model_error"] = model_error
        return AgentResult(
            self.name,
            "completed" if correlated else "failed",
            answer,
            data={
                "answer": answer,
                "recommendation": recommendation,
                "root_cause_confidence": confidence,
                "source_count": len(evidence),
                "model_call": model_metadata,
                "model_error": model_error,
            },
            evidence=evidence,
            warnings=[model_error] if model_error else [],
            next_action="prepare_ticket" if correlated and "工单" in task.objective else None,
        )

    @staticmethod
    def _model_input(
        task: TaskEnvelope,
        metrics: AgentResult,
        logs: AgentResult,
        runbook: AgentResult,
        evidence: list[Evidence],
    ) -> str:
        payload = {
            "objective": task.objective,
            "metrics": {
                key: metrics.data[key]
                for key in ("service", "error_rate", "baseline_error_rate", "p95_latency_ms", "db_pool_utilization")
            },
            "log_codes": logs.data.get("codes", []),
            "log_counts": {entry["code"]: entry["count"] for entry in logs.data.get("entries", [])},
            "runbook": {
                "title": runbook.data["runbook"]["title"],
                "steps": runbook.data["runbook"]["steps"],
            },
            "evidence_refs": [item.evidence_id for item in evidence],
        }
        return "Synthesize the verified evidence into the required JSON object:\n" + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _validate_model_output(data: dict[str, Any], evidence: list[Evidence]) -> tuple[str, str]:
        if data.get("root_cause") != "database_pool_exhaustion" or data.get("confidence") != "high":
            raise LLMResponseError("Model root cause or confidence violates the verified evidence contract")
        answer = data.get("answer")
        recommendation = data.get("recommendation")
        refs = data.get("evidence_refs")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 4000:
            raise LLMResponseError("Model answer must be a non-empty string within 4000 characters")
        if not isinstance(recommendation, str) or not recommendation.strip() or len(recommendation) > 4000:
            raise LLMResponseError("Model recommendation must be a non-empty string within 4000 characters")
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            raise LLMResponseError("Model evidence_refs must be a string array")
        available = {item.evidence_id for item in evidence}
        if not set(refs).issubset(available):
            raise LLMResponseError("Model cited evidence outside the verified evidence set")
        cited_types = {item.source_type for item in evidence if item.evidence_id in refs}
        if not {"service_metrics", "log_query", "runbook"}.issubset(cited_types):
            raise LLMResponseError("Model must cite metrics, logs and runbook evidence")
        return answer.strip(), recommendation.strip()
