from __future__ import annotations

import threading
import time
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from enterprise_agent.tool_gateway import (
    ArgumentSpec,
    CircuitOpenError,
    ToolConcurrencyError,
    ToolOutput,
    ToolPermanentError,
    ToolPermissionError,
    ToolPolicy,
    ToolRateLimitError,
    ToolRuntime,
    ToolSpec,
    ToolTimeoutError,
    ToolTransientError,
    ToolValidationError,
)
from enterprise_agent.tools import build_demo_runtime


class ToolGatewayTests(unittest.TestCase):
    def make_runtime(self) -> ToolRuntime:
        runtime = ToolRuntime(max_workers=8)
        self.addCleanup(runtime.close)
        return runtime

    def test_schema_rejects_missing_unknown_and_invalid_arguments(self) -> None:
        runtime = build_demo_runtime()
        self.addCleanup(runtime.close)
        with self.assertRaises(ToolValidationError):
            runtime.invoke("search_docs", allowed_tools=("search_docs",), arguments={})
        with self.assertRaises(ToolValidationError):
            runtime.invoke(
                "search_docs",
                allowed_tools=("search_docs",),
                arguments={"query": "库存", "top_k": 0},
            )
        with self.assertRaises(ToolValidationError):
            runtime.invoke(
                "search_docs",
                allowed_tools=("search_docs",),
                arguments={"query": "库存", "unexpected": True},
            )
        self.assertEqual([record.status for record in runtime.invocations], ["rejected", "rejected", "rejected"])

    def test_high_risk_tool_requires_approval_and_idempotency(self) -> None:
        runtime = build_demo_runtime()
        self.addCleanup(runtime.close)
        base = {"title": "test", "description": "test", "idempotency_key": "action-1"}
        with self.assertRaises(ToolValidationError):
            runtime.invoke(
                "create_ticket",
                allowed_tools=("create_ticket",),
                arguments={"title": "test", "description": "test", "approval_token": "APPROVED_BY_USER"},
            )
        with self.assertRaises(ToolPermissionError):
            runtime.invoke(
                "create_ticket",
                allowed_tools=("create_ticket",),
                arguments={**base, "approval_token": "not-approved"},
            )

    def test_transient_failure_retries_then_records_success(self) -> None:
        runtime = self.make_runtime()
        calls = 0

        def flaky(value: int) -> ToolOutput:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ToolTransientError("temporary upstream failure")
            return ToolOutput("flaky", "completed", {"value": value})

        runtime.register(
            ToolSpec(
                "flaky",
                "transient test",
                "low",
                flaky,
                arguments={"value": ArgumentSpec(int)},
                policy=ToolPolicy(max_retries=2, retry_backoff_seconds=0),
            )
        )
        output = runtime.invoke("flaky", allowed_tools=("flaky",), arguments={"value": 7}, task_id="task-1")
        self.assertEqual(output.data["value"], 7)
        self.assertEqual(output.metadata["attempts"], 2)
        self.assertEqual(calls, 2)
        self.assertEqual([record.status for record in runtime.invocations], ["failed", "completed"])
        self.assertTrue(all(record.task_id == "task-1" for record in runtime.invocations))

    def test_permanent_failure_is_not_retried(self) -> None:
        runtime = self.make_runtime()
        calls = 0

        def broken() -> ToolOutput:
            nonlocal calls
            calls += 1
            raise ValueError("bad request")

        runtime.register(
            ToolSpec(
                "broken",
                "permanent test",
                "low",
                broken,
                policy=ToolPolicy(max_retries=3, retry_backoff_seconds=0),
            )
        )
        with self.assertRaises(ToolPermanentError):
            runtime.invoke("broken", allowed_tools=("broken",), arguments={})
        self.assertEqual(calls, 1)
        self.assertEqual(len(runtime.invocations), 1)

    def test_timeout_is_classified_and_retried(self) -> None:
        runtime = self.make_runtime()

        def slow() -> ToolOutput:
            time.sleep(0.03)
            return ToolOutput("slow", "completed", {})

        runtime.register(
            ToolSpec(
                "slow",
                "timeout test",
                "low",
                slow,
                policy=ToolPolicy(timeout_seconds=0.005, max_retries=1, retry_backoff_seconds=0),
            )
        )
        with self.assertRaises(ToolTimeoutError):
            runtime.invoke("slow", allowed_tools=("slow",), arguments={})
        self.assertEqual(len(runtime.invocations), 2)
        self.assertTrue(all(record.error_type == "ToolTimeoutError" for record in runtime.invocations))

    def test_timed_out_handler_keeps_concurrency_slot_until_it_stops(self) -> None:
        runtime = self.make_runtime()
        calls = 0

        def slow() -> ToolOutput:
            nonlocal calls
            calls += 1
            if calls == 1:
                time.sleep(0.04)
            return ToolOutput("slow", "completed", {})

        runtime.register(
            ToolSpec(
                "slow",
                "runaway timeout test",
                "low",
                slow,
                policy=ToolPolicy(timeout_seconds=0.005, max_concurrency=1, circuit_failure_threshold=3),
            )
        )
        with self.assertRaises(ToolTimeoutError):
            runtime.invoke("slow", allowed_tools=("slow",), arguments={})
        with self.assertRaises(ToolConcurrencyError):
            runtime.invoke("slow", allowed_tools=("slow",), arguments={})
        time.sleep(0.05)
        self.assertEqual(runtime.invoke("slow", allowed_tools=("slow",), arguments={}).status, "completed")

    def test_circuit_opens_after_consecutive_failures(self) -> None:
        runtime = self.make_runtime()
        calls = 0

        def unavailable() -> ToolOutput:
            nonlocal calls
            calls += 1
            raise ToolTransientError("down")

        runtime.register(
            ToolSpec(
                "unavailable",
                "circuit test",
                "low",
                unavailable,
                policy=ToolPolicy(
                    max_retries=0,
                    circuit_failure_threshold=2,
                    circuit_recovery_seconds=60,
                ),
            )
        )
        for _ in range(2):
            with self.assertRaises(ToolTransientError):
                runtime.invoke("unavailable", allowed_tools=("unavailable",), arguments={})
        with self.assertRaises(CircuitOpenError):
            runtime.invoke("unavailable", allowed_tools=("unavailable",), arguments={})
        self.assertEqual(calls, 2)
        self.assertEqual(runtime.invocations[-1].status, "rejected")

    def test_circuit_allows_one_half_open_probe_after_recovery_window(self) -> None:
        now = [0.0]
        runtime = ToolRuntime(max_workers=2, monotonic=lambda: now[0], sleeper=lambda _: None)
        self.addCleanup(runtime.close)
        unavailable = [True]

        def recoverable() -> ToolOutput:
            if unavailable[0]:
                raise ToolTransientError("down")
            return ToolOutput("recoverable", "completed", {})

        runtime.register(
            ToolSpec(
                "recoverable",
                "half-open test",
                "low",
                recoverable,
                policy=ToolPolicy(circuit_failure_threshold=1, circuit_recovery_seconds=10),
            )
        )
        with self.assertRaises(ToolTransientError):
            runtime.invoke("recoverable", allowed_tools=("recoverable",), arguments={})
        with self.assertRaises(CircuitOpenError):
            runtime.invoke("recoverable", allowed_tools=("recoverable",), arguments={})

        now[0] = 11.0
        unavailable[0] = False
        self.assertEqual(runtime.invoke("recoverable", allowed_tools=("recoverable",), arguments={}).status, "completed")
        self.assertEqual(runtime.invoke("recoverable", allowed_tools=("recoverable",), arguments={}).status, "completed")

    def test_fixed_window_rate_limit_rejects_excess_calls(self) -> None:
        runtime = self.make_runtime()

        def healthy() -> ToolOutput:
            return ToolOutput("healthy", "completed", {})

        runtime.register(
            ToolSpec(
                "healthy",
                "rate test",
                "low",
                healthy,
                policy=ToolPolicy(rate_limit_calls=2, rate_limit_window_seconds=60),
            )
        )
        runtime.invoke("healthy", allowed_tools=("healthy",), arguments={})
        runtime.invoke("healthy", allowed_tools=("healthy",), arguments={})
        with self.assertRaises(ToolRateLimitError):
            runtime.invoke("healthy", allowed_tools=("healthy",), arguments={})

    def test_concurrency_limit_fails_fast_when_saturated(self) -> None:
        runtime = self.make_runtime()
        started = threading.Event()
        release = threading.Event()

        def blocking() -> ToolOutput:
            started.set()
            release.wait(timeout=1)
            return ToolOutput("blocking", "completed", {})

        runtime.register(
            ToolSpec(
                "blocking",
                "concurrency test",
                "low",
                blocking,
                policy=ToolPolicy(max_concurrency=1, queue_timeout_seconds=0.01, timeout_seconds=1),
            )
        )
        with ThreadPoolExecutor(max_workers=1) as callers:
            first = callers.submit(runtime.invoke, "blocking", allowed_tools=("blocking",), arguments={})
            self.assertTrue(started.wait(timeout=0.2))
            with self.assertRaises(ToolConcurrencyError):
                runtime.invoke("blocking", allowed_tools=("blocking",), arguments={})
            release.set()
            self.assertEqual(first.result(timeout=1).status, "completed")

    def test_retryable_high_risk_tool_must_require_idempotency(self) -> None:
        runtime = self.make_runtime()
        with self.assertRaises(ValueError):
            runtime.register(
                ToolSpec(
                    "unsafe_write",
                    "unsafe",
                    "high",
                    lambda: ToolOutput("unsafe_write", "completed", {}),
                    policy=ToolPolicy(max_retries=1, idempotency_required=False),
                )
            )

    def test_concurrent_idempotent_writes_create_one_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tools.db"
            runtime = build_demo_runtime(database)
            self.addCleanup(runtime.close)
            arguments = {
                "title": "same action",
                "description": "must only happen once",
                "approval_token": "APPROVED_BY_USER",
                "idempotency_key": "task-1:create_ticket:1",
            }
            with ThreadPoolExecutor(max_workers=2) as callers:
                futures = [
                    callers.submit(
                        runtime.invoke,
                        "create_ticket",
                        allowed_tools=("create_ticket",),
                        arguments=arguments,
                    )
                    for _ in range(2)
                ]
                outputs = [future.result(timeout=2) for future in futures]
            self.assertEqual({output.data["id"] for output in outputs}, {"INC-0001"})
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM demo_tickets").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
