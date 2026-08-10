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
    def test_run_parser_exposes_adaptive_and_shadow_orchestration(self) -> None:
        parser = cli.build_parser()
        active = parser.parse_args(["run", "--orchestration", "adaptive"])
        shadow = parser.parse_args(["run", "--orchestration", "adaptive_shadow"])
        self.assertEqual(active.orchestration, "adaptive")
        self.assertEqual(shadow.orchestration, "adaptive_shadow")

    def test_benchmark_parser_accepts_external_suite_path(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["benchmark", "--suite", "evals/regression"])
        self.assertEqual(args.suite, "evals/regression")

    def test_retrieval_parser_exposes_all_baseline_strategies(self) -> None:
        parser = cli.build_parser()
        for strategy in ("dense", "bm25", "hybrid"):
            args = parser.parse_args(["retrieval-benchmark", "--strategy", strategy])
            self.assertEqual(args.strategy, strategy)

    def test_retrieval_benchmark_writes_versioned_report_without_task_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "retrieval.json"
            argv = ["enterprise-agent", "retrieval-benchmark", "--output", str(output)]
            with patch("sys.argv", argv), patch.object(
                cli,
                "SQLiteEventStore",
                side_effect=AssertionError("task store should be lazy"),
            ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                cli.main()
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["corpus_version"], "incident-runbooks:1.0.0")
        self.assertEqual(report["suite_version"], "incident-retrieval-smoke:1.0.0")
        self.assertEqual(report["case_count"], 30)

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
            "dataset_version": "incident-diagnosis-v2",
            "benchmark_version": "4.0",
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
