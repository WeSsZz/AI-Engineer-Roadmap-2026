from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import (
    AgentResult,
    Budget,
    Event,
    Evidence,
    SpanKind,
    SpanStatus,
    TaskEnvelope,
    TaskState,
    TaskStatus,
    TraceSpan,
)


class TaskNotFoundError(KeyError):
    pass


class ConcurrentCheckpointError(RuntimeError):
    pass


@dataclass
class StoredExecution:
    task: TaskEnvelope
    state: TaskState
    agent_results: list[AgentResult]
    events: list[Event]


class SQLiteEventStore:
    """SQLite-backed task snapshots plus an append-only event log.

    Snapshots make resume fast; the event table preserves the audit trail. Versioned
    updates prevent two workers from silently checkpointing the same task.
    """

    def __init__(self, path: str | Path = "work/enterprise_agent.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS events (
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, sequence),
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, sequence);

                CREATE TABLE IF NOT EXISTS spans (
                    span_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    parent_span_id TEXT,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_ms REAL,
                    attributes_json TEXT NOT NULL,
                    error_type TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_spans_task_started ON spans(task_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
                CREATE INDEX IF NOT EXISTS idx_spans_parent ON spans(parent_span_id);
                CREATE INDEX IF NOT EXISTS idx_spans_name ON spans(name);
                """
            )

    def start_span(self, span: TraceSpan) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO spans(
                    span_id, trace_id, task_id, parent_span_id, name, kind, status,
                    started_at, finished_at, duration_ms, attributes_json, error_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._span_values(span),
            )

    def finish_span(
        self,
        span_id: str,
        *,
        status: SpanStatus,
        finished_at: str,
        duration_ms: float,
        attributes: dict[str, Any],
        error_type: str | None = None,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE spans
                SET status = ?, finished_at = ?, duration_ms = ?, attributes_json = ?, error_type = ?
                WHERE span_id = ?
                """,
                (
                    status.value,
                    finished_at,
                    duration_ms,
                    json.dumps(attributes, ensure_ascii=False, sort_keys=True),
                    error_type,
                    span_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown span: {span_id}")

    def record_span(self, span: TraceSpan) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO spans(
                    span_id, trace_id, task_id, parent_span_id, name, kind, status,
                    started_at, finished_at, duration_ms, attributes_json, error_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._span_values(span),
            )

    def list_spans(self, task_id: str) -> list[TraceSpan]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM spans WHERE task_id = ? ORDER BY started_at, span_id",
                (task_id,),
            ).fetchall()
        return [self._decode_span(row) for row in rows]

    def trace_summary(self, task_id: str) -> dict[str, Any]:
        spans = self.list_spans(task_id)
        completed = [span for span in spans if span.duration_ms is not None]

        def aggregate(items: list[TraceSpan]) -> dict[str, Any]:
            durations = sorted(span.duration_ms for span in items if span.duration_ms is not None)
            errors = sum(span.status == SpanStatus.ERROR for span in items)
            return {
                "count": len(items),
                "error_count": errors,
                "error_rate": round(errors / len(items), 4) if items else 0.0,
                "p50_ms": self._percentile(durations, 0.50),
                "p95_ms": self._percentile(durations, 0.95),
            }

        by_kind = {
            kind.value: aggregate([span for span in completed if span.kind == kind])
            for kind in SpanKind
            if any(span.kind == kind for span in completed)
        }
        names = sorted({span.name for span in completed})
        return {
            "task_id": task_id,
            "span_count": len(spans),
            "running_count": sum(span.status == SpanStatus.RUNNING for span in spans),
            "overall": aggregate(completed),
            "by_kind": by_kind,
            "by_name": {name: aggregate([span for span in completed if span.name == name]) for name in names},
        }

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        # Nearest-rank is deterministic and keeps the metric dependency-free.
        rank = max(1, int(len(values) * percentile + 0.999999999))
        return round(values[min(rank, len(values)) - 1], 3)

    @staticmethod
    def _span_values(span: TraceSpan) -> tuple[Any, ...]:
        return (
            span.span_id,
            span.trace_id,
            span.task_id,
            span.parent_span_id,
            span.name,
            span.kind.value,
            span.status.value,
            span.started_at,
            span.finished_at,
            span.duration_ms,
            json.dumps(span.attributes, ensure_ascii=False, sort_keys=True),
            span.error_type,
        )

    @staticmethod
    def _decode_span(row: sqlite3.Row) -> TraceSpan:
        return TraceSpan(
            span_id=row["span_id"],
            trace_id=row["trace_id"],
            task_id=row["task_id"],
            parent_span_id=row["parent_span_id"],
            name=row["name"],
            kind=SpanKind(row["kind"]),
            status=SpanStatus(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            duration_ms=row["duration_ms"],
            attributes=json.loads(row["attributes_json"]),
            error_type=row["error_type"],
        )

    def create(self, execution: StoredExecution) -> None:
        snapshot = self._encode_snapshot(execution)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id, status, version, snapshot_json) VALUES (?, ?, ?, ?)",
                (execution.task.task_id, execution.state.status.value, execution.state.version, snapshot),
            )
            self._insert_events(connection, execution.task.task_id, execution.events)

    def checkpoint(self, execution: StoredExecution) -> None:
        expected_version = execution.state.version
        next_version = expected_version + 1
        execution.state.version = next_version
        snapshot = self._encode_snapshot(execution)
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, version = ?, snapshot_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE task_id = ? AND version = ?
                    """,
                    (
                        execution.state.status.value,
                        next_version,
                        snapshot,
                        execution.task.task_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConcurrentCheckpointError(
                        f"Task {execution.task.task_id} checkpoint version {expected_version} is stale"
                    )
                self._insert_events(connection, execution.task.task_id, execution.events)
        except Exception:
            execution.state.version = expected_version
            raise

    def load(self, task_id: str) -> StoredExecution:
        with self._connect() as connection:
            row = connection.execute("SELECT snapshot_json FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(task_id)
            event_rows = connection.execute(
                "SELECT sequence, event_type, actor, payload_json, occurred_at FROM events WHERE task_id = ? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        execution = self._decode_snapshot(row["snapshot_json"])
        execution.events = [
            Event(
                sequence=event["sequence"],
                event_type=event["event_type"],
                actor=event["actor"],
                payload=json.loads(event["payload_json"]),
                occurred_at=event["occurred_at"],
            )
            for event in event_rows
        ]
        return execution

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT task_id, status, version, created_at, updated_at FROM tasks ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _insert_events(connection: sqlite3.Connection, task_id: str, events: list[Event]) -> None:
        connection.executemany(
            """
            INSERT OR IGNORE INTO events(task_id, sequence, event_type, actor, payload_json, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    task_id,
                    event.sequence,
                    event.event_type,
                    event.actor,
                    json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    event.occurred_at,
                )
                for event in events
            ],
        )

    @staticmethod
    def _encode_snapshot(execution: StoredExecution) -> str:
        payload = {
            "schema_version": 1,
            "task": {
                "task_id": execution.task.task_id,
                "objective": execution.task.objective,
                "constraints": list(execution.task.constraints),
                "allowed_tools": list(execution.task.allowed_tools),
                "budget": asdict(execution.task.budget),
                "risk_level": execution.task.risk_level,
                "metadata": execution.task.metadata,
            },
            "state": execution.state.to_dict(),
            "agent_results": [SQLiteEventStore._encode_result(result) for result in execution.agent_results],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _decode_snapshot(raw: str) -> StoredExecution:
        payload = json.loads(raw)
        task_data = payload["task"]
        state_data = payload["state"]
        task = TaskEnvelope(
            task_id=task_data["task_id"],
            objective=task_data["objective"],
            constraints=tuple(task_data["constraints"]),
            allowed_tools=tuple(task_data["allowed_tools"]),
            budget=Budget(**task_data["budget"]),
            risk_level=task_data["risk_level"],
            metadata=task_data["metadata"],
        )
        state = TaskState(
            task_id=state_data["task_id"],
            status=TaskStatus(state_data["status"]),
            version=state_data["version"],
            completed_steps=state_data["completed_steps"],
            confirmed_facts=state_data["confirmed_facts"],
            decisions=state_data["decisions"],
            open_items=state_data["open_items"],
            evidence_refs=state_data["evidence_refs"],
            errors=state_data["errors"],
            artifacts=state_data["artifacts"],
        )
        results = [SQLiteEventStore._decode_result(result) for result in payload["agent_results"]]
        return StoredExecution(task=task, state=state, agent_results=results, events=[])

    @staticmethod
    def _encode_result(result: AgentResult) -> dict[str, Any]:
        return {
            "agent_name": result.agent_name,
            "status": result.status,
            "summary": result.summary,
            "data": result.data,
            "evidence": [asdict(evidence) for evidence in result.evidence],
            "warnings": result.warnings,
            "next_action": result.next_action,
        }

    @staticmethod
    def _decode_result(data: dict[str, Any]) -> AgentResult:
        return AgentResult(
            agent_name=data["agent_name"],
            status=data["status"],
            summary=data["summary"],
            data=data["data"],
            evidence=[Evidence(**evidence) for evidence in data["evidence"]],
            warnings=data["warnings"],
            next_action=data["next_action"],
        )
