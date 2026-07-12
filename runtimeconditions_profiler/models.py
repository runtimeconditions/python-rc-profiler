from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    source: str
    message: str


@dataclass(frozen=True)
class SymbolMapping:
    class_name: str
    member_name: str
    target: str = ""
    kind: str = ""
    interface_type: str = ""
    value: str = ""
    method: str = ""
    name_arg: Optional[int] = None
    class_arg: Optional[int] = None
    enum_arg: Optional[int] = None
    string_args: dict[str, int] = field(default_factory=dict)
    applies_to_kinds: list[str] = field(default_factory=list)
    applies_to_interface_types: list[str] = field(default_factory=list)
    options: list["SymbolMapping"] = field(default_factory=list)


@dataclass(frozen=True)
class ManifestModel:
    package: str
    constants: dict[str, str]
    declarations: list[SymbolMapping]
    options: list[SymbolMapping]


@dataclass(frozen=True)
class ExtensionDefinition:
    id: str
    uri: str
    dependencies: list[str]
    kinds: list[str]
    interface_types: list[tuple[str, str]]
    interface_fields: list[tuple[str, str, str]]
    condition_fields: list[tuple[str, tuple[str, ...], tuple[str, ...]]]
    field_values: list[tuple[str, str, str, tuple[str, ...]]]


@dataclass(frozen=True)
class RuntimeConditionsArtifact:
    kind: str
    manifest_uri: str
    extension_uri: Optional[str]
    origin: str
    source_path: Optional[Path]
    root: Path
    language: str = ""


@dataclass
class ValidatedArtifact:
    artifact: RuntimeConditionsArtifact
    manifest_extension_id: Optional[str] = None
    extension_id: Optional[str] = None
    extension_definition_uri: Optional[str] = None
    extension_definition: Optional[ExtensionDefinition] = None
    manifest: Optional[ManifestModel] = None
    dependencies: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def add(self, code: str, source: str, message: str) -> None:
        self.diagnostics.append(Diagnostic("error", code, source, message))


@dataclass(frozen=True)
class DiscoveryOptions:
    package_paths: list[Path] = field(default_factory=list)
    resolve_package_paths: bool = False


@dataclass(frozen=True)
class DiscoveryResult:
    project_root: Path
    project_type: str
    package_paths: list[Path]
    artifacts: list[RuntimeConditionsArtifact]
    validated_artifacts: list[ValidatedArtifact]

    @property
    def diagnostics(self) -> list[Diagnostic]:
        return [diagnostic for artifact in self.validated_artifacts for diagnostic in artifact.diagnostics]

    @property
    def has_errors(self) -> bool:
        return any(diagnostic.severity == "error" for diagnostic in self.diagnostics)

    def to_json(self) -> dict[str, Any]:
        return {
            "project": str(self.project_root),
            "projectType": self.project_type,
            "packagePaths": [str(path) for path in self.package_paths],
            "artifacts": [
                {
                    "kind": artifact.kind,
                    "manifest": artifact.manifest_uri,
                    "extension": artifact.extension_uri,
                    "origin": artifact.origin,
                    "manifestExtensionId": validated.manifest_extension_id,
                    "extensionId": validated.extension_id,
                    "extensionDefinition": validated.extension_definition_uri,
                    "dependencies": validated.dependencies,
                    "pythonPackage": validated.manifest.package if validated.manifest else None,
                    "declarations": len(validated.manifest.declarations) if validated.manifest else 0,
                    "options": len(validated.manifest.options) if validated.manifest else 0,
                    "constants": len(validated.manifest.constants) if validated.manifest else 0,
                }
                for artifact, validated in zip(self.artifacts, self.validated_artifacts)
            ],
            "diagnostics": [
                {
                    "severity": diagnostic.severity,
                    "code": diagnostic.code,
                    "source": diagnostic.source,
                    "message": diagnostic.message,
                }
                for diagnostic in self.diagnostics
            ],
        }


@dataclass(frozen=True)
class ProfileOptions:
    name: str
    workload_uri: str
    workload_version: str
    discovery_options: DiscoveryOptions


@dataclass(frozen=True)
class CallIdentity:
    class_name: str
    member_name: str

