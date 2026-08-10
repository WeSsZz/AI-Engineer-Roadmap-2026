from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal


RiskLevel = Literal["low", "medium", "high"]
ResultStatus = Literal["completed", "needs_approval", "failed"]


class TaskStatus(str, Enum):
    CREATED = "created"
    RUNNING_SPECIALISTS = "running_specialists"
    VERIFYING = "verifying"
    SYNTHESIZING = "synthesizing"
    WAITING_APPROVAL = "needs_approval"
    EXECUTING_ACTION = "executing_action"
    COMPLETED = "completed"
    SAFELY_REFUSED = "safely_refused"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SpanKind(str, Enum):
    RUNTIME = "runtime"
    AGENT = "agent"
    TOOL = "tool"
    MODEL = "model"


class SpanStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass(frozen=True)
class Budget:
    max_steps: int = 8
    max_tokens: int = 12_000
    max_tool_calls: int = 12


@dataclass(frozen=True)
class TaskEnvelope:
    task_id: str
    objective: str
    constraints: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    budget: Budget = field(default_factory=Budget)
    risk_level: RiskLevel = "low"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source_type: str
    title: str
    excerpt: str
    locator: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    agent_name: str
    status: ResultStatus
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    next_action: str | None = None


@dataclass(frozen=True)
class Event:
    sequence: int
    event_type: str
    actor: str
    payload: dict[str, Any]
    occurred_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass(frozen=True)
class TraceSpan:
    span_id: str
    trace_id: str
    task_id: str
    parent_span_id: str | None
    name: str
    kind: SpanKind
    status: SpanStatus
    started_at: str
    finished_at: str | None = None
    duration_ms: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None


@dataclass
class TaskState:
    task_id: str
    status: TaskStatus = TaskStatus.CREATED
    version: int = 0
    completed_steps: list[str] = field(default_factory=list)
    confirmed_facts: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    open_items: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value
