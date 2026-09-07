from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .benchmark import (
    BenchmarkBudget,
    BenchmarkProgress,
    BenchmarkRunner,
    ModelBenchmarkConfig,
    STRATEGIES,
    load_benchmark_cases,
    write_report,
)
from .corpus_ingest import (
    DEFAULT_PUBLIC_CORPUS_MANIFEST,
    CorpusBuildError,
    build_corpus_from_manifest,
)
from .faults import FaultInjector, FaultRule
from .harness import EnterpriseAgentHarness, TaskRunResult
from .llm import DeepSeekProvider, LLMConfig, LLMConfigurationError, LLMError, LLMProvider
from .memory import SQLiteMemoryStore
from .routing import ORCHESTRATION_MODES
from .retrieval import (
    DEFAULT_CORPUS_PATH,
    DEFAULT_RETRIEVAL_SUITE_PATH,
    SentenceTransformerEmbeddingProvider,
    build_bm25_index,
    build_dense_index,
    build_hybrid_index,
    evaluate_retrieval,
    load_corpus,
    load_retrieval_suite,
    write_retrieval_report,
)
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
    run_parser.add_argument("--tenant-id", default="demo", help="Trusted tenant scope for this task")
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
    memory_list_parser = subparsers.add_parser("memory-list", help="List active episodic memories")
    memory_list_parser.add_argument("--tenant-id", required=True)
    memory_list_parser.add_argument("--service")
    memory_list_parser.add_argument("--limit", type=int, default=20)
    memory_forget_parser = subparsers.add_parser("memory-forget", help="Soft-delete an episodic memory")
    memory_forget_parser.add_argument("memory_id")
    memory_forget_parser.add_argument("--tenant-id", required=True)
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
        help="Run the deterministic dense retrieval baseline",
    )
    retrieval_parser.add_argument("--corpus", default=str(DEFAULT_CORPUS_PATH))
    retrieval_parser.add_argument("--suite", default=str(DEFAULT_RETRIEVAL_SUITE_PATH))
    retrieval_parser.add_argument(
        "--strategy",
        choices=("dense", "bm25", "hybrid"),
        default="dense",
    )
    retrieval_parser.add_argument(
        "--embedding-model",
        help="Sentence Transformers model id for learned dense retrieval",
    )
    retrieval_parser.add_argument("--embedding-revision")
    retrieval_parser.add_argument(
        "--embedding-local-path",
        help="Required local model snapshot; its files are fingerprinted in the report",
    )
    retrieval_parser.add_argument("--embedding-device", help="Inference device, for example cuda or cpu")
    retrieval_parser.add_argument("--embedding-batch-size", type=int, default=32)
    retrieval_parser.add_argument("--top-k", type=int, default=5)
    retrieval_parser.add_argument("--output", default="work/retrieval-baseline.json")
    retrieval_parser.add_argument(
        "--include-result-text",
        action="store_true",
        help="Include retrieved source text in the JSON report (off by default)",
    )
    retrieval_parser.add_argument(
        "--omit-result-text",
        action="store_false",
        dest="include_result_text",
        help=argparse.SUPPRESS,
    )
    corpus_parser = subparsers.add_parser(
        "corpus-build",
        help="Build a corpus from allowlisted Markdown in pinned Git commits",
    )
    corpus_parser.add_argument(
        "--manifest",
        default=str(DEFAULT_PUBLIC_CORPUS_MANIFEST),
    )
    corpus_parser.add_argument("--cache-dir", default="work/corpus-cache")
    corpus_parser.add_argument("--output", default="work/corpora/public-ops-v1.json")
    corpus_parser.add_argument("--offline", action="store_true")
    corpus_parser.add_argument("--overwrite", action="store_true")
    smoke_parser = subparsers.add_parser("model-smoke", help="Make one live structured-output model call")
    smoke_parser.add_argument("--model", choices=("deepseek-v4-flash", "deepseek-v4-pro"))
    smoke_parser.add_argument("--thinking", choices=("enabled", "disabled"))
    return parser


def print_result(result: TaskRunResult, include_trace: bool) -> None:
    payload = {
        "task_id": result.task.task_id,
        "tenant_id": result.task.tenant_id,
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
        "episodic_memory_id": result.state.artifacts.get("episodic_memory_id"),
        "historical_memory_ids": [memory.memory_id for memory in result.historical_memories],
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
    if args.command == "memory-list":
        try:
            memories = SQLiteMemoryStore(args.db).list_for_tenant(
                args.tenant_id,
                service=args.service,
                limit=args.limit,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps([asdict(memory) for memory in memories], ensure_ascii=False, indent=2))
        return
    if args.command == "memory-forget":
        try:
            deleted = SQLiteMemoryStore(args.db).forget(
                args.memory_id,
                tenant_id=args.tenant_id,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {"memory_id": args.memory_id, "tenant_id": args.tenant_id, "deleted": deleted},
                ensure_ascii=False,
                indent=2,
            )
        )
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
    if args.command == "corpus-build":
        try:
            receipt = build_corpus_from_manifest(
                args.manifest,
                cache_dir=args.cache_dir,
                output=args.output,
                offline=args.offline,
                overwrite=args.overwrite,
            )
        except CorpusBuildError as exc:
            parser.error(str(exc))
        print(json.dumps(receipt.to_dict(), ensure_ascii=False, indent=2))
        return
    if args.command == "retrieval-benchmark":
        try:
            corpus_sha256 = _file_sha256(args.corpus)
            corpus_version, documents = load_corpus(args.corpus)
            if _file_sha256(args.corpus) != corpus_sha256:
                raise ValueError("corpus changed while it was being loaded")
            suite_sha256 = _file_sha256(args.suite)
            suite_version, cases = load_retrieval_suite(
                args.suite,
                expected_corpus=corpus_version,
                known_document_ids=(document.document_id for document in documents),
                expected_corpus_sha256=corpus_sha256,
            )
            if args.top_k <= 0:
                raise ValueError("--top-k must be positive")
            embedding_provider = None
            index_started = time.perf_counter()
            if args.embedding_model:
                if args.strategy == "bm25":
                    raise ValueError("--embedding-model cannot be used with --strategy bm25")
                if not args.embedding_revision:
                    raise ValueError(
                        "--embedding-revision is required with --embedding-model"
                    )
                if not args.embedding_local_path:
                    raise ValueError("--embedding-local-path is required with --embedding-model")
                embedding_provider = SentenceTransformerEmbeddingProvider(
                    args.embedding_model,
                    model_path=args.embedding_local_path,
                    revision=args.embedding_revision,
                    device=args.embedding_device,
                    batch_size=args.embedding_batch_size,
                )
            if args.strategy == "dense":
                index = build_dense_index(
                    args.corpus,
                    embedding_provider=embedding_provider,
                )
            elif args.strategy == "hybrid":
                index = build_hybrid_index(
                    args.corpus,
                    embedding_provider=embedding_provider,
                )
            else:
                index = build_bm25_index(args.corpus)
            index_build_seconds = round(time.perf_counter() - index_started, 6)
            report = evaluate_retrieval(
                index,
                cases,
                top_k=args.top_k,
            )
            report["corpus_version"] = corpus_version
            if _file_sha256(args.corpus) != corpus_sha256:
                raise ValueError("corpus changed during retrieval benchmark")
            if _file_sha256(args.suite) != suite_sha256:
                raise ValueError("retrieval suite changed during benchmark")
            report["index_build_seconds"] = index_build_seconds
            report["suite_version"] = suite_version
            source_manifest_hashes = sorted(
                {
                    str(document.metadata["manifest_sha256"])
                    for document in documents
                    if "manifest_sha256" in document.metadata
                }
            )
            report["corpus_sha256"] = corpus_sha256
            report["suite_sha256"] = suite_sha256
            report["source_manifest_sha256"] = source_manifest_hashes
            source_licenses: dict[str, dict[str, object]] = {}
            for document in documents:
                source_id = document.metadata.get("source_id")
                if not isinstance(source_id, str) or not source_id:
                    continue
                source_licenses[source_id] = {
                    "source_id": source_id,
                    "license": document.license,
                    "license_url": document.metadata.get("license_url"),
                    "attribution": document.metadata.get("attribution"),
                    "notice_url": document.metadata.get("notice_url"),
                    "modifications": document.metadata.get("modifications"),
                }
            report["source_licenses"] = [
                source_licenses[source_id]
                for source_id in sorted(source_licenses)
            ]
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        write_retrieval_report(
            report,
            args.output,
            include_result_text=args.include_result_text,
        )
        print(
            json.dumps(
                {
                    "output": args.output,
                    "corpus_version": corpus_version,
                    "suite_version": suite_version,
                    "case_count": report["case_count"],
                    "retrieval_method": report["retrieval_method"],
                    "embedding_provider": report["embedding_provider"],
                    "embedding_dimensions": report["embedding_dimensions"],
                    "embedding_device": report["embedding_device"],
                    "index_build_seconds": report["index_build_seconds"],
                    "index_version": report["index_version"],
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
                tenant_id=args.tenant_id,
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

def _file_sha256(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"cannot hash input file {path!r}: {exc}") from exc




def _build_llm_provider(args: argparse.Namespace) -> LLMProvider | None:
    if args.llm_provider == "deterministic":
        return None
    return DeepSeekProvider(LLMConfig.from_env(model=args.model, thinking=args.thinking))


if __name__ == "__main__":
    main()
