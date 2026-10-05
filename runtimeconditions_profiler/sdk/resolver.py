from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..errors import RuntimeConditionsError
from ..source.python import workload_source_files
from ..yamlio import Yaml, dump_yaml


BUNDLE_INDEX = "runtimeconditions-resolved.yaml"


def referenced_distributions(project_root: Path) -> dict[str, str]:
    roots: set[str] = set()
    for source in workload_source_files(project_root.absolute().resolve()):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    package_map = importlib.metadata.packages_distributions()
    result: dict[str, str] = {}
    for root in roots:
        for distribution in package_map.get(root, []):
            try:
                result[distribution.lower().replace("_", "-")] = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                continue
    return result


def _read_location(location: str, base: str | None = None) -> tuple[bytes, str]:
    parsed = urllib.parse.urlparse(location)
    if base and not parsed.scheme:
        location = urllib.parse.urljoin(base, location)
        parsed = urllib.parse.urlparse(location)
    if parsed.scheme in ("http", "https", "file"):
        with urllib.request.urlopen(location) as response:
            return response.read(), location
    path = Path(location).absolute().resolve()
    return path.read_bytes(), path.as_uri()


def _semantic_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def resolve_mappings(project_root: Path, catalog_location: str, destination: Path) -> list[dict[str, Any]]:
    catalog_bytes, catalog_url = _read_location(catalog_location)
    catalog = Yaml.load_text(catalog_bytes.decode("utf-8"))
    if catalog.get("kind") != "RuntimeConditionsSDKMappingCatalog" or catalog.get("ecosystem") != "pypi":
        raise RuntimeConditionsError("mapping resolver requires a pypi Runtime Conditions catalog")
    referenced = referenced_distributions(project_root)
    matches = [
        entry
        for entry in catalog.get("entries", [])
        if isinstance(entry, dict)
        and referenced.get(str(entry.get("package", "")).lower().replace("_", "-")) == str(entry.get("version"))
    ]
    destination = destination.absolute().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    resolved: list[dict[str, Any]] = []
    for entry in matches:
        mapping_record = entry.get("mapping", {})
        extension_record = entry.get("extension", {})
        if not isinstance(mapping_record, dict) or not isinstance(extension_record, dict):
            raise RuntimeConditionsError("catalog entry has invalid mapping or extension metadata")
        mapping_bytes, _ = _read_location(str(mapping_record.get("url", "")), catalog_url)
        mapping_digest = hashlib.sha256(mapping_bytes).hexdigest()
        if mapping_digest != mapping_record.get("sha256"):
            raise RuntimeConditionsError(f"mapping {mapping_record.get('name')} failed integrity validation")
        mapping_doc = Yaml.load_text(mapping_bytes.decode("utf-8"))
        extension_bytes, _ = _read_location(str(extension_record.get("url", "")), catalog_url)
        extension_doc = Yaml.load_text(extension_bytes.decode("utf-8"))
        extension_digest = _semantic_sha256(extension_doc.get("spec", {}))
        if extension_digest != extension_record.get("semanticSha256"):
            raise RuntimeConditionsError(f"extension {extension_record.get('id')} failed semantic integrity validation")
        if mapping_doc.get("extension", {}).get("id") != extension_record.get("id"):
            raise RuntimeConditionsError(f"mapping {mapping_record.get('name')} selects a different extension")
        safe_package = str(entry["package"]).replace("/", "-")
        safe_name = str(mapping_record["name"]).replace("/", "-")
        root = destination / "artifacts" / safe_package / str(entry["version"]) / safe_name
        root.mkdir(parents=True, exist_ok=True)
        mapping_path = root / "runtimeconditions.sdk-mapping.yaml"
        extension_path = root / "runtimeconditions.extension.yaml"
        mapping_path.write_bytes(mapping_bytes)
        extension_path.write_bytes(extension_bytes)
        resolved.append(
            {
                "ecosystem": "pypi",
                "package": entry["package"],
                "packageVersion": str(entry["version"]),
                "mapping": str(mapping_path.relative_to(destination)),
                "extension": str(extension_path.relative_to(destination)),
                "mappingSha256": mapping_digest,
                "extensionSemanticSha256": extension_digest,
            }
        )
    index = {
        "apiVersion": "runtimeconditions.io/resolved-sdk-mappings/v1alpha1",
        "kind": "RuntimeConditionsResolvedSDKMappings",
        "entries": resolved,
    }
    (destination / BUNDLE_INDEX).write_text(dump_yaml(index), encoding="utf-8")
    return resolved


def bundle_artifact_paths(
    project_root: Path, bundle_paths: list[Path]
) -> tuple[list[Path], list[Path]]:
    referenced = referenced_distributions(project_root)
    mappings: list[Path] = []
    extensions: list[Path] = []
    for raw_path in bundle_paths:
        root = raw_path.absolute().resolve()
        index_path = root / BUNDLE_INDEX if root.is_dir() else root
        index = Yaml.load(index_path)
        if index.get("kind") != "RuntimeConditionsResolvedSDKMappings":
            raise RuntimeConditionsError(f"{index_path}: expected a resolved SDK mapping bundle")
        base = index_path.parent
        for entry in index.get("entries", []):
            if not isinstance(entry, dict):
                continue
            package = str(entry.get("package", "")).lower().replace("_", "-")
            if referenced.get(package) != str(entry.get("packageVersion")):
                continue
            mapping = (base / str(entry.get("mapping", ""))).resolve()
            extension = (base / str(entry.get("extension", ""))).resolve()
            if hashlib.sha256(mapping.read_bytes()).hexdigest() != entry.get("mappingSha256"):
                raise RuntimeConditionsError(f"{mapping}: cached mapping failed integrity validation")
            extension_document = Yaml.load(extension)
            if _semantic_sha256(extension_document.get("spec", {})) != entry.get("extensionSemanticSha256"):
                raise RuntimeConditionsError(f"{extension}: cached extension failed semantic integrity validation")
            mappings.append(mapping)
            extensions.append(extension)
    return mappings, extensions
