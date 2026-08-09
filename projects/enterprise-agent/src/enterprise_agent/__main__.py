from __future__ import annotations

import argparse
import json

from .faults import FaultInjector, FaultRule
from .harness import EnterpriseAgentHarness, TaskRunResult
from .store import SQLiteEventStore


DEFAULT_OBJECTIVE = "诊断 payment-api 错误率和延迟突增的根因，并生成故障工单"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable Enterprise Agent Harness demo")
    parser.add_argument("--db", default="work/enterprise_agent.db", help="SQLite checkpoint database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Start a new task")
    run_parser.add_argument("objective", nargs="?", default=DEFAULT_OBJECTIVE)
    run_parser.add_argument("--approve", action="store_true", help="Approve the generated high-risk action")
    run_parser.add_argument("--trace", action="store_true", help="Print the complete event trace")
    run_parser.add_argument("--failpoint", help="Interrupt after a checkpoint for recovery testing")
    run_parser.add_argument("--scenario", choices=("business", "incident"))
    run_parser.add_argument(
        "--inject-fault",
        action="append",
        type=_parse_fault,
        default=[],
        metavar="TOOL:MODE[:TIMES]",
        help="Inject transient, permanent or timeout failures",
    )

    resume_parser = subparsers.add_parser("resume", help="Resume a checkpointed task")
    resume_parser.add_argument("task_id")
    resume_parser.add_argument("--approve", action="store_true")
    resume_parser.add_argument("--trace", action="store_true")
    resume_parser.add_argument("--failpoint")

    subparsers.add_parser("list", help="List persisted tasks")
    trace_parser = subparsers.add_parser("trace", help="Show persisted spans and latency metrics")
    trace_parser.add_argument("task_id")
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
        "root_cause_confidence": result.state.artifacts.get("root_cause_confidence"),
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
    if args.command == "trace":
        spans = store.list_spans(args.task_id)
        payload = {
            "task_id": args.task_id,
            "summary": store.trace_summary(args.task_id),
            "spans": [
                {
                    **span.__dict__,
                    "kind": span.kind.value,
                    "status": span.status.value,
                }
                for span in spans
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    rules = tuple(getattr(args, "inject_fault", []))
    harness = EnterpriseAgentHarness(store=store, fault_injector=FaultInjector(rules))
    approval_token = "APPROVED_BY_USER" if args.approve else None
    try:
        if args.command == "run":
            result = harness.run(
                args.objective,
                approval_token=approval_token,
                failpoint=args.failpoint,
                scenario=args.scenario,
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


def _parse_fault(value: str) -> FaultRule:
    parts = value.split(":")
    if len(parts) not in {2, 3}:
        raise argparse.ArgumentTypeError("fault must be TOOL:MODE[:TIMES]")
    try:
        times = int(parts[2]) if len(parts) == 3 else 1
        return FaultRule(parts[0], parts[1], times=times)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


if __name__ == "__main__":
    main()
