"""Verify installed generated bindings using static package metadata and data.

The versioned schemas in schemas.json are JSON representations of the approved
extension-binding schemas and ship with the profiler wheel. No application or
binding module is imported, and no site-packages directory is searched
recursively.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata, resources
from pathlib import Path, PurePosixPath
from typing import Any

import rfc8785
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from ..errors import RuntimeConditionsError
from ..extension.identity import definition_identifier, parse_identifier
from .installed import (
    MAX_RESOURCE_BYTES,
    RESOURCE_NAMES,
    InstalledBindingDiscoveryResult,
    InstalledBindingPackage,
)


SCHEMAS = {
    "runtimeconditions.bindings.yaml": "runtimeconditions.binding-manifest.schema.yaml",
    "runtimeconditions.binding-model.yaml": "runtimeconditions.binding-model.schema.yaml",
    "runtimeconditions.extension.yaml": "runtimeconditions.extension-semantic.schema.yaml",
    "runtimeconditions.binding-release.yaml": "runtimeconditions.binding-release.schema.yaml",
}


@dataclass(frozen=True)
class VerifiedBindingPackage:
    installed: InstalledBindingPackage
    manifest: dict[str, Any]
    model: dict[str, Any]
    extension: dict[str, Any]
    release: dict[str, Any]


@dataclass(frozen=True)
class VerifiedBindingSet:
    packages: tuple[VerifiedBindingPackage, ...]
    imported_packages: tuple[VerifiedBindingPackage, ...]


class _StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError("YAML mapping keys must be strings")
            if key in result:
                raise ValueError(f"duplicate YAML mapping key {key!r}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _yaml_document(data: bytes, name: str) -> dict[str, Any]:
    try:
        if data.startswith(b"\xef\xbb\xbf"):
            raise ValueError("UTF-8 byte-order mark is not supported")
        text = data.decode("utf-8")
        restricted_model = name.endswith("/runtimeconditions.binding-model.yaml")
        aliases = 0
        for token in yaml.scan(text):
            if isinstance(token, yaml.tokens.AliasToken):
                aliases += 1
                if aliases > 100:
                    raise ValueError("YAML input exceeds 100 alias references")
            if restricted_model and isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
                raise ValueError("binding model YAML cannot contain anchors or aliases")
        value = yaml.load(text, Loader=_StrictLoader)
    except (UnicodeError, yaml.YAMLError, ValueError, RecursionError) as exc:
        raise RuntimeConditionsError(f"{name}: invalid YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeConditionsError(f"{name}: YAML document must be a mapping")
    pending: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 1_000_000 or depth > 256:
            raise RuntimeConditionsError(f"{name}: YAML document exceeds node or nesting limit")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


@lru_cache(maxsize=1)
def _schemas() -> dict[str, dict[str, Any]]:
    try:
        bundled = resources.files("runtimeconditions_profiler").joinpath("schemas.json")
        schemas = json.loads(bundled.read_text(encoding="utf-8"))
        if set(schemas) != set(SCHEMAS.values()):
            raise ValueError("schema bundle is incomplete")
        for schema in schemas.values():
            Draft202012Validator.check_schema(schema)
        return schemas
    except (OSError, ValueError, SchemaError) as exc:
        raise RuntimeConditionsError(f"profiler schema bundle is unavailable: {exc}") from exc


def _validate(document: dict[str, Any], schema: dict[str, Any], name: str) -> None:
    try:
        first = next(Draft202012Validator(schema).iter_errors(document), None)
    except RecursionError as exc:
        raise RuntimeConditionsError(f"{name}: schema validation exceeded nesting limit") from exc
    if first is not None:
        location = "/" + "/".join(map(str, first.absolute_path))
        raise RuntimeConditionsError(f"{name}{location}: {first.message}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(value: Any) -> str:
    try:
        return _sha256(rfc8785.dumps(value))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise RuntimeConditionsError(f"cannot canonicalize binding data: {exc}") from exc


def _schema_ref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    seen: set[str] = set()
    while "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/$defs/") or ref in seen:
            raise RuntimeConditionsError(f"unsupported semantic schema reference {ref!r}")
        seen.add(ref)
        schema = root["$defs"][ref.removeprefix("#/$defs/")]
    return schema


def _branch_matches(value: Any, branch: dict[str, Any], root: dict[str, Any]) -> bool:
    branch = _schema_ref(branch, root)
    if "const" in branch and branch["const"] != value:
        return False
    kinds = branch.get("type")
    if kinds is None:
        return True
    kinds = [kinds] if isinstance(kinds, str) else kinds
    actual = (
        "null" if value is None else "boolean" if isinstance(value, bool) else
        "object" if isinstance(value, dict) else "array" if isinstance(value, list) else
        "string" if isinstance(value, str) else "integer" if isinstance(value, int) else
        "number" if isinstance(value, float) else "unknown"
    )
    return actual in kinds or (actual == "integer" and "number" in kinds)


def _semantic_value(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> Any:
    schema = _schema_ref(schema, root)
    if "oneOf" in schema:
        branches = [
            _schema_ref(branch, root)
            for branch in schema["oneOf"]
            if _branch_matches(value, branch, root)
        ]
        if len(branches) != 1:
            raise RuntimeConditionsError("semantic schema has ambiguous oneOf branch")
        schema = branches[0]
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties")
        result: dict[str, Any] = {}
        for key, child in value.items():
            child_schema = properties.get(key, additional)
            if not isinstance(child_schema, dict):
                raise RuntimeConditionsError(f"semantic schema has no rule for {key!r}")
            result[key] = _semantic_value(child, child_schema, root)
        return result
    if isinstance(value, list):
        ordering = schema.get("x-runtimeconditions-ordering")
        if ordering not in ("set", "source") or not isinstance(schema.get("items"), dict):
            raise RuntimeConditionsError("semantic array has no ordering or item rule")
        items = [_semantic_value(item, schema["items"], root) for item in value]
        if ordering == "set":
            items.sort(key=rfc8785.dumps)
        return items
    return value


def _recorded_bytes(
    distribution: metadata.Distribution, path: str, content: bytes | None = None
) -> bytes:
    files = distribution.files
    if files is None:
        raise RuntimeConditionsError(f"{distribution.metadata['Name']}: installed distribution has no file record")
    matching = [item for item in files if str(item).replace("\\", "/") == path]
    if not matching:
        raise RuntimeConditionsError(f"{distribution.metadata['Name']}: missing recorded file {path}")
    if len(matching) != 1:
        raise RuntimeConditionsError(f"{distribution.metadata['Name']}: duplicate file record {path}")
    record = matching[0]
    file_hash = getattr(record, "hash", None)
    if file_hash is None or file_hash.mode != "sha256":
        raise RuntimeConditionsError(f"{distribution.metadata['Name']}: {path} has no SHA-256 RECORD hash")
    location = Path(distribution.locate_file(record))
    package_dir = Path(distribution.locate_file(PurePosixPath(path.partition("/")[0]))).resolve()
    if not location.resolve().is_relative_to(package_dir):
        raise RuntimeConditionsError(f"{distribution.metadata['Name']}: {path} escapes its package location")
    if content is None:
        try:
            with location.open("rb") as stream:
                data = stream.read(MAX_RESOURCE_BYTES + 1)
        except OSError as exc:
            raise RuntimeConditionsError(f"{distribution.metadata['Name']}: cannot read {path}: {exc}") from exc
    else:
        data = content
    if len(data) > MAX_RESOURCE_BYTES:
        raise RuntimeConditionsError(f"{distribution.metadata['Name']}: {path} exceeds {MAX_RESOURCE_BYTES} bytes")
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    if digest != file_hash.value:
        raise RuntimeConditionsError(f"{distribution.metadata['Name']}: {path} fails installed RECORD hash")
    return data


def _load_dependency(name: str, import_package: str) -> InstalledBindingPackage:
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError as exc:
        raise RuntimeConditionsError(f"required binding distribution {name} is not installed") from exc
    actual_name = distribution.metadata.get("Name", "")
    if canonicalize_name(actual_name) != canonicalize_name(name):
        raise RuntimeConditionsError(f"binding dependency {name} resolved as {actual_name}")
    owners = metadata.packages_distributions().get(import_package, ())
    if len(owners) != 1 or canonicalize_name(owners[0]) != canonicalize_name(name):
        raise RuntimeConditionsError(f"binding dependency {name} does not uniquely own {import_package}")
    if distribution.files is None or not any(
        str(item).replace("\\", "/") == f"{import_package}/__init__.py" for item in distribution.files
    ):
        raise RuntimeConditionsError(f"binding dependency {name} does not own {import_package}/__init__.py")
    if not distribution.version:
        raise RuntimeConditionsError(f"binding dependency {name} has no installed version")
    resource_paths: dict[str, Path] = {}
    resource_bytes: dict[str, bytes] = {}
    for resource in RESOURCE_NAMES:
        path = f"{import_package}/{resource}"
        resource_bytes[resource] = _recorded_bytes(distribution, path)
        resource_paths[resource] = Path(distribution.locate_file(PurePosixPath(path)))
    return InstalledBindingPackage(import_package, actual_name, distribution.version, resource_paths, resource_bytes)


def _model_locations(model: dict[str, Any]) -> set[tuple[str, str | None]]:
    locations: set[tuple[str, str | None]] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            provenance = value.get("provenance")
            if isinstance(provenance, dict) and "coordinate" in provenance:
                locations.add((provenance["coordinate"], provenance.get("jsonPointer")))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(model.get("vocabulary", {}))
    walk(model.get("schemas", []))
    return locations


def _verify_manifest_edges(manifest: dict[str, Any], model: dict[str, Any], name: str) -> None:
    locations = _model_locations(model)
    types = {item["nativeName"] for item in manifest["types"]}
    if len(types) != len(manifest["types"]):
        raise RuntimeConditionsError(f"{name}: duplicate native type names")
    declarations = {item["function"] for item in manifest["declarations"]}
    if len(declarations) != len(manifest["declarations"]):
        raise RuntimeConditionsError(f"{name}: duplicate declaration functions")
    if types & declarations:
        raise RuntimeConditionsError(f"{name}: native symbol collision")

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("modelRef")
            if isinstance(ref, dict) and (ref["coordinate"], ref.get("jsonPointer")) not in locations:
                raise RuntimeConditionsError(f"{name}: unresolved modelRef {ref}")
            native = value.get("value")
            if isinstance(native, dict) and "type" in native and native["type"] not in types:
                raise RuntimeConditionsError(f"{name}: unresolved native type {native['type']}")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(manifest)
    root_id = model["rootExtension"]["id"]
    owned = {
        (item["coordinate"], item["kind"])
        for item in model["vocabulary"].get("ownedDeclarations", [])
        if item["owner"] == root_id
    }
    for declaration in manifest["declarations"]:
        if declaration["owner"] != root_id or (
            declaration["modelRef"]["coordinate"], declaration["sourceName"]
        ) not in owned:
            raise RuntimeConditionsError(f"{name}: declaration {declaration['function']} has invalid ownership")


def _verify_imports(discovery: InstalledBindingDiscoveryResult, verified: dict[str, VerifiedBindingPackage]) -> None:
    for imported in discovery.imports:
        source = imported.source_import
        if source.symbol is None:
            if source.module != imported.package.import_package and source.module != f"{imported.package.import_package}.bindings":
                raise RuntimeConditionsError(f"{source.location}: unsupported binding module {source.module}")
            continue
        if source.symbol == "*":
            raise RuntimeConditionsError(f"{source.location}: wildcard binding import cannot be verified statically")
        if source.module not in (imported.package.import_package, f"{imported.package.import_package}.bindings"):
            raise RuntimeConditionsError(f"{source.location}: unsupported binding module {source.module}")
        if source.symbol == "bindings" and source.module == imported.package.import_package:
            continue
        manifest = verified[canonicalize_name(imported.package.distribution)].manifest
        exports = {item["function"] for item in manifest["declarations"]}
        exports.update(item["markerInterface"] for item in manifest["declarations"])
        exports.update(item["nativeName"] for item in manifest["types"])
        if manifest["declarations"]:
            exports.add("Declaration")
        def uses_json_value(value: Any) -> bool:
            if isinstance(value, dict):
                return value.get("builtin") == "JSONValue" or any(uses_json_value(child) for child in value.values())
            return isinstance(value, list) and any(uses_json_value(child) for child in value)
        if uses_json_value(manifest):
            exports.add("JSONValue")
        for contract in manifest["importedMarkerContracts"]:
            exports.add(contract["markerInterface"])
            _, declaration = resolve_marker_declaration(
                contract, verified.values(), {parse_identifier(item) for item in verified[canonicalize_name(imported.package.distribution)].model["extensions"]},
            )
            exports.add(declaration["function"])
        if source.symbol not in exports:
            raise RuntimeConditionsError(f"{source.location}: {source.symbol} is not a declared symbol of {imported.package.distribution}")


def resolve_marker_declaration(
    contract: dict[str, Any], packages: Iterable[VerifiedBindingPackage], allowed_extensions: set[tuple[str, str]],
) -> tuple[VerifiedBindingPackage, dict[str, Any]]:
    """Follow verified package re-exports to the declaration that owns a marker."""
    installed = tuple(packages)
    by_import = {package.installed.import_package: package for package in installed}
    if len(by_import) != len(installed):
        raise RuntimeConditionsError("multiple verified distributions claim one import package")
    provider_name = contract["providerPackage"]
    visited: set[str] = set()
    identity = (contract["owner"], contract["sourceName"], contract["markerInterface"],
                contract["markerMethod"], contract["modelRef"])
    while provider_name not in visited:
        visited.add(provider_name)
        provider = by_import.get(provider_name)
        if provider is None or parse_identifier(provider.model["rootExtension"]) not in allowed_extensions:
            raise RuntimeConditionsError(f"imported marker provider {provider_name} is outside the verified extension closure")
        declarations = [
            item for item in provider.manifest["declarations"]
            if (item["owner"], item["sourceName"], item["markerInterface"],
                item["markerMethod"], item["modelRef"]) == identity
        ]
        if len(declarations) == 1 and provider.model["rootExtension"]["id"] == contract["owner"]:
            return provider, declarations[0]
        if declarations:
            raise RuntimeConditionsError(f"imported marker provider {provider_name} has ambiguous ownership")
        links = [
            item for item in provider.manifest["importedMarkerContracts"]
            if (item["owner"], item["sourceName"], item["markerInterface"],
                item["markerMethod"], item["modelRef"]) == identity
        ]
        if len(links) != 1:
            raise RuntimeConditionsError(f"imported marker provider {provider_name} has no matching declaration or re-export")
        provider_name = links[0]["providerPackage"]
    raise RuntimeConditionsError(f"imported marker provider cycle at {provider_name}")


class InstalledBindingVerifier:
    def verify(self, discovery: InstalledBindingDiscoveryResult) -> VerifiedBindingSet:
        if not discovery.packages:
            raise RuntimeConditionsError("no installed generated binding imports found in workload")
        schemas = _schemas()
        pending = list(discovery.packages)
        verified: dict[str, VerifiedBindingPackage] = {}
        while pending:
            package = pending.pop(0)
            key = canonicalize_name(package.distribution)
            if key in verified:
                previous = verified[key].installed
                if previous.import_package != package.import_package or previous.version != package.version:
                    raise RuntimeConditionsError(f"conflicting installed binding identity for {package.distribution}")
                continue
            try:
                distribution = metadata.distribution(package.distribution)
            except metadata.PackageNotFoundError as exc:
                raise RuntimeConditionsError(f"{package.distribution}: installed metadata disappeared during verification") from exc
            if distribution.version != package.version or distribution.metadata.get("Name") != package.distribution:
                raise RuntimeConditionsError(f"{package.distribution}: installed metadata changed during verification")
            if distribution.files is None or not any(
                str(item).replace("\\", "/") == f"{package.import_package}/bindings.py"
                for item in distribution.files
            ):
                raise RuntimeConditionsError(f"{package.distribution}: generated bindings.py is not recorded")
            _recorded_bytes(distribution, f"{package.import_package}/__init__.py")
            _recorded_bytes(distribution, f"{package.import_package}/bindings.py")
            documents: dict[str, dict[str, Any]] = {}
            for resource in RESOURCE_NAMES:
                path = f"{package.import_package}/{resource}"
                data = _recorded_bytes(distribution, path, package.resources[resource])
                document = _yaml_document(data, f"{package.distribution}/{resource}")
                _validate(document, schemas[SCHEMAS[resource]], f"{package.distribution}/{resource}")
                documents[resource] = document
            manifest = documents["runtimeconditions.bindings.yaml"]
            model = documents["runtimeconditions.binding-model.yaml"]
            extension = documents["runtimeconditions.extension.yaml"]
            release = documents["runtimeconditions.binding-release.yaml"]
            identity = (package.distribution, package.import_package, package.version)
            for name, declared in (("manifest", manifest["package"]), ("release", release["package"])):
                found = (declared["coordinate"], declared["name"], declared["version"])
                if declared["language"] != "python" or canonicalize_name(found[0]) != canonicalize_name(identity[0]) or found[1:] != identity[1:]:
                    raise RuntimeConditionsError(f"{package.distribution}: {name} package identity {found} does not match installed {identity}")
            if manifest["package"]["minimumPythonVersion"] != release["package"]["minimumPythonVersion"]:
                raise RuntimeConditionsError(f"{package.distribution}: minimum Python version differs between manifests")
            required_python = distribution.metadata.get("Requires-Python", "")
            expected_python = f">={manifest['package']['minimumPythonVersion']}"
            try:
                matches_python = SpecifierSet(required_python) == SpecifierSet(expected_python)
            except ValueError as exc:
                raise RuntimeConditionsError(f"{package.distribution}: malformed installed Requires-Python: {exc}") from exc
            if not matches_python:
                raise RuntimeConditionsError(f"{package.distribution}: installed Requires-Python differs from binding manifests")
            minimum = tuple(map(int, manifest["package"]["minimumPythonVersion"].split(".")))
            if sys.version_info[:2] < minimum:
                raise RuntimeConditionsError(f"{package.distribution}: requires Python {minimum[0]}.{minimum[1]}")
            provenance = release["provenance"]
            if provenance["mode"] == "production":
                prefix = f"bindings/{release['package']['packageKey']}/python"
                if provenance["sourceDirectory"] != prefix or provenance["targetReleaseTag"] != f"{prefix}/v{package.version}":
                    raise RuntimeConditionsError(f"{package.distribution}: production provenance does not match package identity")
            model_digest = model["metadata"]["semanticSha256"]
            without_digest = {**model, "metadata": {key: value for key, value in model["metadata"].items() if key != "semanticSha256"}}
            if _canonical_sha256(without_digest) != model_digest:
                raise RuntimeConditionsError(f"{package.distribution}: binding model semantic digest mismatch")
            for name, declared in (("manifest", manifest["model"]), ("release", release["model"])):
                if declared != {"apiVersion": model["apiVersion"], "semanticSha256": model_digest}:
                    raise RuntimeConditionsError(f"{package.distribution}: {name} model identity mismatch")
            semantic_extension = _semantic_value(extension, schemas[SCHEMAS["runtimeconditions.extension.yaml"]], schemas[SCHEMAS["runtimeconditions.extension.yaml"]])
            extension_digest = _canonical_sha256(semantic_extension)
            root = model["rootExtension"]
            actual_extension = {"id": definition_identifier(extension["metadata"]), "version": extension["metadata"]["version"], "semanticSha256": extension_digest}
            if root != actual_extension or manifest["extension"] != root or release["rootExtension"] != root:
                raise RuntimeConditionsError(f"{package.distribution}: root extension identity or digest mismatch")
            _verify_manifest_edges(manifest, model, package.distribution)
            verified[key] = VerifiedBindingPackage(package, manifest, model, extension, release)
            for dependency in release["packageDependencies"]:
                dep_key = canonicalize_name(dependency["coordinate"])
                if dep_key not in verified and all(canonicalize_name(item.distribution) != dep_key for item in pending):
                    pending.append(_load_dependency(dependency["coordinate"], dependency["name"]))

        by_extension: dict[tuple[str, str], VerifiedBindingPackage] = {}
        for package in verified.values():
            extension_id = parse_identifier(package.model["rootExtension"])
            if extension_id in by_extension:
                raise RuntimeConditionsError(f"duplicate installed binding extension {extension_id}")
            by_extension[extension_id] = package
        for package in verified.values():
            _verify_closure(package, verified, by_extension)
        _verify_imports(discovery, verified)
        ordered = tuple(sorted(verified.values(), key=lambda item: item.installed.distribution))
        imported = tuple(verified[canonicalize_name(item.distribution)] for item in discovery.packages)
        return VerifiedBindingSet(ordered, imported)


def _verify_closure(
    package: VerifiedBindingPackage,
    all_packages: dict[str, VerifiedBindingPackage],
    by_extension: dict[tuple[str, str], VerifiedBindingPackage],
) -> None:
    name = package.installed.distribution
    model_entries = package.model["extensions"]
    lock_entries = package.release["dependencyLock"]["extensions"]
    models = {parse_identifier(entry): entry for entry in model_entries}
    locks = {parse_identifier(entry): entry for entry in lock_entries}
    if len(models) != len(model_entries) or len(locks) != len(lock_entries) or set(models) != set(locks):
        raise RuntimeConditionsError(f"{name}: dependency lock does not match model closure")
    root_id = parse_identifier(package.model["rootExtension"])
    if root_id not in models or {
        key: value for key, value in models[root_id].items() if key != "dependencies"
    } != package.model["rootExtension"]:
        raise RuntimeConditionsError(f"{name}: model root is absent from extension closure")
    edges = {(parse_identifier(entry["from"]), parse_identifier(entry["to"])) for entry in package.model.get("dependencyEdges", [])}
    derived_edges = {(parse_identifier(entry), parse_identifier(dep)) for entry in model_entries for dep in entry.get("dependencies", [])}
    if len(edges) != len(package.model.get("dependencyEdges", [])) or edges != derived_edges:
        raise RuntimeConditionsError(f"{name}: model dependency edges do not match extension closure")
    for entry in model_entries:
        dependencies = [parse_identifier(dep) for dep in entry.get("dependencies", [])]
        if len(set(dependencies)) != len(dependencies):
            raise RuntimeConditionsError(f"{name}: duplicate extension dependency for {entry['id']}")
    reachable: set[tuple[str, str]] = set()
    pending = [root_id]
    while pending:
        extension_id = pending.pop()
        if extension_id in reachable:
            continue
        if extension_id not in models:
            raise RuntimeConditionsError(f"{name}: unresolved model dependency {extension_id}")
        reachable.add(extension_id)
        pending.extend(parse_identifier(dep) for dep in models[extension_id].get("dependencies", []))
    if reachable != set(models):
        raise RuntimeConditionsError(f"{name}: model closure has extensions unreachable from its root")
    for extension_id, entry in models.items():
        owner = by_extension.get(extension_id)
        if owner is None:
            raise RuntimeConditionsError(f"{name}: no installed binding supplies extension {extension_id}")
        lock = locks[extension_id]
        expected = owner.model["rootExtension"]
        if entry["semanticSha256"] != expected["semanticSha256"] or entry.get("version") != expected.get("version"):
            raise RuntimeConditionsError(f"{name}: extension closure identity mismatch for {extension_id}")
        if {parse_identifier(dep) for dep in entry.get("dependencies", [])} != {parse_identifier(dep) for dep in owner.extension["spec"].get("dependencies", [])}:
            raise RuntimeConditionsError(f"{name}: extension closure edges differ from packaged definition for {extension_id}")
        if lock["semanticSha256"] != entry["semanticSha256"] or lock.get("version") != entry.get("version") or {parse_identifier(dep) for dep in lock.get("dependencies", [])} != {parse_identifier(dep) for dep in entry.get("dependencies", [])}:
            raise RuntimeConditionsError(f"{name}: dependency lock identity or edges mismatch for {extension_id}")
        if lock["sourceSha256"] != _sha256(owner.installed.resources["runtimeconditions.extension.yaml"]):
            raise RuntimeConditionsError(f"{name}: dependency lock source digest mismatch for {extension_id}")
    dependencies = package.release["packageDependencies"]
    # The release records the tested wheel archive hash. Python installation
    # does not retain that archive; hashed installation verifies it beforehand.
    # At profile time, verify the installed RECORD and resource digests instead.
    if len({canonicalize_name(item["coordinate"]) for item in dependencies}) != len(dependencies):
        raise RuntimeConditionsError(f"{name}: duplicate binding package dependency")
    if len({parse_identifier(item["extension"]) for item in dependencies}) != len(dependencies):
        raise RuntimeConditionsError(f"{name}: duplicate binding extension dependency")
    expected_extensions = {parse_identifier(dep) for dep in models[root_id].get("dependencies", [])}
    if {parse_identifier(item["extension"]) for item in dependencies} != expected_extensions:
        raise RuntimeConditionsError(f"{name}: direct package dependencies do not match root extension edges")
    distribution = metadata.distribution(name)
    try:
        requirements = [Requirement(value) for value in distribution.requires or []]
    except InvalidRequirement as exc:
        raise RuntimeConditionsError(f"{name}: malformed installed dependency metadata: {exc}") from exc
    declared = {canonicalize_name(item.name): item for item in requirements}
    if len(declared) != len(requirements) or set(declared) != {canonicalize_name(item["coordinate"]) for item in dependencies}:
        raise RuntimeConditionsError(f"{name}: installed Requires-Dist differs from binding release dependencies")
    for item in dependencies:
        dep_key = canonicalize_name(item["coordinate"])
        dependency = all_packages.get(dep_key)
        if dependency is None or dependency.installed.import_package != item["name"] or parse_identifier(dependency.model["rootExtension"]) != parse_identifier(item["extension"]):
            raise RuntimeConditionsError(f"{name}: binding dependency {item['coordinate']} identity mismatch")
        tested = item["testedVersion"]
        interval = item["compatibleVersionRange"]
        requirement = declared[dep_key]
        expected_specifiers = {f">={interval['minimumInclusive']}", f"<{interval['nextBreakingExclusive']}"}
        if requirement.marker or requirement.extras or requirement.url or {str(spec) for spec in requirement.specifier} != expected_specifiers or tested != interval["minimumInclusive"]:
            raise RuntimeConditionsError(f"{name}: dependency requirement for {item['coordinate']} differs from release interval")
        try:
            if Version(dependency.installed.version) not in requirement.specifier or Version(tested) not in requirement.specifier:
                raise RuntimeConditionsError(f"{name}: dependency {item['coordinate']} is outside compatible interval")
            tested_version = Version(tested)
            next_breaking = Version(interval["nextBreakingExclusive"])
            expected_breaking = (
                Version(f"0.{tested_version.minor + 1}.0") if tested_version.major == 0
                else Version(f"{tested_version.major + 1}.0.0")
            )
            if next_breaking != expected_breaking:
                raise RuntimeConditionsError(f"{name}: dependency {item['coordinate']} has an invalid next-breaking bound")
        except InvalidVersion as exc:
            raise RuntimeConditionsError(f"{name}: invalid dependency version: {exc}") from exc
    for contract in package.manifest["importedMarkerContracts"]:
        try:
            resolve_marker_declaration(contract, all_packages.values(), set(models))
        except RuntimeConditionsError as exc:
            raise RuntimeConditionsError(f"{name}: {exc}") from exc
