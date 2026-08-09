from __future__ import annotations

from typing import Any

from .context import ContextSnapshot
from .models import AgentResult, Evidence, TaskEnvelope, TaskState
from .tools import ToolRuntime


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
