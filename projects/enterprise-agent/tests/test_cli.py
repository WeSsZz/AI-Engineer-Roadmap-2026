from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
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
            ), redirect_stdout(StringIO()):
                cli.main()
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["execution_mode"], "deterministic_synthetic_no_llm")


if __name__ == "__main__":
    unittest.main()
