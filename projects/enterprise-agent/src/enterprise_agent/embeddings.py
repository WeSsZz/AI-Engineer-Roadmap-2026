from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Protocol


class EmbeddingProvider(Protocol):
    """Common contract for deterministic and learned embedding providers."""

    name: str
    dimensions: int
    fingerprint: str

    def embed(self, text: str) -> tuple[float, ...]: ...

    def embed_many(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


class EmbeddingConfigurationError(ValueError):
    """Raised when an embedding provider cannot be configured or returns bad data."""


SnapshotResolver = Callable[..., str | Path]
ModelFactory = Callable[..., Any]


class BGEM3EmbeddingProvider:
    """Pinned, dense-only BGE-M3 embeddings loaded through optional dependencies.

    ``revision`` is deliberately required: silently following a moving model branch
    would make retrieval benchmarks and persisted caches impossible to reproduce.
    Tests and offline callers may inject both dependency boundaries.
    """

    name = "bge-m3"
    dimensions = 1024
    adapter_version = "bge-m3-dense-v1"

    def __init__(
        self,
        *,
        revision: str,
        model_name: str = "BAAI/bge-m3",
        batch_size: int = 16,
        max_length: int = 8192,
        use_fp16: bool = True,
        device: str | None = None,
        model_cache_dir: str | Path | None = None,
        model_factory: ModelFactory | None = None,
        snapshot_resolver: SnapshotResolver | None = None,
    ):
        if not model_name.strip():
            raise EmbeddingConfigurationError("BGE-M3 model_name must not be empty")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", revision.strip()):
            raise EmbeddingConfigurationError(
                "BGE-M3 revision must be a pinned 40-character commit SHA"
            )
        if batch_size <= 0:
            raise EmbeddingConfigurationError("BGE-M3 batch_size must be positive")
        if max_length <= 0:
            raise EmbeddingConfigurationError("BGE-M3 max_length must be positive")
        normalized_device = device.strip() if isinstance(device, str) else device
        if normalized_device and normalized_device.lower().startswith("cpu") and use_fp16:
            raise EmbeddingConfigurationError("BGE-M3 fp16 is not supported on a CPU device")

        self.model_name = model_name.strip()
        self.requested_revision = revision.strip()
        self.batch_size = batch_size
        self.max_length = max_length
        self.use_fp16 = use_fp16
        self.normalize_embeddings = True
        self.requested_device = normalized_device or None
        self.model_cache_dir = (
            Path(model_cache_dir).expanduser()
            if model_cache_dir is not None
            else None
        )

        resolver = snapshot_resolver or _load_snapshot_download()
        factory = model_factory or _load_bge_m3_factory()
        resolver_arguments: dict[str, Any] = {
            "repo_id": self.model_name,
            "revision": self.requested_revision,
            "ignore_patterns": ("onnx/*", "imgs/*", "*.md"),
        }
        if self.model_cache_dir is not None:
            resolver_arguments["cache_dir"] = str(self.model_cache_dir)
        try:
            snapshot = resolver(**resolver_arguments)
        except Exception as error:
            raise EmbeddingConfigurationError(
                f"cannot resolve pinned BGE-M3 snapshot {self.model_name}@{revision}: {error}"
            ) from error
        if not snapshot or not str(snapshot).strip():
            raise EmbeddingConfigurationError("BGE-M3 snapshot resolver returned an empty path")

        self.snapshot_path = Path(snapshot)
        self.resolved_revision = _resolved_snapshot_revision(
            self.snapshot_path,
            fallback=self.requested_revision,
        )
        if self.resolved_revision.lower() != self.requested_revision.lower():
            raise EmbeddingConfigurationError(
                "BGE-M3 snapshot revision does not match the requested commit SHA"
            )
        factory_arguments: dict[str, Any] = {
            "use_fp16": self.use_fp16,
            "normalize_embeddings": self.normalize_embeddings,
        }
        if self.requested_device is not None:
            factory_arguments["devices"] = self.requested_device
        try:
            self._model = factory(str(self.snapshot_path), **factory_arguments)
        except Exception as error:
            raise EmbeddingConfigurationError(
                f"cannot load BGE-M3 snapshot {self.resolved_revision}: {error}"
            ) from error
        model_devices = getattr(self._model, "target_devices", None)
        self.device = _device_label(model_devices) or self.requested_device or "auto"
        self.requested_dtype = "float16" if self.use_fp16 else "float32"
        self.dtype = (
            "float32"
            if self.use_fp16 and _uses_cpu(self.device)
            else self.requested_dtype
        )
        self.library_versions = {
            package: _distribution_version(package)
            for package in ("FlagEmbedding", "transformers", "torch")
        }
        libraries = ",".join(
            f"{package.lower()}-{package_version}"
            for package, package_version in self.library_versions.items()
        )
        self.fingerprint = (
            f"bge-m3:{self.model_name}@{self.resolved_revision}"
            f":dense-{self.dimensions}:max-length-{self.max_length}"
            f":normalized-true:precision-{self.dtype}:device-{self.device}"
            f":adapter-{self.adapter_version}:libraries-{libraries}"
        )

    def embed(self, text: str) -> tuple[float, ...]:
        return self.embed_many((text,))[0]

    def embed_many(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        values = _validate_texts(texts)
        if not values:
            return ()

        try:
            encoded = self._model.encode(
                list(values),
                batch_size=self.batch_size,
                max_length=self.max_length,
                return_dense=True,
                return_sparse=False,
                return_colbert_vecs=False,
            )
        except Exception as error:
            raise EmbeddingConfigurationError(f"BGE-M3 encoding failed: {error}") from error
        if not isinstance(encoded, Mapping) or "dense_vecs" not in encoded:
            raise EmbeddingConfigurationError(
                "BGE-M3 encode result must contain a dense_vecs collection"
            )
        return _validated_vectors(
            encoded["dense_vecs"],
            expected_count=len(values),
            dimensions=self.dimensions,
            source="BGE-M3",
        )


class SQLiteCachedEmbeddingProvider:
    """Persistent exact-text cache scoped to an embedding provider fingerprint."""

    def __init__(self, base: EmbeddingProvider, cache_path: str | Path):
        if base.dimensions <= 0:
            raise EmbeddingConfigurationError("cached provider dimensions must be positive")
        if not base.fingerprint.strip():
            raise EmbeddingConfigurationError("cached provider fingerprint must not be empty")

        self.base = base
        self.name = base.name
        self.dimensions = base.dimensions
        self.fingerprint = base.fingerprint
        raw_cache_path = str(cache_path)
        if not raw_cache_path.strip():
            raise EmbeddingConfigurationError("embedding cache path must not be empty")
        if raw_cache_path == ":memory:":
            raise EmbeddingConfigurationError(
                "persistent embedding cache requires a filesystem path, not :memory:"
            )
        cache_path_value = Path(raw_cache_path).expanduser()
        self.cache_path = str(cache_path_value)
        cache_path_value.parent.mkdir(parents=True, exist_ok=True)

        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._query_bypasses = 0
        self._stats_lock = threading.Lock()
        self._initialize()

    @property
    def cache_stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "writes": self._writes,
                "query_bypasses": self._query_bypasses,
            }

    @property
    def stats(self) -> dict[str, int]:
        return self.cache_stats

    def embed(self, text: str) -> tuple[float, ...]:
        return self.embed_many((text,))[0]

    def embed_query(self, text: str) -> tuple[float, ...]:
        """Encode a live query without polluting latency with a disk-cache hit."""

        vector = _validated_vectors(
            (self.base.embed(text),),
            expected_count=1,
            dimensions=self.dimensions,
            source=f"embedding provider {self.base.name}",
        )[0]
        with self._stats_lock:
            self._query_bypasses += 1
        return vector

    def embed_many(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        values = _validate_texts(texts)
        if not values:
            return ()

        keys = tuple(_cache_key(self.fingerprint, value) for value in values)
        cached = self._read_many(tuple(dict.fromkeys(keys)))
        hit_count = sum(key in cached for key in keys)
        miss_count = len(keys) - hit_count

        missing_by_key: dict[str, str] = {}
        for key, text in zip(keys, values, strict=True):
            if key not in cached:
                missing_by_key.setdefault(key, text)

        if missing_by_key:
            missing_keys = tuple(missing_by_key)
            missing_texts = tuple(missing_by_key.values())
            generated = _validated_vectors(
                self.base.embed_many(missing_texts),
                expected_count=len(missing_texts),
                dimensions=self.dimensions,
                source=f"embedding provider {self.base.name}",
            )
            additions = dict(zip(missing_keys, generated, strict=True))
            self._write_many(additions)
            cached.update(additions)

        with self._stats_lock:
            self._hits += hit_count
            self._misses += miss_count
            self._writes += len(missing_by_key)
        return tuple(cached[key] for key in keys)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.cache_path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    cache_key TEXT PRIMARY KEY,
                    provider_fingerprint TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector_json TEXT NOT NULL
                )
                """
            )

    def _read_many(self, keys: Sequence[str]) -> dict[str, tuple[float, ...]]:
        found: dict[str, tuple[float, ...]] = {}
        with self._connect() as connection:
            for key in keys:
                row = connection.execute(
                    """
                    SELECT vector_json
                    FROM embedding_cache
                    WHERE cache_key = ?
                      AND provider_fingerprint = ?
                      AND dimensions = ?
                    """,
                    (key, self.fingerprint, self.dimensions),
                ).fetchone()
                if row is None:
                    continue
                try:
                    decoded = json.loads(row[0])
                    found[key] = _validated_vectors(
                        (decoded,),
                        expected_count=1,
                        dimensions=self.dimensions,
                        source="embedding cache",
                    )[0]
                except (EmbeddingConfigurationError, json.JSONDecodeError, TypeError):
                    connection.execute(
                        "DELETE FROM embedding_cache WHERE cache_key = ?",
                        (key,),
                    )
        return found

    def _write_many(self, values: Mapping[str, tuple[float, ...]]) -> None:
        rows = (
            (
                key,
                self.fingerprint,
                self.dimensions,
                json.dumps(vector, ensure_ascii=False, separators=(",", ":")),
            )
            for key, vector in values.items()
        )
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO embedding_cache (
                    cache_key,
                    provider_fingerprint,
                    dimensions,
                    vector_json
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    provider_fingerprint = excluded.provider_fingerprint,
                    dimensions = excluded.dimensions,
                    vector_json = excluded.vector_json
                """,
                rows,
            )


def _load_snapshot_download() -> SnapshotResolver:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise EmbeddingConfigurationError(
            "BGE-M3 requires the optional huggingface-hub dependency; "
            "install it with: pip install -e '.[bge]'"
        ) from error
    return snapshot_download


def _load_bge_m3_factory() -> ModelFactory:
    try:
        from FlagEmbedding import BGEM3FlagModel
    except ImportError as error:
        raise EmbeddingConfigurationError(
            "BGE-M3 requires the optional FlagEmbedding dependency; "
            "install it with: pip install -e '.[bge]'"
        ) from error
    return BGEM3FlagModel


def _resolved_snapshot_revision(snapshot_path: Path, *, fallback: str) -> str:
    parts = snapshot_path.parts
    try:
        snapshots_index = parts.index("snapshots")
    except ValueError:
        snapshots_index = -1
    if snapshots_index >= 0 and snapshots_index + 1 < len(parts):
        return parts[snapshots_index + 1]
    return snapshot_path.name or fallback


def _device_label(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        labels = tuple(str(item) for item in value)
        if labels:
            return ",".join(labels)
    return None


def _uses_cpu(device: str) -> bool:
    return any(part.strip().lower().startswith("cpu") for part in device.split(","))


def _distribution_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unknown"


def _validate_texts(texts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(texts, str):
        raise EmbeddingConfigurationError(
            "embedding texts must be a sequence of strings, not one string"
        )
    try:
        values = tuple(texts)
    except TypeError as error:
        raise EmbeddingConfigurationError("embedding texts must be a sequence of strings") from error
    if any(not isinstance(text, str) for text in values):
        raise EmbeddingConfigurationError("embedding texts must contain only strings")
    return values


def _validated_vectors(
    vectors: Any,
    *,
    expected_count: int,
    dimensions: int,
    source: str,
) -> tuple[tuple[float, ...], ...]:
    try:
        rows = tuple(vectors)
    except TypeError as error:
        raise EmbeddingConfigurationError(f"{source} returned non-iterable embeddings") from error
    if len(rows) != expected_count:
        raise EmbeddingConfigurationError(
            f"{source} returned {len(rows)} embeddings for {expected_count} texts"
        )

    validated: list[tuple[float, ...]] = []
    for position, row in enumerate(rows):
        try:
            vector = tuple(float(value) for value in row)
        except (TypeError, ValueError) as error:
            raise EmbeddingConfigurationError(
                f"{source} embedding {position} contains a non-numeric value"
            ) from error
        if len(vector) != dimensions:
            raise EmbeddingConfigurationError(
                f"{source} embedding {position} has dimension {len(vector)}; "
                f"expected {dimensions}"
            )
        if any(not math.isfinite(value) for value in vector):
            raise EmbeddingConfigurationError(
                f"{source} embedding {position} contains a non-finite value"
            )
        if not any(value != 0.0 for value in vector):
            raise EmbeddingConfigurationError(
                f"{source} embedding {position} must not be all zeros"
            )
        validated.append(vector)
    return tuple(validated)


def _cache_key(provider_fingerprint: str, text: str) -> str:
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    material = f"{provider_fingerprint}\0{text_hash}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()
