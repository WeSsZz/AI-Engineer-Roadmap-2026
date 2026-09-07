from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence


PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_CORPUS_PATH = PACKAGE_ROOT / "corpora" / "incident-runbooks-v1.json"
DEFAULT_RETRIEVAL_SUITE_PATH = PACKAGE_ROOT / "retrieval_suites" / "incident-retrieval-smoke-v1.json"


@dataclass(frozen=True)
class KnowledgeDocument:
    document_id: str
    title: str
    text: str
    source_uri: str
    source_type: str
    tenant_id: str
    document_version: str
    effective_at: str
    license: str
    provenance: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    title: str
    text: str
    section_path: str
    source_uri: str
    source_type: str
    tenant_id: str
    document_version: str
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalResult:
    chunk: KnowledgeChunk
    score: float
    rank: int
    embedding_provider: str | None
    index_version: str
    retrieval_method: str = "dense"
    component_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk.chunk_id,
            "document_id": self.chunk.document_id,
            "title": self.chunk.title,
            "text": self.chunk.text,
            "section_path": self.chunk.section_path,
            "source_uri": self.chunk.source_uri,
            "source_type": self.chunk.source_type,
            "tenant_id": self.chunk.tenant_id,
            "document_version": self.chunk.document_version,
            "content_hash": self.chunk.content_hash,
            "metadata": self.chunk.metadata,
            "score": self.score,
            "rank": self.rank,
            "embedding_provider": self.embedding_provider,
            "index_version": self.index_version,
            "retrieval_method": self.retrieval_method,
            "component_scores": self.component_scores,
        }


class EmbeddingProvider(Protocol):
    name: str
    dimensions: int

    def embed(self, text: str) -> tuple[float, ...]: ...
    def embed_many(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


class HashingEmbeddingProvider:
    """Dependency-free deterministic baseline, not a learned semantic model."""

    name = "hashing-char-ngram-v1"

    def __init__(self, dimensions: int = 512):
        if dimensions <= 0:
            raise ValueError("embedding dimensions must be positive")
        self.dimensions = dimensions

    def embed(self, text: str) -> tuple[float, ...]:
        features = _text_features(text)
        vector = [0.0] * self.dimensions
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return tuple(vector)

    def embed_many(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self.embed(text) for text in texts)


class SentenceTransformerEmbeddingProvider:
    """Optional learned embedding provider backed by sentence-transformers."""

    def __init__(
        self,
        model_name: str,
        *,
        model_path: str | Path | None = None,
        revision: str,
        device: str | None = None,
        batch_size: int = 32,
    ) -> None:
        if not model_name.strip():
            raise ValueError("embedding model name must not be empty")
        if not isinstance(revision, str):
            raise ValueError("embedding model revision is required")
        revision = revision.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
            raise ValueError(
                "embedding model revision must be an immutable 40- or 64-character hex commit"
            )
        if batch_size <= 0:
            raise ValueError("embedding batch size must be positive")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "learned embeddings require the optional 'rag' dependencies; "
                "install with 'pip install -e .[rag]'"
            ) from exc

        if model_path is None:
            raise ValueError(
                "learned embedding benchmarks require --embedding-local-path"
            )
        local_path = Path(model_path)
        if not local_path.is_dir():
            raise ValueError(f"embedding model directory does not exist: {local_path}")
        artifact_sha256, artifact_files = _directory_fingerprint(local_path, revision)
        load_target = str(local_path)
        self.model_name = model_name
        self.revision = revision
        self.batch_size = batch_size
        self.name = f"sentence-transformers:{model_name}@{revision}#sha256:{artifact_sha256}"
        self.artifact_sha256 = artifact_sha256
        self.artifact_file_count = artifact_files
        try:
            self._model = SentenceTransformer(
                load_target,
                revision=revision,
                device=device,
            )
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                f"failed to load embedding model {model_name!r} from {load_target!r}: {exc}"
            ) from exc
        self.device = str(self._model.device)
        dimension_getter = getattr(self._model, "get_embedding_dimension", None)
        if dimension_getter is None:
            dimension_getter = self._model.get_sentence_embedding_dimension
        dimensions = dimension_getter()
        if not isinstance(dimensions, int) or dimensions <= 0:
            raise RuntimeError(f"embedding model {model_name!r} did not report valid dimensions")
        self.dimensions = dimensions

    def embed(self, text: str) -> tuple[float, ...]:
        return self.embed_many((text,))[0]

    def embed_many(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        values = tuple(texts)
        if not values:
            return ()
        encoded = self._model.encode(
            list(values),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        vectors = tuple(tuple(float(value) for value in vector) for vector in encoded)
        if len(vectors) != len(values):
            raise RuntimeError(
                f"embedding model returned {len(vectors)} vectors for {len(values)} texts"
            )
        if any(len(vector) != self.dimensions for vector in vectors):
            raise RuntimeError("embedding model returned an unexpected vector dimension")
        return vectors


class DeterministicChunker:
    def __init__(self, max_chars: int = 800, overlap_chars: int = 80):
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        if overlap_chars < 0 or overlap_chars >= max_chars:
            raise ValueError("overlap_chars must be non-negative and smaller than max_chars")
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars

    def split(self, document: KnowledgeDocument) -> tuple[KnowledgeChunk, ...]:
        sections = _markdown_sections(document.text)
        chunks: list[KnowledgeChunk] = []
        for section_path, section_text in sections:
            for offset, value in _windows(section_text, self.max_chars, self.overlap_chars):
                content_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
                chunk_id = (
                    document.document_id
                    if len(sections) == 1 and offset == 0 and len(value) == len(section_text)
                    else f"{document.document_id}:{content_hash[:12]}"
                )
                chunks.append(
                    KnowledgeChunk(
                        chunk_id=chunk_id,
                        document_id=document.document_id,
                        title=document.title,
                        text=value,
                        section_path=section_path,
                        source_uri=document.source_uri,
                        source_type=document.source_type,
                        tenant_id=document.tenant_id,
                        document_version=document.document_version,
                        content_hash=content_hash,
                        metadata={
                            **document.metadata,
                            "effective_at": document.effective_at,
                            "license": document.license,
                            "provenance": document.provenance,
                        },
                    )
                )
        return tuple(chunks)


class DenseKnowledgeIndex:
    retrieval_method = "dense"

    def __init__(
        self,
        chunks: Iterable[KnowledgeChunk],
        *,
        embedding_provider: EmbeddingProvider | None = None,
        index_version: str = "dense-content-v2",
    ):
        self.embedding_provider = embedding_provider or HashingEmbeddingProvider()
        self.index_version = index_version
        self.chunks = tuple(chunks)
        if not self.chunks:
            raise ValueError("knowledge index requires at least one chunk")
        self._vectors = self.embedding_provider.embed_many(
            tuple(_indexable_text(chunk) for chunk in self.chunks)
        )

    def search(
        self,
        query: str,
        *,
        tenant_id: str,
        top_k: int = 3,
        filters: dict[str, str] | None = None,
        min_score: float = 0.0,
    ) -> tuple[RetrievalResult, ...]:
        if not query.strip():
            raise ValueError("retrieval query must not be empty")
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        query_vector = self.embedding_provider.embed(query)
        ranked: list[tuple[float, KnowledgeChunk]] = []
        for chunk, vector in zip(self.chunks, self._vectors, strict=True):
            if chunk.tenant_id != tenant_id:
                continue
            if filters and any(str(chunk.metadata.get(key)) != value for key, value in filters.items()):
                continue
            score = sum(left * right for left, right in zip(query_vector, vector, strict=True))
            if score > min_score:
                ranked.append((score, chunk))
        ranked.sort(key=lambda item: (-item[0], item[1].chunk_id))
        return tuple(
            RetrievalResult(
                chunk=chunk,
                score=round(score, 6),
                rank=rank,
                embedding_provider=self.embedding_provider.name,
                index_version=self.index_version,
                retrieval_method=self.retrieval_method,
                component_scores={"dense": round(score, 6)},
            )
            for rank, (score, chunk) in enumerate(ranked[:top_k], start=1)
        )


class BM25KnowledgeIndex:
    retrieval_method = "bm25"
    embedding_provider = None

    def __init__(
        self,
        chunks: Iterable[KnowledgeChunk],
        *,
        index_version: str = "bm25-content-v2",
        k1: float = 1.5,
        b: float = 0.75,
    ):
        self.chunks = tuple(chunks)
        if not self.chunks:
            raise ValueError("knowledge index requires at least one chunk")
        if k1 <= 0 or not 0 <= b <= 1:
            raise ValueError("invalid BM25 parameters")
        self.index_version = index_version
        self.k1 = k1
        self.b = b
        self._terms = tuple(_text_features(_indexable_text(chunk)) for chunk in self.chunks)
        self._lengths = tuple(len(terms) for terms in self._terms)
        self._average_length = sum(self._lengths) / len(self._lengths)
        self._document_frequency: dict[str, int] = {}
        for terms in self._terms:
            for term in set(terms):
                self._document_frequency[term] = self._document_frequency.get(term, 0) + 1

    def search(
        self,
        query: str,
        *,
        tenant_id: str,
        top_k: int = 3,
        filters: dict[str, str] | None = None,
        min_score: float = 0.0,
    ) -> tuple[RetrievalResult, ...]:
        _validate_search(query, tenant_id, top_k)
        query_terms = _text_features(query)
        ranked: list[tuple[float, KnowledgeChunk]] = []
        document_count = len(self.chunks)
        for chunk, terms, length in zip(self.chunks, self._terms, self._lengths, strict=True):
            if not _matches_scope(chunk, tenant_id, filters):
                continue
            frequencies: dict[str, int] = {}
            for term in terms:
                frequencies[term] = frequencies.get(term, 0) + 1
            score = 0.0
            for term in query_terms:
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                document_frequency = self._document_frequency.get(term, 0)
                inverse_document_frequency = math.log(
                    1 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + self.k1 * (
                    1 - self.b + self.b * length / self._average_length
                )
                score += inverse_document_frequency * frequency * (self.k1 + 1) / denominator
            if score > min_score:
                ranked.append((score, chunk))
        ranked.sort(key=lambda item: (-item[0], item[1].chunk_id))
        return tuple(
            RetrievalResult(
                chunk=chunk,
                score=round(score, 6),
                rank=rank,
                embedding_provider=None,
                index_version=self.index_version,
                retrieval_method=self.retrieval_method,
                component_scores={"bm25": round(score, 6)},
            )
            for rank, (score, chunk) in enumerate(ranked[:top_k], start=1)
        )


class ReciprocalRankFusionIndex:
    retrieval_method = "hybrid_rrf"

    def __init__(
        self,
        dense: DenseKnowledgeIndex,
        bm25: BM25KnowledgeIndex,
        *,
        rrf_k: int = 60,
        index_version: str = "hybrid-document-rrf-v2",
    ):
        if rrf_k <= 0:
            raise ValueError("rrf_k must be positive")
        if tuple(chunk.chunk_id for chunk in dense.chunks) != tuple(chunk.chunk_id for chunk in bm25.chunks):
            raise ValueError("hybrid indexes must contain the same chunks")
        self.dense = dense
        self.bm25 = bm25
        self.embedding_provider = dense.embedding_provider
        self.index_version = index_version
        self.rrf_k = rrf_k
        self.chunks = dense.chunks

    def search(
        self,
        query: str,
        *,
        tenant_id: str,
        top_k: int = 3,
        filters: dict[str, str] | None = None,
        min_score: float = 0.0,
    ) -> tuple[RetrievalResult, ...]:
        _validate_search(query, tenant_id, top_k)
        candidate_k = max(top_k * 4, 20)
        component_top_k = len(self.chunks)
        dense_results = self.dense.search(
            query,
            tenant_id=tenant_id,
            top_k=component_top_k,
            filters=filters,
            min_score=min_score,
        )
        bm25_results = self.bm25.search(
            query,
            tenant_id=tenant_id,
            top_k=component_top_k,
            filters=filters,
            min_score=min_score,
        )
        documents: dict[str, KnowledgeChunk] = {}
        representative_ranks: dict[str, int] = {}
        scores: dict[str, float] = {}
        component_ranks: dict[str, dict[str, float]] = {}
        for method, results in (("dense", dense_results), ("bm25", bm25_results)):
            unique_results = _unique_document_results(results, top_k=candidate_k)
            for document_rank, result in enumerate(unique_results, start=1):
                document_id = result.chunk.document_id
                if document_rank < representative_ranks.get(document_id, math.inf):
                    documents[document_id] = result.chunk
                    representative_ranks[document_id] = document_rank
                scores[document_id] = (
                    scores.get(document_id, 0.0)
                    + 1.0 / (self.rrf_k + document_rank)
                )
                component_ranks.setdefault(document_id, {})[
                    f"{method}_rank"
                ] = float(document_rank)
                component_ranks[document_id][f"{method}_score"] = result.score
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        return tuple(
            RetrievalResult(
                chunk=documents[document_id],
                score=round(score, 6),
                rank=rank,
                embedding_provider=self.embedding_provider.name,
                index_version=self.index_version,
                retrieval_method=self.retrieval_method,
                component_scores=component_ranks[document_id],
            )
            for rank, (document_id, score) in enumerate(ranked, start=1)
        )


@dataclass(frozen=True)
class RetrievalCase:
    case_id: str
    query: str
    tenant_id: str
    relevant_document_ids: tuple[str, ...]
    filters: dict[str, str] = field(default_factory=dict)


def load_corpus(path: str | Path = DEFAULT_CORPUS_PATH) -> tuple[str, tuple[KnowledgeDocument, ...]]:
    payload = _load_json(path)
    _require_exact_keys(payload, {"corpus_id", "version", "documents"}, "corpus")
    corpus_id = _non_empty_string(payload["corpus_id"], "corpus.corpus_id")
    version = _non_empty_string(payload["version"], "corpus.version")
    records = payload["documents"]
    if not isinstance(records, list) or not records:
        raise ValueError("corpus.documents must be a non-empty array")
    documents = tuple(_decode_document(record, index) for index, record in enumerate(records))
    ids = [document.document_id for document in documents]
    if len(ids) != len(set(ids)):
        raise ValueError("corpus document_id values must be unique")
    return f"{corpus_id}:{version}", documents


def load_retrieval_suite(
    path: str | Path = DEFAULT_RETRIEVAL_SUITE_PATH,
    *,
    expected_corpus: str | None = None,
    known_document_ids: Iterable[str] | None = None,
    expected_corpus_sha256: str | None = None,
) -> tuple[str, tuple[RetrievalCase, ...]]:
    payload = _load_json(path)
    suite_fields = {"suite_id", "version", "corpus", "cases"}
    if "corpus_sha256" in payload:
        suite_fields.add("corpus_sha256")
    _require_exact_keys(payload, suite_fields, "suite")
    suite_id = _non_empty_string(payload["suite_id"], "suite.suite_id")
    version = _non_empty_string(payload["version"], "suite.version")
    declared_corpus = _non_empty_string(payload["corpus"], "suite.corpus")
    if expected_corpus is not None and declared_corpus != expected_corpus:
        raise ValueError(
            f"retrieval suite targets {declared_corpus!r}, "
            f"but the loaded corpus is {expected_corpus!r}"
        )
    declared_corpus_sha256 = payload.get("corpus_sha256")
    if declared_corpus_sha256 is not None:
        declared_corpus_sha256 = _non_empty_string(
            declared_corpus_sha256,
            "suite.corpus_sha256",
        ).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", declared_corpus_sha256):
            raise ValueError("suite.corpus_sha256 must be a SHA-256 hex digest")
    if expected_corpus_sha256 is not None:
        if declared_corpus_sha256 is None:
            raise ValueError(
                "retrieval suite must declare corpus_sha256 when the corpus is validated"
            )
        if declared_corpus_sha256 != expected_corpus_sha256.lower():
            raise ValueError(
                "retrieval suite corpus_sha256 does not match the loaded corpus"
            )
    records = payload["cases"]
    if not isinstance(records, list) or not records:
        raise ValueError("suite.cases must be a non-empty array")
    cases = tuple(_decode_case(record, index) for index, record in enumerate(records))
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("retrieval case_id values must be unique")
    if known_document_ids is not None:
        known = set(known_document_ids)
        referenced = {
            document_id
            for case in cases
            for document_id in case.relevant_document_ids
        }
        unknown = sorted(referenced - known)
        if unknown:
            raise ValueError(
                f"retrieval suite references unknown document_id values: {unknown}"
            )
    return f"{suite_id}:{version}", cases


def build_dense_index(
    corpus_path: str | Path = DEFAULT_CORPUS_PATH,
    *,
    embedding_provider: EmbeddingProvider | None = None,
) -> DenseKnowledgeIndex:
    corpus_version, chunks = load_corpus_chunks(corpus_path)
    provider = embedding_provider or HashingEmbeddingProvider()
    version = (
        f"{corpus_version}:dense-hashing-content-v2"
        if isinstance(provider, HashingEmbeddingProvider)
        else f"{corpus_version}:dense-content-v2:{provider.name}"
    )
    return DenseKnowledgeIndex(chunks, embedding_provider=provider, index_version=version)


def build_bm25_index(corpus_path: str | Path = DEFAULT_CORPUS_PATH) -> BM25KnowledgeIndex:
    corpus_version, chunks = load_corpus_chunks(corpus_path)
    return BM25KnowledgeIndex(chunks, index_version=f"{corpus_version}:bm25-content-v2")


def build_hybrid_index(
    corpus_path: str | Path = DEFAULT_CORPUS_PATH,
    *,
    embedding_provider: EmbeddingProvider | None = None,
) -> ReciprocalRankFusionIndex:
    corpus_version, chunks = load_corpus_chunks(corpus_path)
    provider = embedding_provider or HashingEmbeddingProvider()
    dense_version = (
        f"{corpus_version}:dense-hashing-content-v2"
        if isinstance(provider, HashingEmbeddingProvider)
        else f"{corpus_version}:dense-content-v2:{provider.name}"
    )
    dense = DenseKnowledgeIndex(
        chunks,
        embedding_provider=provider,
        index_version=dense_version,
    )
    bm25 = BM25KnowledgeIndex(chunks, index_version=f"{corpus_version}:bm25-content-v2")
    hybrid_version = (
        f"{corpus_version}:hybrid-document-rrf-v2"
        if isinstance(provider, HashingEmbeddingProvider)
        else f"{corpus_version}:hybrid-document-rrf-v2:{provider.name}"
    )
    return ReciprocalRankFusionIndex(
        dense,
        bm25,
        index_version=hybrid_version,
    )


def load_corpus_chunks(
    corpus_path: str | Path = DEFAULT_CORPUS_PATH,
) -> tuple[str, tuple[KnowledgeChunk, ...]]:
    corpus_version, documents = load_corpus(corpus_path)
    chunker = DeterministicChunker()
    chunks = tuple(chunk for document in documents for chunk in chunker.split(document))
    return corpus_version, chunks


def build_default_dense_index() -> DenseKnowledgeIndex:
    return build_dense_index()


def evaluate_retrieval(
    index: DenseKnowledgeIndex | BM25KnowledgeIndex | ReciprocalRankFusionIndex,
    cases: Sequence[RetrievalCase],
    *,
    top_k: int = 5,
) -> dict[str, Any]:
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    positive_recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    scope_results: list[bool] = []
    semantic_no_answer_results: list[bool] = []
    serving_latencies_ms: list[float] = []
    exhaustive_latencies_ms: list[float] = []
    indexed_tenants = {chunk.tenant_id for chunk in index.chunks}
    for case in cases:
        filters = case.filters if not case.relevant_document_ids else None
        serving_started = time.perf_counter()
        index.search(
            case.query,
            tenant_id=case.tenant_id,
            top_k=top_k,
            filters=filters,
        )
        serving_latencies_ms.append((time.perf_counter() - serving_started) * 1000)
        exhaustive_started = time.perf_counter()
        chunk_results = index.search(
            case.query,
            tenant_id=case.tenant_id,
            top_k=max(top_k, len(index.chunks)),
            # Positive cases evaluate ranking across the corpus. Negative cases
            # exercise tenant/metadata scoping and must return no evidence.
            filters=filters,
        )
        results = _unique_document_results(chunk_results, top_k=top_k)
        exhaustive_latencies_ms.append((time.perf_counter() - exhaustive_started) * 1000)
        retrieved = [result.chunk.document_id for result in results]
        relevant = set(case.relevant_document_ids)
        if relevant:
            hits = relevant.intersection(retrieved)
            positive_recalls.append(len(hits) / len(relevant))
            first_rank = next((rank for rank, document_id in enumerate(retrieved, 1) if document_id in relevant), None)
            reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
            ndcgs.append(_binary_ndcg(retrieved, relevant))
            passed = bool(hits)
            case_type = "relevance"
        else:
            passed = not results
            is_scope_case = bool(case.filters) or case.tenant_id not in indexed_tenants
            if is_scope_case:
                case_type = "scope_isolation"
                scope_results.append(passed)
            else:
                case_type = "semantic_no_answer"
                semantic_no_answer_results.append(passed)
        rows.append(
            {
                "case_id": case.case_id,
                "case_type": case_type,
                "query": case.query,
                "relevant_document_ids": list(case.relevant_document_ids),
                "retrieved_document_ids": retrieved,
                "passed": passed,
                "results": [result.to_dict() for result in results],
            }
        )
    return {
        "benchmark_version": "retrieval-benchmark-v2",
        "retrieval_method": index.retrieval_method,
        "embedding_provider": index.embedding_provider.name if index.embedding_provider else None,
        "embedding_dimensions": (
            index.embedding_provider.dimensions if index.embedding_provider else None
        ),
        "embedding_artifact_sha256": (
            getattr(index.embedding_provider, "artifact_sha256", None)
            if index.embedding_provider
            else None
        ),
        "embedding_artifact_file_count": (
            getattr(index.embedding_provider, "artifact_file_count", None)
            if index.embedding_provider
            else None
        ),
        "embedding_device": (
            getattr(index.embedding_provider, "device", None)
            if index.embedding_provider
            else None
        ),
        "index_version": index.index_version,
        "case_count": len(cases),
        "positive_case_count": len(positive_recalls),
        "negative_case_count": len(scope_results) + len(semantic_no_answer_results),
        "scope_case_count": len(scope_results),
        "semantic_no_answer_case_count": len(semantic_no_answer_results),
        "metrics": {
            f"recall_at_{top_k}": _mean(positive_recalls),
            "mrr": _mean(reciprocal_ranks),
            f"ndcg_at_{top_k}": _mean(ndcgs),
            "scope_isolation_accuracy": _mean(
                [float(value) for value in scope_results]
            ),
            "semantic_no_answer_accuracy": _mean(
                [float(value) for value in semantic_no_answer_results]
            ),
            "serving_p50_latency_ms": _percentile(serving_latencies_ms, 0.50),
            "serving_p95_latency_ms": _percentile(serving_latencies_ms, 0.95),
            "exhaustive_eval_p50_latency_ms": _percentile(
                exhaustive_latencies_ms, 0.50
            ),
            "exhaustive_eval_p95_latency_ms": _percentile(
                exhaustive_latencies_ms, 0.95
            ),
        },
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "rows": rows,
    }


def evaluate_dense_retrieval(
    index: DenseKnowledgeIndex,
    cases: Sequence[RetrievalCase],
    *,
    top_k: int = 5,
) -> dict[str, Any]:
    """Backward-compatible name for the original R0 evaluation entry point."""

    return evaluate_retrieval(index, cases, top_k=top_k)


def write_retrieval_report(
    report: dict[str, Any],
    path: str | Path,
    *,
    include_result_text: bool = True,
) -> None:
    payload = {**report, "result_text_included": include_result_text}
    if not include_result_text:
        payload["rows"] = [
            {
                **row,
                "results": [
                    {key: value for key, value in result.items() if key != "text"}
                    for result in row["results"]
                ],
            }
            for row in report["rows"]
        ]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _decode_document(value: Any, index: int) -> KnowledgeDocument:
    location = f"corpus.documents[{index}]"
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    required = {
        "document_id", "title", "text", "source_uri", "source_type", "tenant_id",
        "document_version", "effective_at", "license", "provenance", "metadata",
    }
    _require_exact_keys(value, required, location)
    metadata = value["metadata"]
    if not isinstance(metadata, dict):
        raise ValueError(f"{location}.metadata must be an object")
    return KnowledgeDocument(
        **{
            key: _non_empty_string(value[key], f"{location}.{key}")
            for key in required - {"metadata"}
        },
        metadata=dict(metadata),
    )


def _decode_case(value: Any, index: int) -> RetrievalCase:
    location = f"suite.cases[{index}]"
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    _require_exact_keys(value, {"case_id", "query", "tenant_id", "relevant_document_ids", "filters"}, location)
    relevant = value["relevant_document_ids"]
    filters = value["filters"]
    if not isinstance(relevant, list) or any(not isinstance(item, str) or not item for item in relevant):
        raise ValueError(f"{location}.relevant_document_ids must be an array of strings")
    if not isinstance(filters, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in filters.items()):
        raise ValueError(f"{location}.filters must be a string map")
    return RetrievalCase(
        case_id=_non_empty_string(value["case_id"], f"{location}.case_id"),
        query=_non_empty_string(value["query"], f"{location}.query"),
        tenant_id=_non_empty_string(value["tenant_id"], f"{location}.tenant_id"),
        relevant_document_ids=tuple(relevant),
        filters=dict(filters),
    )


def _indexable_text(chunk: KnowledgeChunk) -> str:
    searchable_metadata = "\n".join(
        str(chunk.metadata[field])
        for field in ("service", "symptom")
        if chunk.metadata.get(field)
    )
    values = (chunk.title, chunk.section_path, chunk.text, searchable_metadata)
    return "\n".join(value for value in values if value)


def _directory_fingerprint(path: Path, revision: str) -> tuple[str, int]:
    tree_path = path / ".cache" / "huggingface" / "trees" / f"{revision}.json"
    try:
        tree_payload = json.loads(tree_path.read_text(encoding="utf-8"))
        tree_files = tree_payload["files"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(
            f"local model snapshot is missing valid revision metadata: {tree_path}"
        ) from exc
    if not isinstance(tree_files, dict):
        raise ValueError(
            f"local model revision metadata has invalid files: {tree_path}"
        )
    digest = hashlib.sha256()
    file_count = 0
    for file in sorted(
        (candidate for candidate in path.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(path).as_posix(),
    ):
        relative = file.relative_to(path).as_posix()
        if relative.startswith(".cache/"):
            continue
        if file.is_symlink():
            raise ValueError(f"embedding model artifact must not be a symlink: {relative}")
        expected = tree_files.get(relative)
        if not isinstance(expected, dict):
            raise ValueError(
                f"embedding model artifact is absent from revision {revision}: {relative}"
            )
        size = file.stat().st_size
        if expected.get("size") != size:
            raise ValueError(
                f"embedding model artifact size mismatch for revision {revision}: {relative}"
            )

        file_count += 1
        file_digest = hashlib.sha256()
        git_digest = hashlib.sha1(f"blob {size}\0".encode("ascii"))
        with file.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                file_digest.update(block)
                git_digest.update(block)
        artifact_digest = file_digest.hexdigest()
        expected_lfs = expected.get("lfs_sha256")
        expected_blob = expected.get("blob_id")
        if expected_lfs is not None:
            matches_revision = artifact_digest == expected_lfs
        else:
            matches_revision = git_digest.hexdigest() == expected_blob
        if not matches_revision:
            raise ValueError(
                f"embedding model artifact does not match revision {revision}: {relative}"
            )

        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(bytes.fromhex(artifact_digest))
    if file_count == 0:
        raise ValueError(f"embedding model directory contains no files: {path}")
    return digest.hexdigest(), file_count


def _validate_search(query: str, tenant_id: str, top_k: int) -> None:
    if not query.strip():
        raise ValueError("retrieval query must not be empty")
    if not tenant_id.strip():
        raise ValueError("tenant_id must not be empty")
    if top_k <= 0:
        raise ValueError("top_k must be positive")


def _matches_scope(
    chunk: KnowledgeChunk,
    tenant_id: str,
    filters: dict[str, str] | None,
) -> bool:
    return chunk.tenant_id == tenant_id and not (
        filters and any(str(chunk.metadata.get(key)) != value for key, value in filters.items())
    )


def _text_features(text: str) -> tuple[str, ...]:
    normalized = text.lower().replace("_", "-")
    words = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized)
    cjk_sequences = re.findall(r"[\u3400-\u9fff]+", normalized)
    features = list(words)
    for sequence in cjk_sequences:
        features.extend(sequence)
        features.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
        features.extend(sequence[index : index + 3] for index in range(len(sequence) - 2))
    return tuple(features)


def _markdown_sections(text: str) -> tuple[tuple[str, str], ...]:
    current = "root"
    sections: list[tuple[str, str]] = []
    buffer: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            if buffer and "\n".join(buffer).strip():
                sections.append((current, "\n".join(buffer).strip()))
            current = line.lstrip("#").strip() or "root"
            buffer = []
        else:
            buffer.append(line)
    if buffer and "\n".join(buffer).strip():
        sections.append((current, "\n".join(buffer).strip()))
    return tuple(sections) or (("root", text.strip()),)


def _windows(text: str, max_chars: int, overlap_chars: int) -> Iterable[tuple[int, str]]:
    if len(text) <= max_chars:
        yield 0, text
        return
    step = max_chars - overlap_chars
    for offset in range(0, len(text), step):
        value = text[offset : offset + max_chars].strip()
        if value:
            yield offset, value
        if offset + max_chars >= len(text):
            break


def _binary_ndcg(retrieved: Sequence[str], relevant: set[str]) -> float:
    dcg = sum(1.0 / math.log2(rank + 1) for rank, item in enumerate(retrieved, 1) if item in relevant)
    ideal_hits = min(len(relevant), len(retrieved))
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / ideal if ideal else 0.0


def _unique_document_results(
    results: Sequence[RetrievalResult],
    *,
    top_k: int,
) -> tuple[RetrievalResult, ...]:
    unique: list[RetrievalResult] = []
    seen: set[str] = set()
    for result in results:
        document_id = result.chunk.document_id
        if document_id in seen:
            continue
        seen.add(document_id)
        unique.append(result)
        if len(unique) == top_k:
            break
    return tuple(unique)


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[rank], 3)


def _load_json(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load JSON from {target}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{target} must contain a JSON object")
    return payload


def _non_empty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} must be a non-empty string")
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)}")
        if unknown:
            details.append(f"unknown={sorted(unknown)}")
        raise ValueError(f"{location} has invalid fields: {', '.join(details)}")
