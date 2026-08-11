from __future__ import annotations

import argparse
import json
import sys
import time

from .benchmark import (
    BenchmarkBudget,
    BenchmarkProgress,
    BenchmarkRunner,
    ModelBenchmarkConfig,
    STRATEGIES,
    load_benchmark_cases,
    write_report,
)
from .embeddings import (
    BGEM3EmbeddingProvider,
    EmbeddingProvider,
    SQLiteCachedEmbeddingProvider,
)
from .faults import FaultInjector, FaultRule
from .harness import EnterpriseAgentHarness, TaskRunResult
from .llm import DeepSeekProvider, LLMConfig, LLMConfigurationError, LLMError, LLMProvider
from .routing import ORCHESTRATION_MODES
from .retrieval import (
    DEFAULT_CORPUS_PATH,
    DEFAULT_RETRIEVAL_SUITE_PATH,
    build_bm25_index,
    build_dense_index,
    build_hybrid_index,
    evaluate_retrieval,
    HashingEmbeddingProvider,
    load_corpus,
    load_retrieval_suite,
    write_retrieval_report,
)
from .store import SQLiteEventStore


DEFAULT_OBJECTIVE = "诊断 payment-api 错误率和延迟突增的根因，并生成故障工单"
DEFAULT_BGE_M3_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


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
        "--orchestration",
        choices=ORCHESTRATION_MODES,
        default="fixed_multi",
    )
    _add_llm_arguments(run_parser)
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
    _add_llm_arguments(resume_parser)

    subparsers.add_parser("list", help="List persisted tasks")
    trace_parser = subparsers.add_parser("trace", help="Show persisted spans and latency metrics")
    trace_parser.add_argument("task_id")
    benchmark_parser = subparsers.add_parser("benchmark", help="Compare agent orchestration strategies")
    benchmark_parser.add_argument(
        "--strategies",
        nargs="+",
        choices=tuple(STRATEGIES),
        default=list(STRATEGIES),
    )
    benchmark_parser.add_argument("--repetitions", type=int, default=1)
    benchmark_parser.add_argument(
        "--execution-order",
        choices=("fixed", "counterbalanced"),
        default="counterbalanced",
    )
    benchmark_parser.add_argument("--output", default="work/benchmark-report.json")
    benchmark_parser.add_argument("--workspace", help="Keep isolated run databases in this directory")
    benchmark_parser.add_argument(
        "--suite",
        help="Load benchmark cases from a versioned JSON/JSONL suite file or directory",
    )
    _add_llm_arguments(benchmark_parser)
    benchmark_parser.add_argument("--max-model-calls", type=int, default=16)
    benchmark_parser.add_argument("--max-total-tokens", type=int, default=50_000)
    benchmark_parser.add_argument("--max-estimated-cost-usd", type=float, default=0.10)
    retrieval_parser = subparsers.add_parser(
        "retrieval-benchmark",
        help="Compare deterministic and learned retrieval providers",
    )
    retrieval_parser.add_argument("--corpus", default=str(DEFAULT_CORPUS_PATH))
    retrieval_parser.add_argument("--suite", default=str(DEFAULT_RETRIEVAL_SUITE_PATH))
    retrieval_parser.add_argument(
        "--strategy",
        choices=("dense", "bm25", "hybrid"),
        default="dense",
    )
    retrieval_parser.add_argument("--top-k", type=int, default=5)
    retrieval_parser.add_argument("--output", default="work/retrieval-baseline.json")
    retrieval_parser.add_argument(
        "--embedding-provider",
        choices=("hashing", "bge-m3"),
        default="hashing",
        help="Dense provider used by dense/hybrid strategies",
    )
    retrieval_parser.add_argument("--embedding-model", default="BAAI/bge-m3")
    retrieval_parser.add_argument(
        "--embedding-revision",
        default=DEFAULT_BGE_M3_REVISION,
        help="Pinned Hugging Face model revision",
    )
    retrieval_parser.add_argument("--embedding-batch-size", type=int, default=8)
    retrieval_parser.add_argument("--embedding-max-length", type=int, default=1024)
    retrieval_parser.add_argument("--embedding-device", default="cpu")
    retrieval_parser.add_argument(
        "--embedding-cache",
        default="work/retrieval-embeddings.sqlite3",
        help="Persistent exact-text embedding cache",
    )
    retrieval_parser.add_argument(
        "--model-cache",
        default="models/huggingface",
        help="Directory for pinned model snapshots",
    )
    retrieval_parser.add_argument(
        "--embedding-fp16",
        action="store_true",
        help="Use fp16 model weights when supported by the selected device",
    )
    smoke_parser = subparsers.add_parser("model-smoke", help="Make one live structured-output model call")
    smoke_parser.add_argument("--model", choices=("deepseek-v4-flash", "deepseek-v4-pro"))
    smoke_parser.add_argument("--thinking", choices=("enabled", "disabled"))
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
        "orchestration": result.task.metadata.get("orchestration", "fixed_multi"),
        "routing_decision": result.state.artifacts.get("routing_decision"),
        "outcome_class": result.state.artifacts.get("outcome_class"),
        "model_call": result.state.artifacts.get("model_call"),
        "model_error": result.state.artifacts.get("model_error"),
    }
    if include_trace:
        payload["events"] = [event.__dict__ for event in result.events]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "list":
        store = SQLiteEventStore(args.db)
        print(json.dumps(store.list_tasks(), ensure_ascii=False, indent=2))
        return
    if args.command == "trace":
        store = SQLiteEventStore(args.db)
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
    if args.command == "benchmark":
        try:
            model_config = None
            if args.llm_provider == "deepseek":
                model_config = ModelBenchmarkConfig.deepseek(
                    LLMConfig.from_env(model=args.model, thinking=args.thinking)
                )
            budget = BenchmarkBudget(
                max_model_calls=args.max_model_calls,
                max_total_tokens=args.max_total_tokens,
                max_estimated_cost_usd=args.max_estimated_cost_usd,
            )
            report = BenchmarkRunner(
                cases=load_benchmark_cases(args.suite) if args.suite else None,
                strategy_names=tuple(args.strategies),
                model_config=model_config,
                budget=budget,
                execution_order=args.execution_order,
            ).run(
                repetitions=args.repetitions,
                workspace=args.workspace,
                progress_callback=_print_benchmark_progress,
            )
        except (LLMConfigurationError, ValueError) as exc:
            parser.error(str(exc))
        write_report(report, args.output)
        print(
            json.dumps(
                {
                    "output": args.output,
                    "dataset_version": report["dataset_version"],
                    "benchmark_version": report["benchmark_version"],
                    "execution_mode": report["execution_mode"],
                    "execution_order": report["execution_order"],
                    "model": report["model"],
                    "budget": report["budget"],
                    "aggregates": report["aggregates"],
                    "paired_comparisons": report["paired_comparisons"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if report["budget"]["stop_reason"] == "user_interrupt":
            raise SystemExit(130)
        return
    if args.command == "retrieval-benchmark":
        try:
            corpus_version, _ = load_corpus(args.corpus)
            suite_version, cases = load_retrieval_suite(args.suite)
            if args.top_k <= 0:
                raise ValueError("--top-k must be positive")
            if args.strategy == "bm25" and args.embedding_provider != "hashing":
                raise ValueError(
                    "--embedding-provider applies only to dense or hybrid strategies"
                )
            setup_started = time.perf_counter()
            provider_started = time.perf_counter()
            provider = (
                _build_embedding_provider(args)
                if args.strategy in {"dense", "hybrid"}
                else None
            )
            provider_load_ms = round((time.perf_counter() - provider_started) * 1000, 3)
            if args.strategy == "dense":
                index = build_dense_index(args.corpus, embedding_provider=provider)
            elif args.strategy == "hybrid":
                index = build_hybrid_index(args.corpus, embedding_provider=provider)
            else:
                index = build_bm25_index(args.corpus)
            total_setup_ms = round((time.perf_counter() - setup_started) * 1000, 3)
            report = evaluate_retrieval(
                index,
                cases,
                top_k=args.top_k,
            )
            report["corpus_version"] = corpus_version
            report["suite_version"] = suite_version
            report["setup"] = {
                "provider_load_ms": provider_load_ms,
                "index_build_ms": index.index_build_ms,
                "total_setup_ms": total_setup_ms,
            }
        except ValueError as exc:
            parser.error(str(exc))
        write_retrieval_report(report, args.output)
        print(
            json.dumps(
                {
                    "output": args.output,
                    "corpus_version": corpus_version,
                    "suite_version": suite_version,
                    "case_count": report["case_count"],
                    "retrieval_method": report["retrieval_method"],
                    "embedding_provider": report["embedding_provider"],
                    "embedding": report["embedding"],
                    "index_version": report["index_version"],
                    "setup": report["setup"],
                    "metrics": report["metrics"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.command == "model-smoke":
        try:
            provider = DeepSeekProvider(LLMConfig.from_env(model=args.model, thinking=args.thinking))
            response = provider.complete_json(
                system_prompt='Return only a JSON object with schema {"status":"ok","message":"string"}.',
                user_prompt="Reply in JSON and confirm the model adapter is reachable.",
            )
        except LLMError as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "provider": response.provider,
                    "model": response.model,
                    "data": response.data,
                    "usage": response.usage.__dict__,
                    "latency_ms": round(response.latency_ms, 3),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    rules = tuple(getattr(args, "inject_fault", []))
    store = SQLiteEventStore(args.db)
    try:
        llm_provider = _build_llm_provider(args)
    except LLMConfigurationError as exc:
        parser.error(str(exc))
    harness = EnterpriseAgentHarness(
        store=store,
        fault_injector=FaultInjector(rules),
        llm_provider=llm_provider,
    )
    approval_token = "APPROVED_BY_USER" if args.approve else None
    try:
        if args.command == "run":
            result = harness.run(
                args.objective,
                approval_token=approval_token,
                failpoint=args.failpoint,
                scenario=args.scenario,
                orchestration=args.orchestration,
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


def _add_llm_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--llm-provider", choices=("deterministic", "deepseek"), default="deterministic")
    parser.add_argument("--model", choices=("deepseek-v4-flash", "deepseek-v4-pro"))
    parser.add_argument("--thinking", choices=("enabled", "disabled"))


def _print_benchmark_progress(progress: BenchmarkProgress) -> None:
    prefix = f"[benchmark {progress.run_index}/{progress.planned_run_count}]"
    target = f"r{progress.repetition}/{progress.repetitions} {progress.strategy}/{progress.case_id}"
    if progress.event == "started":
        message = f"{prefix} starting {target}"
    elif progress.event == "completed":
        message = (
            f"{prefix} completed {target} status={progress.status} "
            f"model_calls={progress.model_calls} tokens={progress.total_tokens} "
            f"cost=${progress.estimated_cost_usd:.6f} elapsed={progress.elapsed_seconds:.1f}s"
        )
    elif progress.event == "skipped":
        message = f"{prefix} skipped {target} reason={progress.reason}"
    else:
        message = (
            f"{prefix} interrupted {target}; saving partial report "
            f"completed={progress.completed_run_count} elapsed={progress.elapsed_seconds:.1f}s"
        )
    print(message, file=sys.stderr, flush=True)


def _build_llm_provider(args: argparse.Namespace) -> LLMProvider | None:
    if args.llm_provider == "deterministic":
        return None
    return DeepSeekProvider(LLMConfig.from_env(model=args.model, thinking=args.thinking))


def _build_embedding_provider(args: argparse.Namespace) -> EmbeddingProvider:
    if args.embedding_provider == "hashing":
        return HashingEmbeddingProvider()
    provider = BGEM3EmbeddingProvider(
        model_name=args.embedding_model,
        revision=args.embedding_revision,
        batch_size=args.embedding_batch_size,
        max_length=args.embedding_max_length,
        use_fp16=args.embedding_fp16,
        device=args.embedding_device,
        model_cache_dir=args.model_cache or None,
    )
    return SQLiteCachedEmbeddingProvider(provider, args.embedding_cache)


if __name__ == "__main__":
    main()
