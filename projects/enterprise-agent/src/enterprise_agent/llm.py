from __future__ import annotations

import json
import os
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Protocol


class LLMError(RuntimeError):
    pass


class LLMConfigurationError(LLMError):
    pass


class LLMTransientError(LLMError):
    pass


class LLMTransportError(LLMError):
    pass


class LLMResponseError(LLMError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    api_key: str = field(repr=False)
    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com"
    thinking: str = "disabled"
    timeout_seconds: float = 30.0
    max_tokens: int = 1200
    max_retries: int = 2
    retry_backoff_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not self.api_key:
            raise LLMConfigurationError("DEEPSEEK_API_KEY is required for the DeepSeek provider")
        if self.model not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
            raise LLMConfigurationError(f"Unsupported DeepSeek model: {self.model}")
        if self.thinking not in {"enabled", "disabled"}:
            raise LLMConfigurationError("thinking must be 'enabled' or 'disabled'")
        if not self.base_url.startswith("https://"):
            raise LLMConfigurationError("DeepSeek base_url must use HTTPS")
        if self.timeout_seconds <= 0 or self.max_tokens <= 0 or self.max_retries < 0:
            raise LLMConfigurationError("timeout/max_tokens must be positive and max_retries non-negative")

    @classmethod
    def from_env(
        cls,
        *,
        model: str | None = None,
        thinking: str | None = None,
    ) -> LLMConfig:
        try:
            return cls(
                api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                model=model or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                thinking=thinking or os.environ.get("DEEPSEEK_THINKING", "disabled"),
                timeout_seconds=float(os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "30")),
                max_tokens=int(os.environ.get("DEEPSEEK_MAX_TOKENS", "1200")),
                max_retries=int(os.environ.get("DEEPSEEK_MAX_RETRIES", "2")),
            )
        except ValueError as exc:
            raise LLMConfigurationError("DeepSeek numeric environment configuration is invalid") from exc


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    data: dict[str, Any]
    provider: str
    model: str
    finish_reason: str
    usage: LLMUsage
    call_id: str
    attempts: int
    latency_ms: float


@dataclass(frozen=True)
class LLMCallRecord:
    call_id: str
    task_id: str | None
    trace_id: str | None
    parent_span_id: str | None
    provider: str
    model: str
    attempt: int
    status: str
    duration_ms: float
    error_type: str | None
    started_at: str
    usage: LLMUsage = field(default_factory=LLMUsage)


class LLMProvider(Protocol):
    name: str
    model: str

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        task_id: str | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> LLMResponse: ...

    def set_record_sink(self, sink: Callable[[LLMCallRecord], None] | None) -> None: ...


Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


class DeepSeekProvider:
    name = "deepseek"

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: Transport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        record_sink: Callable[[LLMCallRecord], None] | None = None,
    ) -> None:
        self.config = config
        self.model = config.model
        self._transport = transport or self._http_transport
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._record_sink = record_sink
        self._sink_errors: list[str] = []

    @property
    def sink_errors(self) -> tuple[str, ...]:
        return tuple(self._sink_errors)

    def set_record_sink(self, sink: Callable[[LLMCallRecord], None] | None) -> None:
        self._record_sink = sink

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        task_id: str | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> LLMResponse:
        if "json" not in f"{system_prompt} {user_prompt}".lower():
            raise LLMConfigurationError("DeepSeek JSON Output requires the prompt to explicitly request JSON")
        call_id = f"llm-{uuid.uuid4().hex[:12]}"
        call_started = self._monotonic()
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": self.config.thinking},
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        attempts = self.config.max_retries + 1
        for attempt in range(1, attempts + 1):
            attempt_started = self._monotonic()
            started_at = datetime.now(UTC).isoformat()
            try:
                raw = self._transport(
                    f"{self.config.base_url.rstrip('/')}/chat/completions",
                    headers,
                    payload,
                    self.config.timeout_seconds,
                )
                response = self._decode_response(raw, call_id, attempt, call_started)
                self._record(
                    LLMCallRecord(
                        call_id,
                        task_id,
                        trace_id,
                        parent_span_id,
                        self.name,
                        self.config.model,
                        attempt,
                        "completed",
                        (self._monotonic() - attempt_started) * 1000,
                        None,
                        started_at,
                        response.usage,
                    )
                )
                return response
            except LLMError as error:
                classified = error
            except Exception as error:
                classified = LLMTransportError(f"Unexpected model transport failure: {type(error).__name__}")
            self._record(
                LLMCallRecord(
                    call_id,
                    task_id,
                    trace_id,
                    parent_span_id,
                    self.name,
                    self.config.model,
                    attempt,
                    "failed",
                    (self._monotonic() - attempt_started) * 1000,
                    type(classified).__name__,
                    started_at,
                )
            )
            retryable = isinstance(classified, LLMTransientError)
            if not retryable or attempt == attempts:
                raise classified
            self._sleeper(self.config.retry_backoff_seconds * (2 ** (attempt - 1)))
        raise AssertionError("LLM retry loop ended unexpectedly")

    def _decode_response(
        self,
        raw: dict[str, Any],
        call_id: str,
        attempts: int,
        call_started: float,
    ) -> LLMResponse:
        try:
            choice = raw["choices"][0]
            finish_reason = choice["finish_reason"]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("DeepSeek response is missing choices/message/content") from exc
        if finish_reason == "length":
            raise LLMResponseError("DeepSeek JSON response was truncated at max_tokens")
        if finish_reason == "insufficient_system_resource":
            raise LLMTransientError("DeepSeek reported insufficient system resources")
        if finish_reason != "stop":
            raise LLMResponseError(f"DeepSeek returned unsupported finish_reason: {finish_reason}")
        if not isinstance(content, str) or not content.strip():
            raise LLMTransientError("DeepSeek returned empty JSON content")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMResponseError("DeepSeek returned malformed JSON content") from exc
        if not isinstance(data, dict):
            raise LLMResponseError("DeepSeek JSON response must be an object")
        usage_raw = raw.get("usage") or {}
        usage = LLMUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            total_tokens=int(usage_raw.get("total_tokens", 0)),
            prompt_cache_hit_tokens=int(usage_raw.get("prompt_cache_hit_tokens", 0)),
            prompt_cache_miss_tokens=int(usage_raw.get("prompt_cache_miss_tokens", 0)),
        )
        return LLMResponse(
            data=data,
            provider=self.name,
            model=str(raw.get("model") or self.config.model),
            finish_reason=finish_reason,
            usage=usage,
            call_id=call_id,
            attempts=attempts,
            latency_ms=(self._monotonic() - call_started) * 1000,
        )

    def _record(self, record: LLMCallRecord) -> None:
        if self._record_sink is None:
            return
        try:
            self._record_sink(record)
        except Exception as exc:
            self._sink_errors.append(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _http_transport(
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in {408, 429} or exc.code >= 500:
                raise LLMTransientError(f"DeepSeek HTTP {exc.code}") from exc
            raise LLMTransportError(f"DeepSeek HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMTransientError(f"DeepSeek network failure: {type(exc).__name__}") from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMResponseError("DeepSeek HTTP response was not JSON") from exc
        if not isinstance(value, dict):
            raise LLMResponseError("DeepSeek HTTP response must be a JSON object")
        return value
