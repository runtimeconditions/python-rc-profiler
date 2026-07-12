from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

from ..constants import API_VERSION, BINDING_KIND, EXTENSION_DEFINITION, EXTENSION_KIND, LANGUAGE, PACKAGE_KIND
from ..manifest.parser import ManifestParser, require_scalar
from ..models import ExtensionDefinition, RuntimeConditionsArtifact, ValidatedArtifact
from ..util import as_map, scalar, uri_to_path
from ..yamlio import Yaml
from .definition import dependency_cycle_errors, parse_extension_definition
from .manifest_validator import ManifestVocabularyValidator


class ArtifactValidator:
    def validate(self, artifacts: list[RuntimeConditionsArtifact]) -> list[ValidatedArtifact]:
        validated = [self._validate_one(artifact) for artifact in artifacts]
        self._validate_set(validated)
        return validated

    def _validate_one(self, artifact: RuntimeConditionsArtifact) -> ValidatedArtifact:
        item = ValidatedArtifact(artifact=artifact)
        manifest_doc: dict[str, Any] | None = None
        try:
            manifest_doc = Yaml.load(uri_to_path(artifact.manifest_uri))
        except Exception as exc:
            item.add("package-manifest", artifact.manifest_uri, f"failed to read manifest: {exc}")

        if manifest_doc is not None:
            expected_kind = BINDING_KIND if artifact.kind == "binding" else PACKAGE_KIND
            require_value(item, manifest_doc.get("apiVersion"), API_VERSION, artifact.manifest_uri, "apiVersion")
            require_value(item, manifest_doc.get("kind"), expected_kind, artifact.manifest_uri, "kind")
            metadata = as_map(manifest_doc.get("metadata"))
            require_value(item, metadata.get("language"), LANGUAGE, artifact.manifest_uri, "metadata.language")
            section = as_map(manifest_doc.get(LANGUAGE))
            if section is None:
                item.add("package-language", artifact.manifest_uri, "python section is required")
            else:
                item.manifest = ManifestParser().parse(section, artifact.manifest_uri, item)

            if artifact.kind == "binding":
                item.manifest_extension_id = require_scalar(item, metadata, artifact.manifest_uri, "metadata.extension")
                if section is not None:
                    require_scalar(item, section, artifact.manifest_uri, "python.package")
                override = scalar(metadata.get("extensionDefinition"))
            else:
                extension = as_map(manifest_doc.get("extension"))
                item.manifest_extension_id = require_scalar(item, extension, artifact.manifest_uri, "extension.id")
                override = scalar(extension.get("definition"))
            validate_extension_id(item, item.manifest_extension_id, artifact.manifest_uri)
            item.extension_definition_uri = resolve_extension_definition(artifact, override, item)

        if item.extension_definition_uri is not None:
            try:
                extension_doc = Yaml.load(uri_to_path(item.extension_definition_uri))
                require_value(item, extension_doc.get("apiVersion"), API_VERSION, item.extension_definition_uri, "apiVersion")
                require_value(item, extension_doc.get("kind"), EXTENSION_KIND, item.extension_definition_uri, "kind")
                metadata = as_map(extension_doc.get("metadata"))
                item.extension_id = require_scalar(item, metadata, item.extension_definition_uri, "metadata.id")
                validate_extension_id(item, item.extension_id, item.extension_definition_uri)
                if item.extension_id:
                    item.extension_definition = parse_extension_definition(
                        extension_doc,
                        item.extension_id,
                        item.extension_definition_uri,
                    )
                    item.dependencies = item.extension_definition.dependencies
            except Exception as exc:
                item.add("extension-definition", item.extension_definition_uri, f"failed to read extension definition: {exc}")

        if item.manifest_extension_id and item.extension_id and item.manifest_extension_id != item.extension_id:
            item.add(
                "extension-definition",
                artifact.manifest_uri,
                f"manifest extension id {item.manifest_extension_id} does not match extension definition {item.extension_id}",
            )
        return item

    def _validate_set(self, artifacts: list[ValidatedArtifact]) -> None:
        definitions_by_id: dict[str, str] = {}
        models_by_id: dict[str, ExtensionDefinition] = {}
        for artifact in artifacts:
            if not artifact.extension_id:
                continue
            previous = definitions_by_id.setdefault(artifact.extension_id, artifact.extension_definition_uri or "")
            if previous != (artifact.extension_definition_uri or ""):
                artifact.add(
                    "extension-definition",
                    artifact.extension_definition_uri or artifact.artifact.manifest_uri,
                    f"duplicate extension id {artifact.extension_id} already defined by {previous}",
                )
            if artifact.extension_definition is not None:
                models_by_id.setdefault(artifact.extension_id, artifact.extension_definition)

        for artifact in artifacts:
            for dependency in artifact.dependencies:
                if dependency not in definitions_by_id:
                    artifact.add(
                        "extension-dependency",
                        artifact.extension_definition_uri or artifact.artifact.manifest_uri,
                        f"dependency {dependency} cannot be resolved from discovered Runtime Conditions artifacts",
                    )

        cycle_errors = dependency_cycle_errors(models_by_id)
        for artifact in artifacts:
            for message in cycle_errors:
                artifact.add("extension-dependency", artifact.extension_definition_uri or artifact.artifact.manifest_uri, message)

        for artifact in artifacts:
            ManifestVocabularyValidator().validate(artifact, models_by_id)


def resolve_extension_definition(
    artifact: RuntimeConditionsArtifact,
    override: Optional[str],
    validated: ValidatedArtifact,
) -> Optional[str]:
    if override:
        path = (uri_to_path(artifact.manifest_uri).parent / override).resolve()
        return path.as_uri()
    if artifact.extension_uri is None:
        validated.add(
            "extension-definition",
            artifact.manifest_uri,
            f"{EXTENSION_DEFINITION} is required next to the manifest",
        )
        return None
    return artifact.extension_uri


def validate_extension_id(artifact: ValidatedArtifact, value: Optional[str], source: str) -> None:
    if not value:
        return
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        artifact.add("extension-definition", source, "extension id must be an absolute HTTP or HTTPS URI")


def require_value(artifact: ValidatedArtifact, actual: Any, expected: str, source: str, field_name: str) -> None:
    parsed = scalar(actual)
    if not parsed:
        artifact.add("package-manifest", source, f"{field_name} is required")
    elif parsed != expected:
        artifact.add("package-manifest", source, f"{field_name} must be {expected}")

