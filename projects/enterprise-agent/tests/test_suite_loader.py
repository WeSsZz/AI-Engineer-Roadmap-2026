from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from enterprise_agent.benchmark import DEFAULT_SUITE_ROOT, default_cases, load_benchmark_cases
from enterprise_agent.evals import SuiteKind
from enterprise_agent.suite_loader import (
    SuiteValidationError,
    load_evaluation_suite,
    load_evaluation_suites,
)


class SuiteLoaderTests(unittest.TestCase):
    def test_default_cases_are_loaded_from_development_and_regression_suites(self) -> None:
        development = load_evaluation_suites(DEFAULT_SUITE_ROOT / "development")
        regression = load_evaluation_suites(DEFAULT_SUITE_ROOT / "regression")
        cases = default_cases()

        self.assertEqual(len(development), 1)
        self.assertEqual(development[0].kind, SuiteKind.CAPABILITY)
        self.assertEqual(regression[0].kind, SuiteKind.REGRESSION)
        self.assertEqual(len(cases), 5)
        self.assertEqual({case.suite_kind for case in cases}, {"capability", "regression"})
        self.assertTrue(all(case.metadata["human_reviewed"] for case in cases))
        self.assertTrue(all(case.metadata["provenance"] for case in cases))

    def test_jsonl_header_and_task_records_are_supported(self) -> None:
        header = {
            "record_type": "suite",
            "schema_version": 1,
            "suite_id": "jsonl-suite",
            "version": "1.0.0",
            "kind": "held_out",
            "metadata": {"dataset_version": "jsonl-v1"},
        }
        task = {
            "record_type": "task",
            "task_id": "jsonl-task",
            "objective": "diagnose",
            "initial_state": {"service": "api", "fault_rules": []},
            "success_criteria": {
                "outcome": {"status": "completed", "root_cause": "cause", "confidence": "high"},
                "evidence": {"required_types": []},
                "safety": {"require_safe_refusal": False},
                "recovery": {"required": False},
                "benchmark": {"expected_tools": []},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.jsonl"
            path.write_text(
                "\n".join((json.dumps(header), json.dumps(task))),
                encoding="utf-8",
            )
            suite = load_evaluation_suite(path)
            cases = load_benchmark_cases(path)

        self.assertEqual(suite.tasks[0].task_id, "jsonl-task")
        self.assertEqual(cases[0].metadata["dataset_version"], "jsonl-v1")

    def test_schema_validation_reports_file_and_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "suite_id": "invalid",
                        "version": "1",
                        "kind": "capability",
                        "tasks": [{"task_id": "missing-contracts"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                SuiteValidationError,
                r"invalid\.json: suite\.tasks\[0\] is missing fields",
            ):
                load_evaluation_suite(path)


if __name__ == "__main__":
    unittest.main()
