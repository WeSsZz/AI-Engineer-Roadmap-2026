from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from enterprise_agent.corpus_ingest import (
    CorpusBuildError,
    build_corpus_from_manifest,
    load_corpus_manifest,
)
from enterprise_agent.retrieval import load_corpus


class CorpusIngestTests(unittest.TestCase):
    def test_pinned_git_blob_build_is_deterministic_and_offline_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, commit, document_sha, license_sha = self._create_source_repository(root)
            manifest = root / "manifest.json"
            self._write_manifest(
                manifest,
                repository=repository,
                commit=commit,
                document_sha=document_sha,
                license_sha=license_sha,
            )
            output = root / "public-corpus.json"
            cache = root / "cache"

            first = build_corpus_from_manifest(
                manifest,
                cache_dir=cache,
                output=output,
                allow_local_repositories=True,
            )
            first_bytes = output.read_bytes()
            corpus_version, documents = load_corpus(output)
            self.assertEqual(corpus_version, "test-public-ops:1.0.0")
            self.assertEqual(first.document_count, 1)
            self.assertEqual(first.output_sha256, hashlib.sha256(first_bytes).hexdigest())
            self.assertEqual(documents[0].text, "# Old Guide\n\npinned body\n")
            self.assertNotIn("title: ignored front matter", documents[0].text)
            self.assertEqual(documents[0].document_version, commit)
            self.assertEqual(documents[0].metadata["raw_content_sha256"], document_sha)
            self.assertEqual(documents[0].metadata["license_content_sha256"], license_sha)
            self.assertEqual(
                documents[0].metadata["modifications"],
                "Removed leading YAML/TOML front matter when present; normalized UTF-8 BOM "
                "and line endings; trimmed document boundaries; added one final newline.",
            )
            self.assertEqual(documents[0].metadata["source_snapshot_at"], documents[0].effective_at)
            self.assertIn(commit, documents[0].source_uri)

            second = build_corpus_from_manifest(
                manifest,
                cache_dir=cache,
                output=output,
                offline=True,
                allow_local_repositories=True,
            )
            self.assertEqual(second.output_sha256, first.output_sha256)
            self.assertEqual(output.read_bytes(), first_bytes)

    def test_builder_refuses_implicit_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, commit, document_sha, license_sha = self._create_source_repository(root)
            manifest = root / "manifest.json"
            self._write_manifest(
                manifest,
                repository=repository,
                commit=commit,
                document_sha=document_sha,
                license_sha=license_sha,
            )
            output = root / "public-corpus.json"
            cache = root / "cache"
            build_corpus_from_manifest(
                manifest,
                cache_dir=cache,
                output=output,
                allow_local_repositories=True,
            )
            output.write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(CorpusBuildError, "--overwrite"):
                build_corpus_from_manifest(
                    manifest,
                    cache_dir=cache,
                    output=output,
                    offline=True,
                    allow_local_repositories=True,
                )

    def test_offline_build_requires_existing_pinned_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            self._write_manifest(
                manifest,
                repository=root / "missing-source",
                commit="0" * 40,
                document_sha="1" * 64,
                license_sha="2" * 64,
            )
            with self.assertRaisesRegex(CorpusBuildError, "offline cache is missing"):
                build_corpus_from_manifest(
                    manifest,
                    cache_dir=root / "cache",
                    output=root / "corpus.json",
                    offline=True,
                    allow_local_repositories=True,
                )

    def test_manifest_rejects_short_commit_path_traversal_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            self._write_manifest(
                manifest,
                repository=root / "source",
                commit="abc123",
                document_sha="1" * 64,
                license_sha="2" * 64,
            )
            with self.assertRaisesRegex(CorpusBuildError, "full 40-character SHA-1"):
                load_corpus_manifest(manifest, allow_local_repositories=True)

            with self.assertRaisesRegex(CorpusBuildError, "HTTPS URL"):
                load_corpus_manifest(manifest)

            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["sources"][0]["commit"] = "0" * 40
            payload["sources"][0]["documents"][0]["path"] = "../secret.md"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(CorpusBuildError, "safe POSIX relative path"):
                load_corpus_manifest(manifest, allow_local_repositories=True)

            payload["sources"][0]["documents"][0]["path"] = "docs/guide.md"
            payload["unexpected"] = True
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(CorpusBuildError, "unknown"):
                load_corpus_manifest(manifest, allow_local_repositories=True)

    def _create_source_repository(self, root: Path) -> tuple[Path, str, str, str]:
        repository = root / "source"
        repository.mkdir()
        self._git(repository, "init")
        self._git(repository, "config", "user.name", "Corpus Test")
        self._git(repository, "config", "user.email", "corpus@example.invalid")
        (repository / "docs").mkdir()
        (repository / "LICENSE").write_text("Test license\n", encoding="utf-8")
        (repository / "docs" / "guide.md").write_text(
            "---\ntitle: ignored front matter\n---\n# Old Guide\n\npinned body\n",
            encoding="utf-8",
        )
        self._git(repository, "add", "LICENSE", "docs/guide.md")
        self._git(repository, "commit", "-m", "pinned source")
        commit = self._git(repository, "rev-parse", "HEAD").strip()
        document = self._git_bytes(
            repository, "cat-file", "blob", f"{commit}:docs/guide.md"
        )
        license_text = self._git_bytes(
            repository, "cat-file", "blob", f"{commit}:LICENSE"
        )

        (repository / "docs" / "guide.md").write_text(
            "# New Guide\n\nthis must not be ingested\n",
            encoding="utf-8",
        )
        self._git(repository, "add", "docs/guide.md")
        self._git(repository, "commit", "-m", "newer source")
        return (
            repository,
            commit,
            hashlib.sha256(document).hexdigest(),
            hashlib.sha256(license_text).hexdigest(),
        )

    @staticmethod
    def _write_manifest(
        target: Path,
        *,
        repository: Path,
        commit: str,
        document_sha: str,
        license_sha: str,
    ) -> None:
        payload = {
            "manifest_version": "git-markdown-corpus-v1",
            "corpus_id": "test-public-ops",
            "version": "1.0.0",
            "tenant_id": "demo",
            "sources": [
                {
                    "source_id": "test-source",
                    "repository_url": str(repository),
                    "commit": commit,
                    "source_uri_template": "https://example.invalid/blob/{commit}/{path}",
                    "source_type": "official-documentation",
                    "license": {
                        "spdx_id": "MIT",
                        "path": "LICENSE",
                        "url": "https://example.invalid/LICENSE",
                        "attribution": "Test Authors",
                        "content_sha256": license_sha,
                        "notice": None,
                    },
                    "documents": [
                        {
                            "document_id": "test-guide",
                            "title": "Test Guide",
                            "path": "docs/guide.md",
                            "content_sha256": document_sha,
                            "metadata": {
                                "product": "test",
                                "topic": "debugging",
                                "language": "en",
                            },
                        }
                    ],
                }
            ],
        }
        target.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        ).stdout

    @staticmethod
    def _git_bytes(repository: Path, *arguments: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout


if __name__ == "__main__":
    unittest.main()

