from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Callable

from .models import Evidence


class ToolError(RuntimeError):
    """Base class for errors classified by the tool gateway."""


class ToolValidationError(ToolError):
    pass


class ToolPermissionError(ToolError):
    pass


class ToolTimeoutError(ToolError):
    pass


class ToolTransientError(ToolError):
    pass


class ToolPermanentError(ToolError):
    pass


class ToolRateLimitError(ToolError):
    pass


class ToolConcurrencyError(ToolError):
    pass


class CircuitOpenError(ToolError):
    pass


@dataclass(frozen=True)
class ToolOutput:
    tool_name: str
    status: str
    data: dict[str, Any]
    evidence: tuple[Evidence, ...] = ()
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArgumentSpec:
    expected_type: type | tuple[type, ...]
    required: bool = True
    allow_none: bool = False
    validator: Callable[[Any], bool] | None = None
    description: str = ""


@dataclass(frozen=True)
class ToolPolicy:
    timeout_seconds: float = 5.0
    max_retries: int = 0
    retry_backoff_seconds: float = 0.05
    max_concurrency: int = 8
    queue_timeout_seconds: float = 0.0
    rate_limit_calls: int = 0
    rate_limit_window_seconds: float = 60.0
    circuit_failure_threshold: int = 3
    circuit_recovery_seconds: float = 30.0
    requires_approval: bool = False
    idempotency_required: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk_level: str
    handler: Callable[..., ToolOutput]
    arguments: dict[str, ArgumentSpec] = field(default_factory=dict)
    policy: ToolPolicy = field(default_factory=ToolPolicy)


@dataclass(frozen=True)
class ToolCallRecord:
    call_id: str
    task_id: str | None
    tool_name: str
    attempt: int
    status: str
    duration_ms: float
    error_type: str | None
    idempotency_key: str | None
    started_at: str
    trace_id: str | None = None
    parent_span_id: str | None = None


@dataclass
class _ToolState:
    semaphore: threading.BoundedSemaphore
    lock: threading.Lock = field(default_factory=threading.Lock)
    request_times: deque[float] = field(default_factory=deque)
    consecutive_failures: int = 0
    opened_at: float | None = None
    half_open_in_progress: bool = False


class ToolRuntime:
    """Reliable tool gateway with validation, policy enforcement and failure isolation.

    Timeouts bound how long the caller waits. Python threads cannot forcibly stop a
    handler that ignores cancellation, so write tools must also use provider-side
    idempotency. The demo ticket tool does so with a SQLite unique key.
    """

    def __init__(
        self,
        *,
        max_workers: int = 16,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        record_sink: Callable[[ToolCallRecord], None] | None = None,
    ) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._states: dict[str, _ToolState] = {}
        self._records: list[ToolCallRecord] = []
        self._records_lock = threading.Lock()
        self._record_sink = record_sink
        self._sink_errors: list[str] = []
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tool-gateway")
        self._monotonic = monotonic
        self._sleeper = sleeper

    @property
    def invocations(self) -> tuple[ToolCallRecord, ...]:
        with self._records_lock:
            return tuple(self._records)

    @property
    def sink_errors(self) -> tuple[str, ...]:
        with self._records_lock:
            return tuple(self._sink_errors)

    def set_record_sink(self, sink: Callable[[ToolCallRecord], None] | None) -> None:
        self._record_sink = sink

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool '{spec.name}' is already registered")
        if spec.policy.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if spec.policy.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if spec.policy.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if spec.policy.queue_timeout_seconds < 0:
            raise ValueError("queue_timeout_seconds cannot be negative")
        if spec.policy.circuit_failure_threshold <= 0:
            raise ValueError("circuit_failure_threshold must be positive")
        if spec.policy.rate_limit_calls > 0 and spec.policy.rate_limit_window_seconds <= 0:
            raise ValueError("rate_limit_window_seconds must be positive when rate limiting is enabled")
        if spec.risk_level == "high" and spec.policy.max_retries > 0 and not spec.policy.idempotency_required:
            raise ValueError("Retryable high-risk tools must require an idempotency key")
        self._tools[spec.name] = spec
        self._states[spec.name] = _ToolState(threading.BoundedSemaphore(spec.policy.max_concurrency))

    def invoke(
        self,
        name: str,
        *,
        allowed_tools: tuple[str, ...],
        arguments: dict[str, Any],
        task_id: str | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> ToolOutput:
        call_id = f"call-{uuid.uuid4().hex[:12]}"
        request_started = self._monotonic()
        if name not in allowed_tools:
            error = ToolPermissionError(f"Tool '{name}' is not allowed for this task")
            self._record_rejection(call_id, task_id, name, arguments, error, trace_id, parent_span_id)
            raise error
        spec = self._tools.get(name)
        if spec is None:
            error = ToolValidationError(f"Unknown tool '{name}'")
            self._record_rejection(call_id, task_id, name, arguments, error, trace_id, parent_span_id)
            raise error

        try:
            self._validate_arguments(spec, arguments)
            self._enforce_action_policy(spec, arguments)
            state = self._states[name]
            self._consume_rate_limit(spec, state)
        except ToolError as error:
            self._record_rejection(call_id, task_id, name, arguments, error, trace_id, parent_span_id)
            raise

        try:
            self._before_circuit(spec, state)
        except ToolError as error:
            self._record_rejection(call_id, task_id, name, arguments, error, trace_id, parent_span_id)
            raise

        attempts = spec.policy.max_retries + 1
        for attempt in range(1, attempts + 1):
            attempt_started = self._monotonic()
            started_at = datetime.now(UTC).isoformat()
            try:
                acquired = state.semaphore.acquire(timeout=spec.policy.queue_timeout_seconds)
                if not acquired:
                    raise ToolConcurrencyError(f"Tool '{name}' concurrency limit is saturated")
                try:
                    future = self._executor.submit(spec.handler, **arguments)
                except Exception:
                    state.semaphore.release()
                    raise
                # Keep the slot occupied even when the caller times out. A Python
                # thread cannot be killed safely; the callback releases capacity
                # only when the underlying handler actually stops.
                future.add_done_callback(lambda _: state.semaphore.release())
                output = future.result(timeout=spec.policy.timeout_seconds)
                if not isinstance(output, ToolOutput):
                    raise ToolPermanentError(f"Tool '{name}' returned {type(output).__name__}, expected ToolOutput")
                duration_ms = (self._monotonic() - attempt_started) * 1000
                self._append_record(
                    ToolCallRecord(
                        call_id=call_id,
                        task_id=task_id,
                        tool_name=name,
                        attempt=attempt,
                        status="completed",
                        duration_ms=duration_ms,
                        error_type=None,
                        idempotency_key=arguments.get("idempotency_key"),
                        started_at=started_at,
                        trace_id=trace_id,
                        parent_span_id=parent_span_id,
                    )
                )
                self._on_success(state)
                return replace(
                    output,
                    metadata={
                        **output.metadata,
                        "call_id": call_id,
                        "attempts": attempt,
                        "latency_ms": round((self._monotonic() - request_started) * 1000, 3),
                    },
                )
            except FutureTimeoutError:
                future.cancel()
                error: ToolError = ToolTimeoutError(
                    f"Tool '{name}' exceeded timeout of {spec.policy.timeout_seconds:.3f}s"
                )
            except ToolError as caught:
                error = caught
            except Exception as caught:
                error = ToolPermanentError(f"Tool '{name}' failed: {type(caught).__name__}: {caught}")

            duration_ms = (self._monotonic() - attempt_started) * 1000
            self._append_record(
                ToolCallRecord(
                    call_id=call_id,
                    task_id=task_id,
                    tool_name=name,
                    attempt=attempt,
                    status="failed",
                    duration_ms=duration_ms,
                    error_type=type(error).__name__,
                    idempotency_key=arguments.get("idempotency_key"),
                    started_at=started_at,
                    trace_id=trace_id,
                    parent_span_id=parent_span_id,
                )
            )
            retryable = isinstance(error, (ToolTransientError, ToolTimeoutError))
            if not retryable or attempt == attempts:
                if not isinstance(error, ToolConcurrencyError):
                    self._on_failure(spec, state)
                raise error
            backoff = spec.policy.retry_backoff_seconds * (2 ** (attempt - 1))
            if backoff > 0:
                self._sleeper(backoff)
        raise AssertionError("Tool invocation loop ended unexpectedly")

    def close(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    @staticmethod
    def _validate_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> None:
        unknown = sorted(set(arguments) - set(spec.arguments))
        if unknown:
            raise ToolValidationError(f"Tool '{spec.name}' received unknown arguments: {', '.join(unknown)}")
        missing = sorted(name for name, argument in spec.arguments.items() if argument.required and name not in arguments)
        if missing:
            raise ToolValidationError(f"Tool '{spec.name}' is missing required arguments: {', '.join(missing)}")
        for name, value in arguments.items():
            argument = spec.arguments[name]
            if value is None:
                if argument.allow_none:
                    continue
                raise ToolValidationError(f"Argument '{name}' cannot be null")
            if argument.expected_type is int and isinstance(value, bool):
                raise ToolValidationError(f"Argument '{name}' must be int, got bool")
            if not isinstance(value, argument.expected_type):
                expected = argument.expected_type
                expected_name = (
                    ", ".join(item.__name__ for item in expected)
                    if isinstance(expected, tuple)
                    else expected.__name__
                )
                raise ToolValidationError(
                    f"Argument '{name}' must be {expected_name}, got {type(value).__name__}"
                )
            if argument.validator is not None and not argument.validator(value):
                raise ToolValidationError(f"Argument '{name}' failed validation")

    @staticmethod
    def _enforce_action_policy(spec: ToolSpec, arguments: dict[str, Any]) -> None:
        if spec.policy.requires_approval and arguments.get("approval_token") != "APPROVED_BY_USER":
            raise ToolPermissionError(f"Tool '{spec.name}' requires explicit human approval")
        if spec.policy.idempotency_required and not arguments.get("idempotency_key"):
            raise ToolValidationError(f"Tool '{spec.name}' requires an idempotency_key")

    def _consume_rate_limit(self, spec: ToolSpec, state: _ToolState) -> None:
        if spec.policy.rate_limit_calls <= 0:
            return
        now = self._monotonic()
        boundary = now - spec.policy.rate_limit_window_seconds
        with state.lock:
            while state.request_times and state.request_times[0] <= boundary:
                state.request_times.popleft()
            if len(state.request_times) >= spec.policy.rate_limit_calls:
                raise ToolRateLimitError(
                    f"Tool '{spec.name}' exceeded {spec.policy.rate_limit_calls} calls "
                    f"per {spec.policy.rate_limit_window_seconds:.1f}s"
                )
            state.request_times.append(now)

    def _before_circuit(self, spec: ToolSpec, state: _ToolState) -> None:
        with state.lock:
            if state.opened_at is None:
                return
            elapsed = self._monotonic() - state.opened_at
            if elapsed < spec.policy.circuit_recovery_seconds:
                raise CircuitOpenError(f"Circuit for tool '{spec.name}' is open")
            if state.half_open_in_progress:
                raise CircuitOpenError(f"Circuit for tool '{spec.name}' is half-open")
            state.half_open_in_progress = True

    @staticmethod
    def _on_success(state: _ToolState) -> None:
        with state.lock:
            state.consecutive_failures = 0
            state.opened_at = None
            state.half_open_in_progress = False

    def _on_failure(self, spec: ToolSpec, state: _ToolState) -> None:
        with state.lock:
            state.consecutive_failures += 1
            state.half_open_in_progress = False
            if state.consecutive_failures >= spec.policy.circuit_failure_threshold:
                state.opened_at = self._monotonic()

    def _record_rejection(
        self,
        call_id: str,
        task_id: str | None,
        tool_name: str,
        arguments: dict[str, Any],
        error: ToolError,
        trace_id: str | None,
        parent_span_id: str | None,
    ) -> None:
        self._append_record(
            ToolCallRecord(
                call_id=call_id,
                task_id=task_id,
                tool_name=tool_name,
                attempt=0,
                status="rejected",
                duration_ms=0.0,
                error_type=type(error).__name__,
                idempotency_key=arguments.get("idempotency_key"),
                started_at=datetime.now(UTC).isoformat(),
                trace_id=trace_id,
                parent_span_id=parent_span_id,
            )
        )

    def _append_record(self, record: ToolCallRecord) -> None:
        with self._records_lock:
            self._records.append(record)
        if self._record_sink is not None:
            try:
                self._record_sink(record)
            except Exception as exc:
                # Telemetry must not turn a successful business call into a failure.
                with self._records_lock:
                    self._sink_errors.append(f"{type(exc).__name__}: {exc}")
