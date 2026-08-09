from __future__ import annotations

import argparse
import json

from .harness import EnterpriseAgentHarness, TaskRunResult
from .store import SQLiteEventStore


DEFAULT_OBJECTIVE = "分析华东区域本月订单下降原因，并在确认库存问题后生成补货工单"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable Enterprise Agent Harness demo")
    parser.add_argument("--db", default="work/enterprise_agent.db", help="SQLite checkpoint database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Start a new task")
    run_parser.add_argument("objective", nargs="?", default=DEFAULT_OBJECTIVE)
    run_parser.add_argument("--approve", action="store_true", help="Approve the generated high-risk action")
    run_parser.add_argument("--trace", action="store_true", help="Print the complete event trace")
    run_parser.add_argument("--failpoint", help="Interrupt after a checkpoint for recovery testing")

    resume_parser = subparsers.add_parser("resume", help="Resume a checkpointed task")
    resume_parser.add_argument("task_id")
    resume_parser.add_argument("--approve", action="store_true")
    resume_parser.add_argument("--trace", action="store_true")
    resume_parser.add_argument("--failpoint")

    subparsers.add_parser("list", help="List persisted tasks")
    return parser


def print_result(result: TaskRunResult, include_trace: bool) -> None:
    payload = {
        "task_id": result.task.task_id,
        "status": result.state.status.value,
        "checkpoint_version": result.state.version,
        "answer": result.answer,
        "recommendation": result.recommendation,
        "pending_action": result.state.artifacts.get("pending_action"),
        "ticket": result.state.artifacts.get("ticket"),
        "context_compacted": result.context_compacted,
    }
    if include_trace:
        payload["events"] = [event.__dict__ for event in result.events]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    args = build_parser().parse_args()
    store = SQLiteEventStore(args.db)
    if args.command == "list":
        print(json.dumps(store.list_tasks(), ensure_ascii=False, indent=2))
        return

    harness = EnterpriseAgentHarness(store=store)
    approval_token = "APPROVED_BY_USER" if args.approve else None
    try:
        if args.command == "run":
            result = harness.run(
                args.objective,
                approval_token=approval_token,
                failpoint=args.failpoint,
            )
        else:
            result = harness.resume(
                args.task_id,
                approval_token=approval_token,
                failpoint=args.failpoint,
            )
    finally:
        harness.close()
    print_result(result, include_trace=args.trace)


if __name__ == "__main__":
    main()
