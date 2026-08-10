from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .evals import EvalTask, EvaluationSuite, SuiteKind


SUITE_SCHEMA_VERSION = 1


class SuiteValidationError(ValueError):
    """Raised when a suite file does not satisfy the versioned data contract."""


def load_evaluation_suite(path: str | Path) -> EvaluationSuite:
    source = Path(path)
    try:
        payload = _read_payload(source)
        return evaluation_suite_from_dict(payload)
    except SuiteValidationError as exc:
        raise SuiteValidationError(f"{source}: {exc}") from exc


def load_evaluation_suites(path: str | Path) -> tuple[EvaluationSuite, ...]:
    """Load one suite file or every JSON/JSONL suite below a directory."""

    source = Path(path)
    files = (
        [source]
        if source.is_file()
        else sorted(
            candidate
            for candidate in source.rglob("*")
            if candidate.is_file() and candidate.suffix.lower() in {".json", ".jsonl"}
        )
    )
    if not files:
        raise SuiteValidationError(f"{source}: no JSON or JSONL suite files found")
    suites = tuple(load_evaluation_suite(file) for file in files)
    task_ids = [task.task_id for suite in suites for task in suite.tasks]
    duplicates = sorted({task_id for task_id in task_ids if task_ids.count(task_id) > 1})
    if duplicates:
        raise SuiteValidationError(
            f"{source}: duplicate task ids across suites: {', '.join(duplicates)}"
        )
    return suites


def evaluation_suite_from_dict(payload: dict[str, Any]) -> EvaluationSuite:
    _require_object(payload, "suite")
    _check_fields(
        payload,
        required={"schema_version", "suite_id", "version", "kind", "tasks"},
        optional={"provenance", "metadata"},
        location="suite",
    )
    schema_version = payload["schema_version"]
    if type(schema_version) is not int or schema_version != SUITE_SCHEMA_VERSION:
        raise SuiteValidationError(
            f"suite.schema_version must equal {SUITE_SCHEMA_VERSION}"
        )
    suite_id = _require_non_empty_string(payload["suite_id"], "suite.suite_id")
    version = _require_non_empty_string(payload["version"], "suite.version")
    try:
        kind = SuiteKind(_require_non_empty_string(payload["kind"], "suite.kind"))
    except ValueError as exc:
        allowed = ", ".join(kind.value for kind in SuiteKind)
        raise SuiteValidationError(f"suite.kind must be one of: {allowed}") from exc
    provenance = _string_tuple(payload.get("provenance", []), "suite.provenance")
    metadata = _require_object(payload.get("metadata", {}), "suite.metadata")
    raw_tasks = payload["tasks"]
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise SuiteValidationError("suite.tasks must be a non-empty array")
    tasks = tuple(_task_from_dict(raw, index) for index, raw in enumerate(raw_tasks))
    try:
        return EvaluationSuite(
            suite_id=suite_id,
            version=version,
            kind=kind,
            tasks=tasks,
            provenance=provenance,
            metadata=dict(metadata),
        )
    except ValueError as exc:
        raise SuiteValidationError(str(exc)) from exc


def _task_from_dict(payload: Any, index: int) -> EvalTask:
    location = f"suite.tasks[{index}]"
    raw = _require_object(payload, location)
    _check_fields(
        raw,
        required={"task_id", "objective", "initial_state", "success_criteria"},
        optional={"metadata"},
        location=location,
    )
    return EvalTask(
        task_id=_require_non_empty_string(raw["task_id"], f"{location}.task_id"),
        objective=_require_non_empty_string(raw["objective"], f"{location}.objective"),
        initial_state=dict(_require_object(raw["initial_state"], f"{location}.initial_state")),
        success_criteria=dict(
            _require_object(raw["success_criteria"], f"{location}.success_criteria")
        ),
        metadata=dict(_require_object(raw.get("metadata", {}), f"{location}.metadata")),
    )


def _read_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SuiteValidationError("suite file does not exist")
    try:
        if path.suffix.lower() == ".jsonl":
            return _read_jsonl(path)
        if path.suffix.lower() != ".json":
            raise SuiteValidationError("suite file extension must be .json or .jsonl")
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SuiteValidationError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    return dict(_require_object(value, "suite"))


def _read_jsonl(path: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SuiteValidationError(f"invalid JSONL record on line {line_number}: {exc.msg}") from exc
        records.append(dict(_require_object(record, f"line {line_number}")))
    if not records:
        raise SuiteValidationError("JSONL suite is empty")
    header = records[0]
    if header.get("record_type") != "suite":
        raise SuiteValidationError("first JSONL record must have record_type='suite'")
    payload = {key: value for key, value in header.items() if key != "record_type"}
    if "tasks" in payload:
        raise SuiteValidationError("JSONL suite header must not contain tasks")
    tasks = []
    for index, record in enumerate(records[1:], 2):
        if record.get("record_type") != "task":
            raise SuiteValidationError(f"JSONL line {index} must have record_type='task'")
        tasks.append({key: value for key, value in record.items() if key != "record_type"})
    payload["tasks"] = tasks
    return payload


def _check_fields(
    payload: dict[str, Any],
    *,
    required: set[str],
    optional: set[str],
    location: str,
) -> None:
    missing = sorted(required - payload.keys())
    unknown = sorted(payload.keys() - required - optional)
    if missing:
        raise SuiteValidationError(f"{location} is missing fields: {', '.join(missing)}")
    if unknown:
        raise SuiteValidationError(f"{location} has unknown fields: {', '.join(unknown)}")


def _require_object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SuiteValidationError(f"{location} must be an object")
    return value


def _require_non_empty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SuiteValidationError(f"{location} must be a non-empty string")
    return value


def _string_tuple(values: Any, location: str) -> tuple[str, ...]:
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise SuiteValidationError(f"{location} must be an array of non-empty strings")
    return tuple(values)
