from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .tool_gateway import ToolPermanentError, ToolTransientError


@dataclass(frozen=True)
class FaultRule:
    tool_name: str
    mode: str
    times: int = 1
    delay_seconds: float = 0.1

    def __post_init__(self) -> None:
        if self.mode not in {"transient", "permanent", "timeout"}:
            raise ValueError(f"Unsupported fault mode: {self.mode}")
        if self.times <= 0:
            raise ValueError("Fault times must be positive")
        if self.delay_seconds < 0:
            raise ValueError("Fault delay cannot be negative")


class FaultInjector:
    """Thread-safe deterministic faults at the provider adapter boundary."""

    def __init__(self, rules: tuple[FaultRule, ...] = (), sleeper: Callable[[float], None] = time.sleep) -> None:
        self._rules = {rule.tool_name: rule for rule in rules}
        self._remaining = {rule.tool_name: rule.times for rule in rules}
        self._triggered: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._sleeper = sleeper

    @property
    def triggered(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(self._triggered)

    def before_call(self, tool_name: str) -> None:
        with self._lock:
            rule = self._rules.get(tool_name)
            if rule is None or self._remaining[tool_name] <= 0:
                return
            self._remaining[tool_name] -= 1
            self._triggered.append((tool_name, rule.mode))
        if rule.mode == "transient":
            raise ToolTransientError(f"Injected transient failure for '{tool_name}'")
        if rule.mode == "permanent":
            raise ToolPermanentError(f"Injected permanent failure for '{tool_name}'")
        self._sleeper(rule.delay_seconds)
