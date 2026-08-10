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
        index_version: str = "dense-hashing-v1",
    ):
        self.embedding_provider = embedding_provider or HashingEmbeddingProvider()
        self.index_version = index_version
        self.chunks = tuple(chunks)
        if not self.chunks:
            raise ValueError("knowledge index requires at least one chunk")
        self._vectors = tuple(
            self.embedding_provider.embed(_indexable_text(chunk)) for chunk in self.chunks
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
        index_version: str = "bm25-v1",
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
        index_version: str = "hybrid-rrf-v1",
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
        dense_results = self.dense.search(
            query,
            tenant_id=tenant_id,
            top_k=candidate_k,
            filters=filters,
            min_score=min_score,
        )
        bm25_results = self.bm25.search(
            query,
            tenant_id=tenant_id,
            top_k=candidate_k,
            filters=filters,
            min_score=min_score,
        )
        chunks: dict[str, KnowledgeChunk] = {}
        scores: dict[str, float] = {}
        component_ranks: dict[str, dict[str, float]] = {}
        for method, results in (("dense", dense_results), ("bm25", bm25_results)):
            for result in results:
                chunk_id = result.chunk.chunk_id
                chunks[chunk_id] = result.chunk
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (self.rrf_k + result.rank)
                component_ranks.setdefault(chunk_id, {})[f"{method}_rank"] = float(result.rank)
                component_ranks[chunk_id][f"{method}_score"] = result.score
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        return tuple(
            RetrievalResult(
                chunk=chunks[chunk_id],
                score=round(score, 6),
                rank=rank,
                embedding_provider=self.embedding_provider.name,
                index_version=self.index_version,
                retrieval_method=self.retrieval_method,
                component_scores=component_ranks[chunk_id],
            )
            for rank, (chunk_id, score) in enumerate(ranked, start=1)
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
) -> tuple[str, tuple[RetrievalCase, ...]]:
    payload = _load_json(path)
    _require_exact_keys(payload, {"suite_id", "version", "corpus", "cases"}, "suite")
    suite_id = _non_empty_string(payload["suite_id"], "suite.suite_id")
    version = _non_empty_string(payload["version"], "suite.version")
    records = payload["cases"]
    if not isinstance(records, list) or not records:
        raise ValueError("suite.cases must be a non-empty array")
    cases = tuple(_decode_case(record, index) for index, record in enumerate(records))
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("retrieval case_id values must be unique")
    return f"{suite_id}:{version}", cases


def build_dense_index(corpus_path: str | Path = DEFAULT_CORPUS_PATH) -> DenseKnowledgeIndex:
    corpus_version, chunks = load_corpus_chunks(corpus_path)
    return DenseKnowledgeIndex(chunks, index_version=f"{corpus_version}:dense-hashing-v1")


def build_bm25_index(corpus_path: str | Path = DEFAULT_CORPUS_PATH) -> BM25KnowledgeIndex:
    corpus_version, chunks = load_corpus_chunks(corpus_path)
    return BM25KnowledgeIndex(chunks, index_version=f"{corpus_version}:bm25-v1")


def build_hybrid_index(corpus_path: str | Path = DEFAULT_CORPUS_PATH) -> ReciprocalRankFusionIndex:
    corpus_version, chunks = load_corpus_chunks(corpus_path)
    dense = DenseKnowledgeIndex(chunks, index_version=f"{corpus_version}:dense-hashing-v1")
    bm25 = BM25KnowledgeIndex(chunks, index_version=f"{corpus_version}:bm25-v1")
    return ReciprocalRankFusionIndex(
        dense,
        bm25,
        index_version=f"{corpus_version}:hybrid-rrf-v1",
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
    negative_results: list[bool] = []
    latencies_ms: list[float] = []
    for case in cases:
        case_started = time.perf_counter()
        results = index.search(
            case.query,
            tenant_id=case.tenant_id,
            top_k=top_k,
            # Positive cases evaluate ranking across the corpus. Negative cases
            # exercise tenant/metadata scoping and must return no evidence.
            filters=case.filters if not case.relevant_document_ids else None,
        )
        latencies_ms.append((time.perf_counter() - case_started) * 1000)
        retrieved = [result.chunk.document_id for result in results]
        relevant = set(case.relevant_document_ids)
        if relevant:
            hits = relevant.intersection(retrieved)
            positive_recalls.append(len(hits) / len(relevant))
            first_rank = next((rank for rank, document_id in enumerate(retrieved, 1) if document_id in relevant), None)
            reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
            ndcgs.append(_binary_ndcg(retrieved, relevant))
            passed = bool(hits)
        else:
            passed = not results
            negative_results.append(passed)
        rows.append(
            {
                "case_id": case.case_id,
                "query": case.query,
                "relevant_document_ids": list(case.relevant_document_ids),
                "retrieved_document_ids": retrieved,
                "passed": passed,
                "results": [result.to_dict() for result in results],
            }
        )
    return {
        "benchmark_version": "retrieval-benchmark-v1",
        "retrieval_method": index.retrieval_method,
        "embedding_provider": index.embedding_provider.name if index.embedding_provider else None,
        "index_version": index.index_version,
        "case_count": len(cases),
        "positive_case_count": len(positive_recalls),
        "negative_case_count": len(negative_results),
        "metrics": {
            f"recall_at_{top_k}": _mean(positive_recalls),
            "mrr": _mean(reciprocal_ranks),
            f"ndcg_at_{top_k}": _mean(ndcgs),
            "no_answer_accuracy": _mean([float(value) for value in negative_results]),
            "p50_latency_ms": _percentile(latencies_ms, 0.50),
            "p95_latency_ms": _percentile(latencies_ms, 0.95),
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


def write_retrieval_report(report: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    metadata = " ".join(str(value) for value in chunk.metadata.values())
    return f"{chunk.title} {chunk.text} {metadata}"


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
