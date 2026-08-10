from __future__ import annotations

import unittest

from enterprise_agent.evals import (
    EvalOutcome,
    EvalTask,
    EvaluationSuite,
    Grade,
    SuiteKind,
    Transcript,
    Trial,
    reliability_summary,
)


class EvalContractTests(unittest.TestCase):
    def test_suite_rejects_duplicate_task_ids(self) -> None:
        task = EvalTask("task-1", "diagnose", {}, {"status": "completed"})
        with self.assertRaises(ValueError):
            EvaluationSuite("suite", "1", SuiteKind.REGRESSION, (task, task))

    def test_trial_requires_every_grader_to_pass(self) -> None:
        trial = Trial(
            trial_id="trial-1",
            task_id="task-1",
            repetition=1,
            seed=7,
            policy_version="fixed-v1",
            prompt_version=None,
            model_version=None,
            transcript=Transcript(),
            outcome=EvalOutcome("completed", {}),
            grades=(
                Grade("outcome", True, 1.0),
                Grade("safety", False, 0.0, violations=("unauthorized_write",)),
            ),
        )
        self.assertFalse(trial.passed)

    def test_reliability_reports_consecutive_pass_power(self) -> None:
        rows = [
            {"case_id": "stable", "repetition": index + 1, "success": True}
            for index in range(4)
        ] + [
            {"case_id": "flaky", "repetition": index + 1, "success": success}
            for index, success in enumerate((True, True, False, True))
        ]
        summary = reliability_summary(rows)
        self.assertEqual(summary["pass_at_1"], 0.875)
        self.assertEqual(summary["pass_power_3"], 0.5)
        self.assertEqual(summary["fully_reliable_case_rate"], 0.5)
        self.assertEqual(summary["case_reliability"]["flaky"]["longest_success_streak"], 2)

    def test_grade_bounds_are_enforced(self) -> None:
        with self.assertRaises(ValueError):
            Grade("outcome", True, 1.1)


if __name__ == "__main__":
    unittest.main()
