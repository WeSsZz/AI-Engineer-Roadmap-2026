from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .retrieval import load_corpus


PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_PUBLIC_CORPUS_MANIFEST = (
    PACKAGE_ROOT / "corpus_manifests" / "public-ops-v1.json"
)
MANIFEST_VERSION = "git-markdown-corpus-v1"
TRANSFORM_VERSION = "markdown-frontmatter-strip-v1"
TRANSFORM_DESCRIPTION = (
    "Removed leading YAML/TOML front matter when present; normalized UTF-8 BOM "
    "and line endings; trimmed document boundaries; added one final newline."
)
MAX_DOCUMENT_BYTES = 1_000_000
MAX_AUXILIARY_BYTES = 1_000_000
MAX_CORPUS_BYTES = 25_000_000
GIT_COMMAND_TIMEOUT_SECONDS = 60
_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_RESERVED_METADATA = {
    "attribution",
    "license_content_sha256",
    "license_url",
    "modifications",
    "manifest_sha256",
    "normalized_content_sha256",
    "notice_content_sha256",
    "notice_url",
    "raw_content_sha256",
    "repository_url",
    "source_commit",
    "source_id",
    "source_snapshot_at",
    "source_path",
    "transform_version",
}


class CorpusBuildError(ValueError):
    """The corpus manifest, pinned source, or generated artifact is invalid."""


@dataclass(frozen=True)
class LicenseSpec:
    spdx_id: str
    path: str
    url: str
    attribution: str
    content_sha256: str
    notice_path: str | None = None
    notice_url: str | None = None
    notice_sha256: str | None = None


@dataclass(frozen=True)
class DocumentSpec:
    document_id: str
    title: str
    path: str
    content_sha256: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    repository_url: str
    commit: str
    source_uri_template: str
    source_type: str
    license: LicenseSpec
    documents: tuple[DocumentSpec, ...]


@dataclass(frozen=True)
class CorpusManifest:
    corpus_id: str
    version: str
    tenant_id: str
    sources: tuple[SourceSpec, ...]
    manifest_sha256: str


@dataclass(frozen=True)
class BuildReceipt:
    corpus_version: str
    document_count: int
    manifest_sha256: str
    output_sha256: str
    output: str
    sources: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_version": self.corpus_version,
            "document_count": self.document_count,
            "manifest_sha256": self.manifest_sha256,
            "output_sha256": self.output_sha256,
            "output": self.output,
            "sources": list(self.sources),
        }


def load_corpus_manifest(
    path: str | Path = DEFAULT_PUBLIC_CORPUS_MANIFEST,
    *,
    allow_local_repositories: bool = False,
) -> CorpusManifest:
    target = Path(path)
    try:
        raw = target.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusBuildError(f"cannot load corpus manifest {target}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CorpusBuildError("manifest must contain a JSON object")
    _require_fields(
        payload,
        {"manifest_version", "corpus_id", "version", "tenant_id", "sources"},
        "manifest",
    )
    if payload["manifest_version"] != MANIFEST_VERSION:
        raise CorpusBuildError(
            f"manifest.manifest_version must be {MANIFEST_VERSION!r}"
        )
    sources_payload = payload["sources"]
    if not isinstance(sources_payload, list) or not sources_payload:
        raise CorpusBuildError("manifest.sources must be a non-empty array")

    sources = tuple(
        _decode_source(value, index, allow_local_repositories=allow_local_repositories)
        for index, value in enumerate(sources_payload)
    )
    _require_unique((source.source_id for source in sources), "source_id")
    all_document_ids = (
        document.document_id for source in sources for document in source.documents
    )
    _require_unique(all_document_ids, "document_id")
    return CorpusManifest(
        corpus_id=_identifier(payload["corpus_id"], "manifest.corpus_id"),
        version=_string(payload["version"], "manifest.version"),
        tenant_id=_string(payload["tenant_id"], "manifest.tenant_id"),
        sources=sources,
        manifest_sha256=_sha256(raw),
    )


def build_corpus_from_manifest(
    manifest_path: str | Path,
    *,
    cache_dir: str | Path,
    output: str | Path,
    offline: bool = False,
    overwrite: bool = False,
    allow_local_repositories: bool = False,
) -> BuildReceipt:
    manifest = load_corpus_manifest(
        manifest_path,
        allow_local_repositories=allow_local_repositories,
    )
    documents: list[dict[str, Any]] = []
    source_receipts: list[dict[str, str]] = []
    total_raw_bytes = 0

    for source in sorted(manifest.sources, key=lambda item: item.source_id):
        repository = _ensure_source(source, Path(cache_dir), offline=offline)
        commit_time = _git_text(
            repository, "show", "-s", "--format=%cI", source.commit
        ).strip()
        if not commit_time:
            raise CorpusBuildError(f"source {source.source_id!r} has no commit timestamp")

        license_bytes = _read_regular_blob(
            repository,
            source.commit,
            source.license.path,
            offline=offline,
            max_bytes=MAX_AUXILIARY_BYTES,
        )
        _verify_sha256(
            license_bytes,
            source.license.content_sha256,
            f"source {source.source_id!r} license",
        )
        if not license_bytes.strip():
            raise CorpusBuildError(f"source {source.source_id!r} license is empty")

        notice_sha = None
        if source.license.notice_path is not None:
            notice_bytes = _read_regular_blob(
                repository,
                source.commit,
                source.license.notice_path,
                offline=offline,
                max_bytes=MAX_AUXILIARY_BYTES,
            )
            assert source.license.notice_sha256 is not None
            _verify_sha256(
                notice_bytes,
                source.license.notice_sha256,
                f"source {source.source_id!r} notice",
            )
            if not notice_bytes.strip():
                raise CorpusBuildError(f"source {source.source_id!r} notice is empty")
            notice_sha = _sha256(notice_bytes)

        for spec in sorted(source.documents, key=lambda item: item.document_id):
            raw = _read_regular_blob(
                repository,
                source.commit,
                spec.path,
                offline=offline,
            )
            total_raw_bytes += len(raw)
            if total_raw_bytes > MAX_CORPUS_BYTES:
                raise CorpusBuildError(
                    f"corpus exceeds the {MAX_CORPUS_BYTES}-byte source limit"
                )
            _verify_sha256(raw, spec.content_sha256, f"document {spec.document_id!r}")
            text = _normalize_markdown(raw, spec.document_id)
            raw_sha = _sha256(raw)
            normalized_sha = _sha256(text.encode("utf-8"))
            source_uri = (
                source.source_uri_template.replace("{commit}", source.commit)
                .replace("{path}", spec.path)
            )
            metadata = {
                **spec.metadata,
                "source_id": source.source_id,
                "repository_url": source.repository_url,
                "source_commit": source.commit,
                "source_path": spec.path,
                "raw_content_sha256": raw_sha,
                "normalized_content_sha256": normalized_sha,
                "manifest_sha256": manifest.manifest_sha256,
                "license_url": source.license.url,
                "license_content_sha256": _sha256(license_bytes),
                "attribution": source.license.attribution,
                "modifications": TRANSFORM_DESCRIPTION,
                "source_snapshot_at": commit_time,
                "transform_version": TRANSFORM_VERSION,
            }
            if source.license.notice_url is not None:
                assert notice_sha is not None
                metadata["notice_url"] = source.license.notice_url
                metadata["notice_content_sha256"] = notice_sha
            documents.append(
                {
                    "document_id": spec.document_id,
                    "title": spec.title,
                    "text": text,
                    "source_uri": source_uri,
                    "source_type": source.source_type,
                    "tenant_id": manifest.tenant_id,
                    "document_version": source.commit,
                    "effective_at": commit_time,
                    "license": source.license.spdx_id,
                    "provenance": (
                        f"{source.repository_url}@{source.commit}:{spec.path}"
                    ),
                    "metadata": metadata,
                }
            )
        source_receipts.append(
            {
                "source_id": source.source_id,
                "repository_url": source.repository_url,
                "commit": source.commit,
                "license": source.license.spdx_id,
            }
        )

    payload = {
        "corpus_id": manifest.corpus_id,
        "version": manifest.version,
        "documents": sorted(documents, key=lambda item: item["document_id"]),
    }
    output_path = Path(output)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_validated_corpus(encoded, output_path, overwrite=overwrite)
    return BuildReceipt(
        corpus_version=f"{manifest.corpus_id}:{manifest.version}",
        document_count=len(documents),
        manifest_sha256=manifest.manifest_sha256,
        output_sha256=_sha256(encoded),
        output=str(output_path),
        sources=tuple(source_receipts),
    )


def _decode_source(
    value: Any,
    index: int,
    *,
    allow_local_repositories: bool,
) -> SourceSpec:
    location = f"manifest.sources[{index}]"
    if not isinstance(value, dict):
        raise CorpusBuildError(f"{location} must be an object")
    _require_fields(
        value,
        {
            "source_id",
            "repository_url",
            "commit",
            "source_uri_template",
            "source_type",
            "license",
            "documents",
        },
        location,
    )
    repository_url = _validate_repository_url(
        value["repository_url"],
        f"{location}.repository_url",
        allow_local=allow_local_repositories,
    )
    commit = _string(value["commit"], f"{location}.commit").lower()
    if not _COMMIT_PATTERN.fullmatch(commit):
        raise CorpusBuildError(f"{location}.commit must be a full 40-character SHA-1 commit ID")
    template = _string(value["source_uri_template"], f"{location}.source_uri_template")
    placeholders = set(re.findall(r"\{([^{}]+)\}", template))
    if placeholders != {"commit", "path"}:
        raise CorpusBuildError(
            f"{location}.source_uri_template must contain only {{commit}} and {{path}}"
        )
    documents_payload = value["documents"]
    if not isinstance(documents_payload, list) or not documents_payload:
        raise CorpusBuildError(f"{location}.documents must be a non-empty array")
    documents = tuple(
        _decode_document(item, location, document_index)
        for document_index, item in enumerate(documents_payload)
    )
    _require_unique(
        (document.document_id for document in documents),
        f"{location}.document_id",
    )
    return SourceSpec(
        source_id=_identifier(value["source_id"], f"{location}.source_id"),
        repository_url=repository_url,
        commit=commit,
        source_uri_template=template,
        source_type=_identifier(value["source_type"], f"{location}.source_type"),
        license=_decode_license(value["license"], location),
        documents=documents,
    )


def _decode_license(value: Any, source_location: str) -> LicenseSpec:
    location = f"{source_location}.license"
    if not isinstance(value, dict):
        raise CorpusBuildError(f"{location} must be an object")
    _require_fields(
        value,
        {"spdx_id", "path", "url", "attribution", "content_sha256", "notice"},
        location,
    )
    notice = value["notice"]
    notice_path = notice_url = notice_sha = None
    if notice is not None:
        if not isinstance(notice, dict):
            raise CorpusBuildError(f"{location}.notice must be null or an object")
        _require_fields(notice, {"path", "url", "content_sha256"}, f"{location}.notice")
        notice_path = _repository_path(notice["path"], f"{location}.notice.path")
        notice_url = _string(notice["url"], f"{location}.notice.url")
        notice_sha = _sha256_string(
            notice["content_sha256"], f"{location}.notice.content_sha256"
        )
    return LicenseSpec(
        spdx_id=_string(value["spdx_id"], f"{location}.spdx_id"),
        path=_repository_path(value["path"], f"{location}.path"),
        url=_string(value["url"], f"{location}.url"),
        attribution=_string(value["attribution"], f"{location}.attribution"),
        content_sha256=_sha256_string(
            value["content_sha256"], f"{location}.content_sha256"
        ),
        notice_path=notice_path,
        notice_url=notice_url,
        notice_sha256=notice_sha,
    )


def _decode_document(value: Any, source_location: str, index: int) -> DocumentSpec:
    location = f"{source_location}.documents[{index}]"
    if not isinstance(value, dict):
        raise CorpusBuildError(f"{location} must be an object")
    _require_fields(
        value,
        {"document_id", "title", "path", "content_sha256", "metadata"},
        location,
    )
    path = _repository_path(value["path"], f"{location}.path")
    if PurePosixPath(path).suffix.lower() not in {".md", ".markdown"}:
        raise CorpusBuildError(f"{location}.path must point to Markdown")
    metadata = value["metadata"]
    if not isinstance(metadata, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in metadata.items()
    ):
        raise CorpusBuildError(f"{location}.metadata must be a string map")
    overlap = _RESERVED_METADATA.intersection(metadata)
    if overlap:
        raise CorpusBuildError(
            f"{location}.metadata uses reserved fields: {sorted(overlap)}"
        )
    return DocumentSpec(
        document_id=_identifier(value["document_id"], f"{location}.document_id"),
        title=_string(value["title"], f"{location}.title"),
        path=path,
        content_sha256=_sha256_string(
            value["content_sha256"], f"{location}.content_sha256"
        ),
        metadata=dict(metadata),
    )


def _ensure_source(source: SourceSpec, cache_dir: Path, *, offline: bool) -> Path:
    repository = cache_dir / f"{source.source_id}.git"
    env = _git_environment(offline=offline)
    if not repository.exists():
        if offline:
            raise CorpusBuildError(
                f"offline cache is missing source {source.source_id!r}: {repository}"
            )
        repository.parent.mkdir(parents=True, exist_ok=True)
        _run(("git", "init", "--bare", str(repository)), env=env)
        _git(repository, "remote", "add", "origin", source.repository_url, env=env)
    elif _git_text(
        repository, "rev-parse", "--is-bare-repository", env=env
    ).strip() != "true":
        raise CorpusBuildError(f"source cache is not a bare Git repository: {repository}")

    remote = _git(repository, "remote", "get-url", "origin", env=env, check=False)
    if remote.returncode != 0:
        _git(repository, "remote", "add", "origin", source.repository_url, env=env)
    elif remote.stdout.decode("utf-8", errors="replace").strip() != source.repository_url:
        raise CorpusBuildError(
            f"source cache origin mismatch for {source.source_id!r}"
        )

    commit_ref = f"{source.commit}^{{commit}}"
    present = _git(repository, "cat-file", "-e", commit_ref, env=env, check=False)
    if present.returncode != 0:
        if offline:
            raise CorpusBuildError(
                f"offline cache does not contain commit {source.commit} for {source.source_id!r}"
            )
        _git_with_retry(
            repository,
            "fetch",
            "--depth=1",
            "--filter=blob:none",
            "--no-tags",
            "origin",
            source.commit,
            env=env,
        )
    resolved = _git_text(repository, "rev-parse", commit_ref, env=env).strip().lower()
    if resolved != source.commit:
        raise CorpusBuildError(
            f"source {source.source_id!r} resolved to {resolved}, expected {source.commit}"
        )
    return repository


def _read_regular_blob(
    repository: Path,
    commit: str,
    path: str,
    *,
    offline: bool = False,
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> bytes:
    env = _git_environment(offline=offline)
    tree = _git(repository, "ls-tree", "-z", commit, "--", path, env=env)
    record = tree.stdout.rstrip(b"\0")
    if not record or b"\t" not in record:
        raise CorpusBuildError(f"pinned source path does not exist: {path}")
    header, returned_path = record.split(b"\t", 1)
    parts = header.split()
    if len(parts) != 3 or parts[0] not in {b"100644", b"100755"} or parts[1] != b"blob":
        raise CorpusBuildError(f"pinned source path is not a regular file: {path}")
    if returned_path.decode("utf-8", errors="strict") != path:
        raise CorpusBuildError(f"Git returned an unexpected path for {path}")
    object_id = parts[2].decode("ascii", errors="strict")
    size_text = _git_text(
        repository,
        "cat-file",
        "-s",
        object_id,
        env=env,
    ).strip()
    try:
        size = int(size_text)
    except ValueError as exc:
        raise CorpusBuildError(f"Git returned an invalid blob size for {path}") from exc
    if size < 0 or size > max_bytes:
        raise CorpusBuildError(
            f"pinned source file {path!r} is {size} bytes; limit is {max_bytes}"
        )
    result = _git_with_retry(
        repository,
        "cat-file",
        "blob",
        object_id,
        env=env,
        attempts=1 if offline else 4,
    ).stdout
    if len(result) != size:
        raise CorpusBuildError(f"Git returned an incomplete blob for {path}")
    return result



def _normalize_markdown(raw: bytes, document_id: str) -> str:
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise CorpusBuildError(
            f"document {document_id!r} exceeds the {MAX_DOCUMENT_BYTES}-byte limit"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CorpusBuildError(f"document {document_id!r} is not UTF-8: {exc}") from exc
    if "\0" in text:
        raise CorpusBuildError(f"document {document_id!r} contains a NUL byte")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[0].strip() in {"---", "+++"}:
        delimiter = lines[0].strip()
        closing = next(
            (
                index
                for index, line in enumerate(lines[1:201], start=1)
                if line.strip() == delimiter
            ),
            None,
        )
        if closing is None:
            raise CorpusBuildError(
                f"document {document_id!r} has unterminated front matter"
            )
        lines = lines[closing + 1 :]
    normalized = "\n".join(lines).strip()
    if not normalized:
        raise CorpusBuildError(f"document {document_id!r} has no Markdown body")
    return normalized + "\n"


def _write_validated_corpus(encoded: bytes, target: Path, *, overwrite: bool) -> None:
    if target.exists():
        existing = target.read_bytes()
        if existing == encoded:
            load_corpus(target)
            return
        if not overwrite:
            raise CorpusBuildError(
                f"refusing to replace different corpus output without --overwrite: {target}"
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(encoded)
        load_corpus(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_repository_url(
    value: Any,
    location: str,
    *,
    allow_local: bool,
) -> str:
    repository = _string(value, location)
    if any(ord(character) < 32 for character in repository):
        raise CorpusBuildError(f"{location} contains control characters")
    if "://" not in repository:
        candidate = Path(repository)
        if (
            allow_local
            and candidate.is_absolute()
            and not repository.startswith("-")
            and "::" not in repository
        ):
            return str(candidate.resolve())
        raise CorpusBuildError(
            f"{location} must be an HTTPS URL; local paths are test-only"
        )
    parsed = urlsplit(repository)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path in {"", "/"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CorpusBuildError(
            f"{location} must be an HTTPS repository URL without credentials, query, or fragment"
        )
    return repository


def _repository_path(value: Any, location: str) -> str:
    path = _string(value, location)
    candidate = PurePosixPath(path)
    if (
        path.startswith("/")
        or "\\" in path
        or ":" in path
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or any(ord(character) < 32 for character in path)
    ):
        raise CorpusBuildError(f"{location} must be a safe POSIX relative path")
    return path


def _identifier(value: Any, location: str) -> str:
    identifier = _string(value, location)
    if not _ID_PATTERN.fullmatch(identifier):
        raise CorpusBuildError(
            f"{location} must match lowercase [a-z0-9][a-z0-9._-]*"
        )
    return identifier


def _sha256_string(value: Any, location: str) -> str:
    digest = _string(value, location).lower()
    if not _SHA256_PATTERN.fullmatch(digest):
        raise CorpusBuildError(f"{location} must be a SHA-256 hex digest")
    return digest


def _verify_sha256(raw: bytes, expected: str, location: str) -> None:
    actual = _sha256(raw)
    if actual != expected:
        raise CorpusBuildError(
            f"{location} SHA-256 mismatch: expected {expected}, got {actual}"
        )


def _require_unique(values: Any, location: str) -> None:
    seen: dict[str, str] = {}
    for value in values:
        folded = value.casefold()
        if folded in seen:
            raise CorpusBuildError(
                f"{location} values must be unique, including case: "
                f"{seen[folded]!r}, {value!r}"
            )
        seen[folded] = value


def _require_fields(value: dict[str, Any], expected: set[str], location: str) -> None:
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)}")
        if unknown:
            details.append(f"unknown={sorted(unknown)}")
        raise CorpusBuildError(f"{location} has invalid fields: {', '.join(details)}")


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorpusBuildError(f"{location} must be a non-empty string")
    return value


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _git_environment(*, offline: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("GIT_CONFIG_") or key in {
            "GIT_ALLOW_PROTOCOL",
            "GIT_PROTOCOL_FROM_USER",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
        }:
            env.pop(key, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_ALLOW_PROTOCOL"] = "https:file"
    env["GIT_PROTOCOL_FROM_USER"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if offline:
        env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def _git(
    repository: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return _run(
        ("git", "--git-dir", str(repository), *arguments),
        env=env or _git_environment(),
        check=check,
    )


def _git_text(
    repository: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> str:
    return _git(repository, *arguments, env=env).stdout.decode("utf-8", errors="strict")


def _git_with_retry(
    repository: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    attempts: int = 4,
) -> subprocess.CompletedProcess[bytes]:
    last_error: CorpusBuildError | None = None
    for attempt in range(attempts):
        try:
            return _git(repository, *arguments, env=env)
        except CorpusBuildError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.5 * (2**attempt))
    assert last_error is not None
    raise last_error


def _run(
    arguments: tuple[str, ...],
    *,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=env,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CorpusBuildError(
            f"Git command timed out after {GIT_COMMAND_TIMEOUT_SECONDS}s: "
            f"{' '.join(arguments[:3])}"
        ) from exc
    except OSError as exc:
        raise CorpusBuildError(f"cannot execute {arguments[0]!r}: {exc}") from exc
    if check and completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise CorpusBuildError(
            f"Git command failed ({' '.join(arguments[:3])}): "
            f"{detail or completed.returncode}"
        )
    return completed

