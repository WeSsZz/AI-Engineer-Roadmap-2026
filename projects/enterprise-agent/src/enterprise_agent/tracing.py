from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

from .models import SpanKind, SpanStatus, TraceSpan
from .store import SQLiteEventStore
from .tool_gateway import ToolCallRecord


class ActiveSpan:
    def __init__(
        self,
        store: SQLiteEventStore,
        *,
        trace_id: str,
        task_id: str,
        parent_span_id: str | None,
        name: str,
        kind: SpanKind,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.span_id = f"span-{uuid.uuid4().hex}"
        self.trace_id = trace_id
        self.task_id = task_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.kind = kind
        self.attributes = dict(attributes or {})
        self.started_at = datetime.now(UTC)
        self._started = time.monotonic()
        self._forced_error_type: str | None = None

    def __enter__(self) -> ActiveSpan:
        self.store.start_span(
            TraceSpan(
                span_id=self.span_id,
                trace_id=self.trace_id,
                task_id=self.task_id,
                parent_span_id=self.parent_span_id,
                name=self.name,
                kind=self.kind,
                status=SpanStatus.RUNNING,
                started_at=self.started_at.isoformat(),
                attributes=self.attributes,
            )
        )
        return self

    def set_attribute(self, name: str, value: Any) -> None:
        self.attributes[name] = value

    def mark_error(self, error_type: str) -> None:
        self._forced_error_type = error_type

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.store.finish_span(
            self.span_id,
            status=SpanStatus.ERROR if exc_type or self._forced_error_type else SpanStatus.COMPLETED,
            finished_at=datetime.now(UTC).isoformat(),
            duration_ms=(time.monotonic() - self._started) * 1000,
            attributes=self.attributes,
            error_type=exc_type.__name__ if exc_type else self._forced_error_type,
        )


class SQLiteTraceRecorder:
    def __init__(self, store: SQLiteEventStore) -> None:
        self.store = store

    def start_span(
        self,
        *,
        trace_id: str,
        task_id: str,
        name: str,
        kind: SpanKind,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> ActiveSpan:
        return ActiveSpan(
            self.store,
            trace_id=trace_id,
            task_id=task_id,
            parent_span_id=parent_span_id,
            name=name,
            kind=kind,
            attributes=attributes,
        )

    def record_tool_call(self, record: ToolCallRecord) -> None:
        if record.task_id is None or record.trace_id is None:
            return
        started = datetime.fromisoformat(record.started_at)
        finished = started + timedelta(milliseconds=record.duration_ms)
        self.store.record_span(
            TraceSpan(
                span_id=f"{record.call_id}-attempt-{record.attempt}",
                trace_id=record.trace_id,
                task_id=record.task_id,
                parent_span_id=record.parent_span_id,
                name=f"tool.{record.tool_name}",
                kind=SpanKind.TOOL,
                status=SpanStatus.COMPLETED if record.status == "completed" else SpanStatus.ERROR,
                started_at=record.started_at,
                finished_at=finished.isoformat(),
                duration_ms=record.duration_ms,
                attributes={
                    "attempt": record.attempt,
                    "gateway_status": record.status,
                    "idempotency_key": record.idempotency_key,
                },
                error_type=record.error_type,
            )
        )
