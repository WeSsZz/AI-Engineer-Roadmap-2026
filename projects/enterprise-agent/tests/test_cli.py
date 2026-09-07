from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from enterprise_agent import __main__ as cli
from enterprise_agent.memory import EpisodicMemory, SQLiteMemoryStore


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
        learned = parser.parse_args(
            [
                "retrieval-benchmark",
                "--strategy",
                "hybrid",
                "--embedding-model",
                "BAAI/bge-m3",
                "--embedding-revision",
                "a" * 40,
                "--embedding-local-path",
                "work/models/bge-m3",
                "--embedding-device",
                "cuda",
                "--omit-result-text",
            ]
        )
        self.assertEqual(learned.embedding_model, "BAAI/bge-m3")
        self.assertEqual(learned.embedding_revision, "a" * 40)
        self.assertEqual(learned.embedding_device, "cuda")

        self.assertFalse(learned.include_result_text)
        self.assertEqual(learned.embedding_local_path, "work/models/bge-m3")

    def test_corpus_build_parser_exposes_reproducibility_controls(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "corpus-build",
                "--manifest",
                "manifest.json",
                "--cache-dir",
                "work/cache",
                "--output",
                "work/corpus.json",
                "--offline",
                "--overwrite",
            ]
        )
        self.assertEqual(args.manifest, "manifest.json")
        self.assertTrue(args.offline)
        self.assertTrue(args.overwrite)


    def test_run_and_memory_commands_require_explicit_tenant_scope(self) -> None:
        parser = cli.build_parser()
        run = parser.parse_args(["run", "--tenant-id", "tenant-a"])
        memory_list = parser.parse_args(["memory-list", "--tenant-id", "tenant-a"])
        memory_forget = parser.parse_args(
            ["memory-forget", "episode:task-1", "--tenant-id", "tenant-a"]
        )
        self.assertEqual(run.tenant_id, "tenant-a")
        self.assertEqual(memory_list.tenant_id, "tenant-a")
        self.assertEqual(memory_forget.tenant_id, "tenant-a")

    def test_memory_list_and_forget_respect_tenant_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.db"
            memory = EpisodicMemory.create(
                source_task_id="task-cli",
                source_checkpoint_version=3,
                tenant_id="tenant-a",
                service="payment-api",
                scenario="incident",
                summary="历史摘要",
                recommended_action="检查实时证据",
                root_cause="database_pool_exhaustion",
                confidence="high",
            )
            SQLiteMemoryStore(database).remember(memory)

            output = StringIO()
            argv = [
                "enterprise-agent",
                "--db",
                str(database),
                "memory-list",
                "--tenant-id",
                "tenant-a",
            ]
            with patch("sys.argv", argv), redirect_stdout(output), redirect_stderr(StringIO()):
                cli.main()
            listed = json.loads(output.getvalue())
            self.assertEqual([item["memory_id"] for item in listed], [memory.memory_id])

            output = StringIO()
            argv = [
                "enterprise-agent",
                "--db",
                str(database),
                "memory-forget",
                memory.memory_id,
                "--tenant-id",
                "tenant-a",
            ]
            with patch("sys.argv", argv), redirect_stdout(output), redirect_stderr(StringIO()):
                cli.main()
            self.assertTrue(json.loads(output.getvalue())["deleted"])
            self.assertEqual(SQLiteMemoryStore(database).list_for_tenant("tenant-a"), [])

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
        self.assertEqual(report["source_licenses"], [])

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
