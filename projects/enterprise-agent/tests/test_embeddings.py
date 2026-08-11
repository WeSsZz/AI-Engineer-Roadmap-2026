from __future__ import annotations

import builtins
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enterprise_agent.embeddings import (
    BGEM3EmbeddingProvider,
    EmbeddingConfigurationError,
    SQLiteCachedEmbeddingProvider,
)


class FakeBGEModel:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def encode(self, texts: list[str], **arguments: object) -> dict[str, list[list[float]]]:
        self.calls.append((texts, arguments))
        return {
            "dense_vecs": [
                [float(position + 1)] + [0.0] * 1023 for position, _ in enumerate(texts)
            ]
        }


class FakeEmbeddingProvider:
    name = "fake"
    dimensions = 3
    fingerprint = "fake:v1:3"

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, text: str) -> tuple[float, ...]:
        return self.embed_many((text,))[0]

    def embed_many(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        values = tuple(texts)
        self.calls.append(values)
        return tuple((float(len(text)), 1.0, 0.0) for text in values)


class EmbeddingTests(unittest.TestCase):
    def test_bge_m3_resolves_pinned_snapshot_and_batches_dense_encoding(self) -> None:
        resolver_calls: list[dict[str, object]] = []
        factory_calls: list[tuple[str, bool, bool]] = []
        model = FakeBGEModel()

        def resolver(**arguments: object) -> str:
            resolver_calls.append(arguments)
            return "/models/models--BAAI--bge-m3/snapshots/" + "a" * 40

        def factory(
            model_path: str,
            *,
            use_fp16: bool,
            normalize_embeddings: bool,
        ) -> FakeBGEModel:
            factory_calls.append((model_path, use_fp16, normalize_embeddings))
            return model

        provider = BGEM3EmbeddingProvider(
            revision="a" * 40,
            batch_size=7,
            max_length=512,
            use_fp16=False,
            model_cache_dir="/models",
            snapshot_resolver=resolver,
            model_factory=factory,
        )
        vectors = provider.embed_many(("one", "two"))

        self.assertEqual(
            resolver_calls,
            [
                {
                    "repo_id": "BAAI/bge-m3",
                    "revision": "a" * 40,
                    "ignore_patterns": ("onnx/*", "imgs/*", "*.md"),
                    "cache_dir": "/models",
                }
            ],
        )
        self.assertEqual(
            factory_calls,
            [("/models/models--BAAI--bge-m3/snapshots/" + "a" * 40, False, True)],
        )
        self.assertEqual(provider.resolved_revision, "a" * 40)
        self.assertIn("a" * 40, provider.fingerprint)
        self.assertEqual(len(vectors), 2)
        self.assertEqual(len(vectors[0]), 1024)
        self.assertEqual(
            model.calls,
            [
                (
                    ["one", "two"],
                    {
                        "batch_size": 7,
                        "max_length": 512,
                        "return_dense": True,
                        "return_sparse": False,
                        "return_colbert_vecs": False,
                    },
                )
            ],
        )

    def test_bge_m3_rejects_wrong_dimensions_and_non_finite_values(self) -> None:
        class BadModel:
            def __init__(self, vector: list[float]):
                self.vector = vector

            def encode(self, texts: list[str], **_: object) -> dict[str, list[list[float]]]:
                return {"dense_vecs": [self.vector for _ in texts]}

        for vector, message in (
            ([0.0] * 8, "dimension 8"),
            ([float("nan")] * 1024, "non-finite"),
            ([0.0] * 1024, "all zeros"),
        ):
            with self.subTest(message=message):
                provider = BGEM3EmbeddingProvider(
                    revision="b" * 40,
                    snapshot_resolver=lambda **_: "/snapshots/" + "b" * 40,
                    model_factory=lambda *_args, **_kwargs: BadModel(vector),
                )
                with self.assertRaisesRegex(EmbeddingConfigurationError, message):
                    provider.embed("example")

    def test_sqlite_cache_batches_unique_misses_and_survives_new_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "embeddings.sqlite3"
            first_base = FakeEmbeddingProvider()
            first = SQLiteCachedEmbeddingProvider(first_base, cache_path)

            original = first.embed_many(("alpha", "beta", "alpha"))

            self.assertEqual(first_base.calls, [("alpha", "beta")])
            self.assertEqual(
                first.cache_stats,
                {"hits": 0, "misses": 3, "writes": 2, "query_bypasses": 0},
            )
            second_base = FakeEmbeddingProvider()
            second = SQLiteCachedEmbeddingProvider(second_base, cache_path)
            cached = second.embed_many(("alpha", "beta", "alpha"))

            self.assertEqual(cached, original)
            self.assertEqual(second_base.calls, [])
            self.assertEqual(
                second.stats,
                {"hits": 3, "misses": 0, "writes": 0, "query_bypasses": 0},
            )

            second.embed_many(("alpha", "gamma"))
            self.assertEqual(second_base.calls, [("gamma",)])
            self.assertEqual(
                second.stats,
                {"hits": 4, "misses": 1, "writes": 1, "query_bypasses": 0},
            )

            second.embed_query("alpha")
            second.embed_query("alpha")
            self.assertEqual(second_base.calls[-2:], [("alpha",), ("alpha",)])
            self.assertEqual(second.stats["query_bypasses"], 2)

    def test_cache_rejects_bad_base_provider_output(self) -> None:
        base = FakeEmbeddingProvider()
        base.embed_many = lambda _texts: ((1.0, 2.0),)  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as directory:
            cached = SQLiteCachedEmbeddingProvider(base, Path(directory) / "cache.sqlite3")
            with self.assertRaisesRegex(EmbeddingConfigurationError, "expected 3"):
                cached.embed("bad")

    def test_missing_optional_dependencies_have_actionable_errors(self) -> None:
        real_import = builtins.__import__

        for blocked_module, arguments in (
            ("huggingface_hub", {}),
            ("FlagEmbedding", {"snapshot_resolver": lambda **_: "/snapshots/fixed"}),
        ):
            with self.subTest(module=blocked_module):
                def block_dependency(name: str, *args: object, **kwargs: object) -> object:
                    if name == blocked_module:
                        raise ModuleNotFoundError("blocked for test")
                    return real_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=block_dependency):
                    with self.assertRaisesRegex(EmbeddingConfigurationError, "pip install"):
                        BGEM3EmbeddingProvider(revision="b" * 40, **arguments)

    def test_configuration_requires_a_non_empty_pinned_revision(self) -> None:
        for revision in (" ", "main", "release-tag"):
            with self.subTest(revision=revision):
                with self.assertRaisesRegex(
                    EmbeddingConfigurationError,
                    "pinned 40-character",
                ):
                    BGEM3EmbeddingProvider(
                        revision=revision,
                        snapshot_resolver=lambda **_: "/snapshots/fixed",
                        model_factory=lambda *_args, **_kwargs: FakeBGEModel(),
                    )

    def test_cpu_reports_effective_fp32_and_rejects_explicit_fp16(self) -> None:
        model = FakeBGEModel()
        model.target_devices = ["cpu"]
        provider = BGEM3EmbeddingProvider(
            revision="c" * 40,
            use_fp16=True,
            snapshot_resolver=lambda **_: "/snapshots/" + "c" * 40,
            model_factory=lambda *_args, **_kwargs: model,
        )

        self.assertEqual(provider.device, "cpu")
        self.assertEqual(provider.requested_dtype, "float16")
        self.assertEqual(provider.dtype, "float32")
        self.assertIn("precision-float32:device-cpu", provider.fingerprint)

        with self.assertRaisesRegex(EmbeddingConfigurationError, "not supported on a CPU"):
            BGEM3EmbeddingProvider(
                revision="c" * 40,
                device="cpu",
                use_fp16=True,
                snapshot_resolver=lambda **_: "/snapshots/" + "c" * 40,
                model_factory=lambda *_args, **_kwargs: model,
            )

    def test_snapshot_must_resolve_to_the_requested_commit(self) -> None:
        with self.assertRaisesRegex(EmbeddingConfigurationError, "does not match"):
            BGEM3EmbeddingProvider(
                revision="d" * 40,
                snapshot_resolver=lambda **_: "/snapshots/" + "e" * 40,
                model_factory=lambda *_args, **_kwargs: FakeBGEModel(),
            )

    def test_cache_requires_a_persistent_filesystem_path(self) -> None:
        with self.assertRaisesRegex(EmbeddingConfigurationError, "filesystem path"):
            SQLiteCachedEmbeddingProvider(FakeEmbeddingProvider(), ":memory:")


if __name__ == "__main__":
    unittest.main()
