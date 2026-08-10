from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from enterprise_agent import __main__ as cli


class CLITests(unittest.TestCase):
    def test_benchmark_does_not_initialize_the_task_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "benchmark.json"
            argv = [
                "enterprise-agent",
                "benchmark",
                "--strategies",
                "single_agent",
                "--output",
                str(output),
            ]
            with patch("sys.argv", argv), patch.object(
                cli,
                "SQLiteEventStore",
                side_effect=AssertionError("task store should be lazy"),
            ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                cli.main()
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["execution_mode"], "deterministic_synthetic_no_llm")

    def test_interrupted_benchmark_writes_partial_report_and_exits_130(self) -> None:
        partial = {
            "dataset_version": "incident-diagnosis-v1",
            "benchmark_version": "2.1",
            "execution_mode": "model_backed",
            "execution_order": "counterbalanced",
            "model": {"provider": "deepseek"},
            "budget": {"stopped_early": True, "stop_reason": "user_interrupt"},
            "aggregates": {},
            "paired_comparisons": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "partial.json"
            argv = ["enterprise-agent", "benchmark", "--output", str(output)]
            with patch("sys.argv", argv), patch.object(
                cli.BenchmarkRunner,
                "run",
                return_value=partial,
            ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.main()
            saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(raised.exception.code, 130)
        self.assertEqual(saved["budget"]["stop_reason"], "user_interrupt")


if __name__ == "__main__":
    unittest.main()
