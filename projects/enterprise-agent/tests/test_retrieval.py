from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from enterprise_agent.retrieval import (
    DEFAULT_CORPUS_PATH,
    build_bm25_index,
    build_dense_index,
    DenseKnowledgeIndex,
    DeterministicChunker,
    HashingEmbeddingProvider,
    KnowledgeDocument,
    build_default_dense_index,
    build_hybrid_index,
    evaluate_dense_retrieval,
    evaluate_retrieval,
    load_corpus,
    load_retrieval_suite,
    write_retrieval_report,
    _binary_ndcg,
)
from enterprise_agent.tools import build_demo_runtime


class RetrievalTests(unittest.TestCase):
    def test_default_corpus_preserves_provenance_and_versions(self) -> None:
        version, documents = load_corpus()
        self.assertEqual(version, "incident-runbooks:1.0.0")
        self.assertEqual(len(documents), 10)
        self.assertTrue(all(document.source_uri for document in documents))
        self.assertTrue(all(document.document_version for document in documents))
        self.assertTrue(all(document.license == "project-synthetic" for document in documents))

    def test_chunk_ids_are_stable_and_metadata_is_preserved(self) -> None:
        _, documents = load_corpus()
        chunker = DeterministicChunker(max_chars=120, overlap_chars=20)
        first = chunker.split(documents[0])
        second = chunker.split(documents[0])
        self.assertEqual(first, second)
        self.assertEqual(first[0].document_id, documents[0].document_id)
        self.assertEqual(first[0].metadata["service"], "payment-api")
        self.assertEqual(len(first[0].content_hash), 64)

    def test_dense_index_enforces_tenant_and_metadata_scope(self) -> None:
        index = build_default_dense_index()
        matches = index.search(
            "payment-api 数据库连接池耗尽",
            tenant_id="demo",
            filters={"service": "payment-api", "symptom": "database_pool_exhaustion"},
        )
        denied = index.search(
            "payment-api 数据库连接池耗尽",
            tenant_id="tenant-other",
        )
        self.assertEqual(matches[0].chunk.document_id, "runbook-payment-db-pool-v3")
        self.assertEqual(denied, ())
        self.assertIn("incident-runbooks:1.0.0", matches[0].index_version)

    def test_hashing_embedding_is_deterministic_and_normalized(self) -> None:
        provider = HashingEmbeddingProvider(dimensions=64)
        first = provider.embed("数据库连接池 DB_POOL_TIMEOUT")
        second = provider.embed("数据库连接池 DB_POOL_TIMEOUT")
        self.assertEqual(first, second)
        self.assertAlmostEqual(sum(value * value for value in first), 1.0)

    def test_dense_index_batches_corpus_embeddings_and_versions_custom_provider(self) -> None:
        class FakeBatchProvider:
            name = "learned-fake"
            dimensions = 2
            fingerprint = "learned-fake:model@revision-1"

            def __init__(self) -> None:
                self.batches: list[tuple[str, ...]] = []

            def embed(self, text: str) -> tuple[float, ...]:
                return (1.0, float(len(text) % 7 + 1))

            def embed_many(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
                values = tuple(texts)
                self.batches.append(values)
                return tuple(self.embed(text) for text in values)

        provider = FakeBatchProvider()
        index = build_dense_index(embedding_provider=provider)

        self.assertEqual(len(provider.batches), 1)
        self.assertGreater(len(provider.batches[0]), 1)
        self.assertIn("dense-", index.index_version)
        self.assertNotIn("dense-hashing-v1", index.index_version)

    def test_smoke_suite_has_thirty_cases_and_reports_honest_baseline(self) -> None:
        suite_version, cases = load_retrieval_suite()
        report = evaluate_dense_retrieval(build_default_dense_index(), cases, top_k=5)
        self.assertEqual(suite_version, "incident-retrieval-smoke:1.0.0")
        self.assertEqual(len(cases), 30)
        self.assertEqual(report["positive_case_count"], 26)
        self.assertEqual(report["negative_case_count"], 4)
        self.assertGreaterEqual(report["metrics"]["recall_at_5"], 0.9)
        self.assertLess(report["metrics"]["mrr"], 1.0)
        self.assertEqual(report["metrics"]["no_answer_accuracy"], 1.0)

    def test_bm25_and_hybrid_are_compared_on_the_same_suite(self) -> None:
        _, cases = load_retrieval_suite()
        dense = evaluate_retrieval(build_default_dense_index(), cases)
        bm25 = evaluate_retrieval(build_bm25_index(), cases)
        hybrid = evaluate_retrieval(build_hybrid_index(), cases)
        self.assertEqual(bm25["metrics"]["recall_at_5"], 1.0)
        self.assertGreater(bm25["metrics"]["mrr"], dense["metrics"]["mrr"])
        self.assertEqual(hybrid["metrics"]["recall_at_5"], 1.0)
        self.assertEqual(hybrid["retrieval_method"], "hybrid_rrf")
        self.assertTrue(hybrid["rows"][0]["results"][0]["component_scores"])

    def test_report_can_be_written_as_json(self) -> None:
        _, cases = load_retrieval_suite()
        report = evaluate_dense_retrieval(build_default_dense_index(), cases[:2])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "retrieval-report.json"
            write_retrieval_report(report, target)
            saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(saved["benchmark_version"], "retrieval-benchmark-v1")
        self.assertEqual(saved["case_count"], 2)

    def test_document_ndcg_does_not_count_duplicate_chunks_twice(self) -> None:
        self.assertEqual(_binary_ndcg(("doc-a", "doc-a", "doc-a"), {"doc-a"}), 1.0)

    def test_loader_rejects_unknown_fields(self) -> None:
        payload = json.loads(DEFAULT_CORPUS_PATH.read_text(encoding="utf-8"))
        payload["unexpected"] = True
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "invalid.json"
            target.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown"):
                load_corpus(target)

    def test_index_rejects_empty_queries(self) -> None:
        document = KnowledgeDocument(
            document_id="doc-1",
            title="Example",
            text="example text",
            source_uri="repo://example",
            source_type="runbook",
            tenant_id="demo",
            document_version="1",
            effective_at="2026-08-01T00:00:00Z",
            license="project-synthetic",
            provenance="test",
        )
        index = DenseKnowledgeIndex(DeterministicChunker().split(document))
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            index.search(" ", tenant_id="demo")

    def test_search_knowledge_runs_through_tool_gateway(self) -> None:
        runtime = build_demo_runtime()
        self.addCleanup(runtime.close)
        result = runtime.invoke(
            "search_knowledge",
            allowed_tools=("search_knowledge",),
            arguments={
                "query": "payment-api 数据库连接池耗尽",
                "tenant_id": "demo",
                "service": "payment-api",
                "symptom": "database_pool_exhaustion",
            },
        )
        self.assertEqual(result.data["count"], 1)
        self.assertEqual(result.evidence[0].evidence_id, "runbook-payment-db-pool-v3")
        self.assertTrue(result.evidence[0].metadata["untrusted_input"])
        self.assertIn("index_version", result.evidence[0].metadata)

    def test_get_runbook_is_a_compatibility_adapter_over_retrieval(self) -> None:
        runtime = build_demo_runtime()
        self.addCleanup(runtime.close)
        result = runtime.invoke(
            "get_runbook",
            allowed_tools=("get_runbook",),
            arguments={"service": "payment-api", "symptom": "database_pool_exhaustion"},
        )
        self.assertEqual(result.data["runbook"]["id"], "runbook-payment-db-pool-v3")
        self.assertIn("bm25-v1", result.data["runbook"]["index_version"])


if __name__ == "__main__":
    unittest.main()
