from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

from enterprise_agent.retrieval import (
    BM25KnowledgeIndex,
    RetrievalCase,
    DEFAULT_CORPUS_PATH,
    build_bm25_index,
    DenseKnowledgeIndex,
    KnowledgeChunk,
    ReciprocalRankFusionIndex,
    RetrievalResult,
    DeterministicChunker,
    HashingEmbeddingProvider,
    SentenceTransformerEmbeddingProvider,
    KnowledgeDocument,
    build_default_dense_index,
    build_hybrid_index,
    evaluate_dense_retrieval,
    evaluate_retrieval,
    _indexable_text,
    load_corpus,

    load_retrieval_suite,
    write_retrieval_report,
)
from enterprise_agent.tools import build_demo_runtime

def _git_blob_id(raw: bytes) -> str:
    digest = hashlib.sha1()
    digest.update(f"blob {len(raw)}\0".encode("ascii"))
    digest.update(raw)
    return digest.hexdigest()



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

    def test_dense_index_batches_corpus_embeddings(self) -> None:
        class RecordingEmbeddingProvider:
            name = "recording-v1"
            dimensions = 2

            def __init__(self) -> None:
                self.batches: list[tuple[str, ...]] = []

            def embed(self, text: str) -> tuple[float, ...]:
                return (1.0, 0.0)

            def embed_many(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
                values = tuple(texts)
                self.batches.append(values)
                return tuple((1.0, 0.0) for _ in values)

        _, documents = load_corpus()
        chunks = DeterministicChunker().split(documents[0])
        provider = RecordingEmbeddingProvider()
        index = DenseKnowledgeIndex(
            chunks,
            embedding_provider=provider,
            index_version="test:recording-v1",
        )
        self.assertEqual(len(provider.batches), 1)
        self.assertEqual(len(provider.batches[0]), len(chunks))
        self.assertTrue(index.search("database pool", tenant_id="demo"))

    def test_sentence_transformer_provider_has_actionable_optional_dependency_error(self) -> None:
        with patch.dict("sys.modules", {"sentence_transformers": None}):
            with self.assertRaisesRegex(RuntimeError, "pip install -e"):
                SentenceTransformerEmbeddingProvider("example/model", revision="0" * 40)

    def test_sentence_transformer_local_path_keeps_versioned_model_identity(self) -> None:
        calls: dict[str, object] = {}

        revision = "a" * 40
        class FakeSentenceTransformer:
            device = "cuda:0"

            def __init__(self, target: str, *, revision: str, device: str | None) -> None:
                calls.update(target=target, revision=revision, requested_device=device)

            def get_sentence_embedding_dimension(self) -> int:
                return 3

            def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
                calls["encode_kwargs"] = kwargs
                return [[1.0, 0.0, 0.0] for _ in texts]

        module = SimpleNamespace(SentenceTransformer=FakeSentenceTransformer)
        with tempfile.TemporaryDirectory() as directory:
            model_directory = Path(directory)
            artifact = model_directory / "config.json"
            artifact.write_text("{}", encoding="utf-8")
            blob = artifact.read_bytes()
            tree_dir = model_directory / ".cache" / "huggingface" / "trees"
            tree_dir.mkdir(parents=True)
            tree = {
                "files": {
                    "config.json": {
                        "size": len(blob),
                        "blob_id": _git_blob_id(blob),
                    }
                }
            }
            (tree_dir / f"{revision}.json").write_text(
                json.dumps(tree), encoding="utf-8"
            )
            with patch.dict("sys.modules", {"sentence_transformers": module}):
                provider = SentenceTransformerEmbeddingProvider(
                    "BAAI/bge-m3",
                    model_path=model_directory,
                    revision=revision,
                    device="cuda",
                )
            self.assertEqual(calls["target"], str(model_directory))
        self.assertEqual(
            provider.name,
            f"sentence-transformers:BAAI/bge-m3@{revision}#sha256:{provider.artifact_sha256}",
        )
        self.assertEqual(provider.device, "cuda:0")
        self.assertEqual(provider.artifact_file_count, 1)
        self.assertEqual(provider.embed_many(("one", "two")), ((1.0, 0.0, 0.0),) * 2)

    def test_smoke_suite_has_thirty_cases_and_reports_honest_baseline(self) -> None:
        suite_version, cases = load_retrieval_suite()
        report = evaluate_dense_retrieval(build_default_dense_index(), cases, top_k=5)
        self.assertEqual(suite_version, "incident-retrieval-smoke:1.0.0")
        self.assertEqual(len(cases), 30)
        self.assertEqual(report["positive_case_count"], 26)
        self.assertEqual(report["negative_case_count"], 4)
        self.assertGreaterEqual(report["metrics"]["recall_at_5"], 0.9)
        self.assertLess(report["metrics"]["mrr"], 1.0)
        self.assertEqual(report["scope_case_count"], 4)
        self.assertEqual(report["semantic_no_answer_case_count"], 0)
        self.assertEqual(report["metrics"]["scope_isolation_accuracy"], 1.0)
        self.assertIsNone(report["metrics"]["semantic_no_answer_accuracy"])

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

    def test_hybrid_fuses_document_ranks_instead_of_duplicate_chunks(self) -> None:
        def chunk(chunk_id: str, document_id: str) -> KnowledgeChunk:
            return KnowledgeChunk(
                chunk_id=chunk_id,
                document_id=document_id,
                title=document_id,
                text=f"{document_id} diagnostic text",
                section_path="root",
                source_uri=f"repo://{document_id}",
                source_type="documentation",
                tenant_id="demo",
                document_version="1",
                content_hash=chunk_id.ljust(64, "0"),
            )

        chunks = (chunk("a1", "doc-a"), chunk("a2", "doc-a"), chunk("b1", "doc-b"))

        class StaticIndex:
            def __init__(self, ordered_chunks: tuple[KnowledgeChunk, ...]) -> None:
                self.chunks = chunks
                self.embedding_provider = HashingEmbeddingProvider(dimensions=8)
                self.results = tuple(
                    RetrievalResult(
                        chunk=value,
                        score=float(len(ordered_chunks) - rank),
                        rank=rank,
                        embedding_provider=self.embedding_provider.name,
                        index_version="static",
                    )
                    for rank, value in enumerate(ordered_chunks, start=1)
                )

            def search(self, *args: object, top_k: int, **kwargs: object) -> tuple[RetrievalResult, ...]:
                return self.results[:top_k]

        dense = StaticIndex((chunks[0], chunks[1], chunks[2]))
        bm25 = StaticIndex((chunks[2], chunks[1], chunks[0]))
        hybrid = ReciprocalRankFusionIndex(dense, bm25, index_version="test:hybrid")
        results = hybrid.search("diagnostic", tenant_id="demo", top_k=2)

        self.assertEqual(
            [result.chunk.document_id for result in results],
            ["doc-a", "doc-b"],
        )
        self.assertTrue(
            all(
                {"dense_rank", "bm25_rank"}.issubset(result.component_scores)
                for result in results
            )
        )

    def test_document_metrics_count_each_relevant_document_once(self) -> None:
        long_document = KnowledgeDocument(
            document_id="long-guide",
            title="Long Guide",
            text="# Diagnosis\n" + ("needle diagnostic procedure " * 40),
            source_uri="repo://long-guide",
            source_type="documentation",
            tenant_id="demo",
            document_version="1",
            effective_at="2026-08-01T00:00:00Z",
            license="project-synthetic",
            provenance="test",
        )
        chunks = DeterministicChunker(max_chars=80, overlap_chars=10).split(long_document)
        self.assertGreater(len(chunks), 5)
        index = BM25KnowledgeIndex(chunks, index_version="test:bm25")
        report = evaluate_retrieval(
            index,
            (
                RetrievalCase(
                    case_id="unique-documents",
                    query="needle diagnostic procedure",
                    tenant_id="demo",
                    relevant_document_ids=("long-guide",),
                ),
            ),
        )
        retrieved = report["rows"][0]["retrieved_document_ids"]
        self.assertEqual(len(retrieved), len(set(retrieved)))
        self.assertEqual(report["metrics"]["ndcg_at_5"], 1.0)

    def test_indexed_text_includes_heading_but_excludes_audit_metadata(self) -> None:
        chunk = KnowledgeChunk(
            chunk_id="guide:1",
            document_id="guide",
            title="Guide title",
            text="Body text",
            section_path="Needle heading",
            source_uri="repo://guide",
            source_type="documentation",
            tenant_id="demo",
            document_version="1",
            content_hash="abc",
            metadata={"audit_only_token": "must-not-enter-the-index"},
        )
        chunk.metadata["service"] = "searchable-service"

        self.assertEqual(
            _indexable_text(chunk),
            "Guide title\nNeedle heading\nBody text\nsearchable-service",
        )
        self.assertNotIn("must-not-enter-the-index", _indexable_text(chunk))



    def test_report_can_be_written_with_or_without_source_text(self) -> None:
        _, cases = load_retrieval_suite()
        report = evaluate_dense_retrieval(build_default_dense_index(), cases[:2])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "retrieval-report.json"
            redacted_target = Path(directory) / "retrieval-report-redacted.json"
            write_retrieval_report(report, target)
            write_retrieval_report(
                report,
                redacted_target,
                include_result_text=False,
            )
            saved = json.loads(target.read_text(encoding="utf-8"))
            redacted = json.loads(redacted_target.read_text(encoding="utf-8"))
        self.assertEqual(saved["benchmark_version"], "retrieval-benchmark-v2")
        self.assertEqual(saved["case_count"], 2)
        self.assertTrue(saved["result_text_included"])
        self.assertFalse(redacted["result_text_included"])
        self.assertTrue(
            all(
                "text" not in result
                for row in redacted["rows"]
                for result in row["results"]
            )
        )
        self.assertIn("text", report["rows"][0]["results"][0])

    def test_loader_rejects_unknown_fields(self) -> None:
        payload = json.loads(DEFAULT_CORPUS_PATH.read_text(encoding="utf-8"))
        payload["unexpected"] = True
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "invalid.json"
            target.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown"):
                load_corpus(target)

    def test_suite_must_match_loaded_corpus_and_known_documents(self) -> None:
        with self.assertRaisesRegex(ValueError, "targets .*loaded corpus"):
            load_retrieval_suite(expected_corpus="another-corpus:1.0.0")
        with self.assertRaisesRegex(ValueError, "unknown document_id"):
            load_retrieval_suite(known_document_ids={"not-a-real-document"})
        with self.assertRaisesRegex(ValueError, "corpus_sha256"):
            load_retrieval_suite(expected_corpus_sha256="0" * 64)


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
        self.assertIn("bm25-content-v2", result.data["runbook"]["index_version"])


if __name__ == "__main__":
    unittest.main()
