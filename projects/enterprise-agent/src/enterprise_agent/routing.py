from __future__ import annotations

from dataclasses import asdict, dataclass

from .models import AgentResult, TaskEnvelope


ROUTING_POLICY_VERSION = "incident-risk-v1"
ORCHESTRATION_MODES = ("fixed_multi", "adaptive_shadow", "adaptive")


@dataclass(frozen=True)
class RoutingDecision:
    policy_version: str
    selected_route: str
    reason: str
    risk_score: int
    anomalous: bool | None
    write_intent: bool
    fail_closed: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class IncidentRiskRouter:
    """Deterministic, auditable routing policy for incident investigations."""

    def decide(self, task: TaskEnvelope, metrics: AgentResult | None) -> RoutingDecision:
        write_intent = "工单" in task.objective or "ticket" in task.objective.lower()
        metrics_available = metrics is not None and "anomalous" in metrics.data
        if not metrics_available:
            return RoutingDecision(
                policy_version=ROUTING_POLICY_VERSION,
                selected_route="verified_multi_agent",
                reason="routing_signal_unavailable_fail_closed",
                risk_score=3 + int(write_intent),
                anomalous=None,
                write_intent=write_intent,
                fail_closed=True,
            )

        anomalous = bool(metrics.data["anomalous"])
        if not anomalous:
            return RoutingDecision(
                policy_version=ROUTING_POLICY_VERSION,
                selected_route="single_agent_triage",
                reason="metrics_below_incident_threshold",
                risk_score=int(write_intent),
                anomalous=False,
                write_intent=write_intent,
                fail_closed=False,
            )

        return RoutingDecision(
            policy_version=ROUTING_POLICY_VERSION,
            selected_route="verified_multi_agent",
            reason=(
                "high_risk_anomaly_with_write_intent"
                if write_intent
                else "anomaly_requires_evidence_verification"
            ),
            risk_score=2 + int(write_intent),
            anomalous=True,
            write_intent=write_intent,
            fail_closed=False,
        )
