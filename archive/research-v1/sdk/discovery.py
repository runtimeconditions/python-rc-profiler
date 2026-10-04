from __future__ import annotations

import hashlib
import json
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

from ..constants import EXTENSION_KIND, SDK_MAPPING_API_VERSION, SDK_MAPPING_INDEX_KIND, SDK_MAPPING_KIND
from ..extension.definition import parse_extension_definition
from ..models import Diagnostic, SDKExtensionArtifact, SDKMappingArtifact
from ..util import as_map, ignored_path, scalar
from ..yamlio import Yaml


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def semantic_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require(value: Any, expected: Any, description: str) -> None:
    if value != expected:
        raise ValueError(f"{description}: got {value!r}, expected {expected!r}")


def safe_relative_path(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("indexed mapping path is required")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe indexed mapping path {value!r}")
    return path


class SDKArtifactDiscovery:
    def discover(self, roots: list[Path], include_installed: bool) -> tuple[list[SDKMappingArtifact], list[SDKExtensionArtifact], list[Diagnostic]]:
        mappings: list[SDKMappingArtifact] = []
        extensions: list[SDKExtensionArtifact] = []
        diagnostics: list[Diagnostic] = []
        seen_indexes: set[Path] = set()
        seen_extensions: set[Path] = set()
        for root in roots:
            resolved = root.absolute().resolve()
            for path in self._index_paths(resolved):
                if path in seen_indexes:
                    continue
                seen_indexes.add(path)
                try:
                    mappings.extend(self._load_index(path, lambda relative, base=resolved: self._resolve_explicit(base, path, relative)))
                except Exception as exc:
                    diagnostics.append(Diagnostic("error", "sdk-mapping", str(path), str(exc)))
            for path in self._extension_paths(resolved):
                if path in seen_extensions:
                    continue
                seen_extensions.add(path)
                try:
                    extension = self._load_extension(path)
                    if extension is not None:
                        extensions.append(extension)
                except Exception as exc:
                    diagnostics.append(Diagnostic("error", "sdk-extension", str(path), str(exc)))
        if include_installed:
            for distribution in metadata.distributions():
                for relative in distribution.files or []:
                    if not str(relative).endswith("/runtimeconditions/index.yaml"):
                        continue
                    path = Path(distribution.locate_file(relative)).resolve()
                    if path in seen_indexes:
                        continue
                    seen_indexes.add(path)
                    try:
                        mappings.extend(self._load_index(path, lambda item, current=distribution: Path(current.locate_file(item)).resolve(), distribution.metadata.get("Name"), distribution.version))
                    except Exception as exc:
                        diagnostics.append(Diagnostic("error", "sdk-mapping", str(path), str(exc)))
        mappings, mapping_diagnostics = self._dedupe_mappings(mappings)
        extensions, extension_diagnostics = self._dedupe_extensions(extensions)
        diagnostics.extend(mapping_diagnostics)
        diagnostics.extend(extension_diagnostics)
        return mappings, extensions, diagnostics

    def _index_paths(self, root: Path) -> list[Path]:
        if root.is_file():
            return [root] if root.name == "index.yaml" and root.parent.name == "runtimeconditions" else []
        if not root.is_dir():
            return []
        return [path.resolve() for path in sorted(root.rglob("index.yaml")) if path.parent.name == "runtimeconditions" and not ignored_path(path)]

    def _extension_paths(self, root: Path) -> list[Path]:
        if root.is_file():
            return [root] if root.name == "runtimeconditions.extension.yaml" else []
        if not root.is_dir():
            return []
        return [path.resolve() for path in sorted(root.rglob("runtimeconditions.extension.yaml")) if not ignored_path(path)]

    def _resolve_explicit(self, root: Path, index_path: Path, relative: Path) -> Path:
        candidates = [root / relative]
        candidates.extend(parent / relative for parent in index_path.parents)
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise ValueError(f"indexed mapping is absent: {relative.as_posix()}")

    def _load_index(self, index_path: Path, locate: Callable[[Path], Path], installed_name: str | None = None, installed_version: str | None = None) -> list[SDKMappingArtifact]:
        index = Yaml.load(index_path)
        require(index.get("apiVersion"), SDK_MAPPING_API_VERSION, "index apiVersion")
        require(index.get("kind"), SDK_MAPPING_INDEX_KIND, "index kind")
        index_metadata = as_map(index.get("metadata"))
        distribution = scalar(index_metadata.get("distribution")) or ""
        distribution_version = scalar(index_metadata.get("distributionVersion")) or ""
        require(scalar(index_metadata.get("language")), "python", "index language")
        if not distribution or not distribution_version:
            raise ValueError("index distribution and distributionVersion are required")
        if installed_name:
            require(distribution.lower().replace("_", "-"), installed_name.lower().replace("_", "-"), "installed distribution")
        if installed_version:
            require(distribution_version, installed_version, "installed distribution version")
        entries = index.get("mappings")
        if not isinstance(entries, list) or not entries:
            raise ValueError("index mappings are required")
        result: list[SDKMappingArtifact] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("index mapping entry must be a mapping")
            relative = safe_relative_path(entry.get("path"))
            mapping_path = locate(relative)
            expected_sha = scalar(entry.get("sha256")) or ""
            require(sha256(mapping_path), expected_sha, "mapping file digest")
            mapping = Yaml.load(mapping_path)
            require(mapping.get("apiVersion"), SDK_MAPPING_API_VERSION, "mapping apiVersion")
            require(mapping.get("kind"), SDK_MAPPING_KIND, "mapping kind")
            mapping_metadata = as_map(mapping.get("metadata"))
            name = scalar(mapping_metadata.get("name")) or ""
            require(name, scalar(entry.get("name")), "mapping name")
            require(scalar(mapping_metadata.get("distribution")), distribution, "mapping distribution")
            require(scalar(mapping_metadata.get("distributionVersion")), distribution_version, "mapping distribution version")
            require(scalar(mapping_metadata.get("language")), "python", "mapping language")
            require(scalar(mapping_metadata.get("semanticSha256")), semantic_sha256({"operations": mapping.get("operations", []), "python": mapping.get("python", {})}), "mapping semantic digest")
            result.append(SDKMappingArtifact(distribution, distribution_version, name, index_path, mapping_path, expected_sha, mapping))
        return result

    def _load_extension(self, path: Path) -> SDKExtensionArtifact | None:
        document = Yaml.load(path)
        if document.get("kind") != EXTENSION_KIND:
            return None
        metadata_value = as_map(document.get("metadata"))
        extension_id = scalar(metadata_value.get("id")) or ""
        version = scalar(metadata_value.get("version")) or ""
        semantic_digest = scalar(metadata_value.get("semanticSha256")) or ""
        if not extension_id or not version or not semantic_digest:
            return None
        require(semantic_digest, semantic_sha256(document.get("spec", {})), "extension semantic digest")
        definition = parse_extension_definition(document, extension_id, path.as_uri())
        return SDKExtensionArtifact(extension_id, version, semantic_digest, path, definition, document)

    def _dedupe_mappings(self, artifacts: list[SDKMappingArtifact]) -> tuple[list[SDKMappingArtifact], list[Diagnostic]]:
        result: list[SDKMappingArtifact] = []
        seen: dict[tuple[str, str, str], SDKMappingArtifact] = {}
        diagnostics: list[Diagnostic] = []
        for artifact in artifacts:
            key = (artifact.distribution, artifact.distribution_version, artifact.name)
            previous = seen.get(key)
            if previous is None:
                seen[key] = artifact
                result.append(artifact)
            elif previous.mapping_sha256 != artifact.mapping_sha256:
                diagnostics.append(Diagnostic("error", "sdk-mapping", str(artifact.mapping_path), f"conflicting SDK mapping {key} already discovered at {previous.mapping_path}"))
        return result, diagnostics

    def _dedupe_extensions(self, artifacts: list[SDKExtensionArtifact]) -> tuple[list[SDKExtensionArtifact], list[Diagnostic]]:
        result: list[SDKExtensionArtifact] = []
        seen: dict[tuple[str, str], SDKExtensionArtifact] = {}
        diagnostics: list[Diagnostic] = []
        for artifact in artifacts:
            key = (artifact.id, artifact.version)
            previous = seen.get(key)
            if previous is None:
                seen[key] = artifact
                result.append(artifact)
            elif previous.semantic_sha256 != artifact.semantic_sha256:
                diagnostics.append(Diagnostic("error", "sdk-extension", str(artifact.path), f"conflicting extension release {key} already discovered at {previous.path}"))
        return result, diagnostics
