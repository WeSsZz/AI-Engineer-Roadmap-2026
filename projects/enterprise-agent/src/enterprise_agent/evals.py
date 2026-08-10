from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol, Sequence


class SuiteKind(str, Enum):
    """The reason a suite exists; splits are explicit instead of filename conventions."""

    CAPABILITY = "capability"
    REGRESSION = "regression"
    HELD_OUT = "held_out"
    SECURITY = "security"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class EvalTask:
    task_id: str
    objective: str
    initial_state: dict[str, Any]
    success_criteria: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TranscriptEvent:
    sequence: int
    kind: str
    actor: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Transcript:
    """Auditable execution record; events may reference external trace artifacts."""

    events: tuple[TranscriptEvent, ...] = ()
    trace_id: str | None = None
    artifact_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalOutcome:
    """Final environment truth, intentionally separate from the agent's answer."""

    status: str
    final_state: dict[str, Any]
    side_effects: tuple[dict[str, Any], ...] = ()
    artifacts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Grade:
    grader: str
    passed: bool
    score: float
    dimensions: dict[str, float] = field(default_factory=dict)
    violations: tuple[str, ...] = ()
    feedback: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("grade score must be between 0 and 1")
        invalid = {key: value for key, value in self.dimensions.items() if not 0.0 <= value <= 1.0}
        if invalid:
            raise ValueError("grade dimensions must be between 0 and 1")


class Grader(Protocol):
    name: str

    def grade(
        self,
        task: EvalTask,
        transcript: Transcript,
        outcome: EvalOutcome,
    ) -> Grade: ...


@dataclass(frozen=True)
class Trial:
    trial_id: str
    task_id: str
    repetition: int
    seed: int | None
    policy_version: str
    prompt_version: str | None
    model_version: str | None
    transcript: Transcript
    outcome: EvalOutcome
    grades: tuple[Grade, ...]

    @property
    def passed(self) -> bool:
        return bool(self.grades) and all(grade.passed for grade in self.grades)


@dataclass(frozen=True)
class EvaluationSuite:
    suite_id: str
    version: str
    kind: SuiteKind
    tasks: tuple[EvalTask, ...]
    provenance: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ids = [task.task_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation suite task ids must be unique")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        return payload


def reliability_summary(
    rows: Sequence[dict[str, Any]],
    *,
    consecutive_windows: tuple[int, ...] = (3,),
) -> dict[str, Any]:
    """Summarize repeatability without treating retries as extra chances to pass."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["case_id"]), []).append(row)

    case_reliability: dict[str, dict[str, Any]] = {}
    all_successful_cases = 0
    for case_id, case_rows in sorted(grouped.items()):
        ordered = sorted(case_rows, key=lambda row: int(row.get("repetition", 0)))
        successes = [bool(row["success"]) for row in ordered]
        longest_streak = _longest_true_streak(successes)
        all_trials_passed = bool(successes) and all(successes)
        all_successful_cases += int(all_trials_passed)
        case_reliability[case_id] = {
            "trial_count": len(successes),
            "success_rate": round(sum(successes) / len(successes), 4) if successes else 0.0,
            "all_trials_passed": all_trials_passed,
            "longest_success_streak": longest_streak,
        }

    window_rates = {
        f"pass_power_{window}": _consecutive_window_rate(grouped, window)
        for window in consecutive_windows
    }
    return {
        "pass_at_1": round(
            sum(bool(row["success"]) for row in rows) / len(rows),
            4,
        ) if rows else 0.0,
        **window_rates,
        "fully_reliable_case_rate": round(all_successful_cases / len(grouped), 4) if grouped else 0.0,
        "case_reliability": case_reliability,
    }


def _consecutive_window_rate(
    grouped: dict[str, list[dict[str, Any]]],
    window: int,
) -> float | None:
    if window <= 0:
        raise ValueError("consecutive reliability window must be positive")
    results: list[bool] = []
    for case_rows in grouped.values():
        ordered = sorted(case_rows, key=lambda row: int(row.get("repetition", 0)))
        successes = [bool(row["success"]) for row in ordered]
        results.extend(all(successes[index : index + window]) for index in range(len(successes) - window + 1))
    return round(sum(results) / len(results), 4) if results else None


def _longest_true_streak(values: Sequence[bool]) -> int:
    best = 0
    current = 0
    for value in values:
        current = current + 1 if value else 0
        best = max(best, current)
    return best
