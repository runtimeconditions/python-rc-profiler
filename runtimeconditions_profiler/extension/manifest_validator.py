from __future__ import annotations

from ..manifest.mapping import all_mappings
from ..models import ExtensionDefinition, SymbolMapping, ValidatedArtifact
from ..source.inspector import SourceInspector
from .definition import resolve_definitions
from .vocabulary import ExtensionVocabulary


class ManifestVocabularyValidator:
    def validate(self, artifact: ValidatedArtifact, definitions_by_id: dict[str, ExtensionDefinition]) -> None:
        if artifact.manifest is None or artifact.extension_id is None:
            return
        vocabulary = ExtensionVocabulary(resolve_definitions(artifact.extension_id, definitions_by_id))
        for key, count in vocabulary.counts().items():
            if count > 1:
                artifact.add(
                    "extension-vocabulary",
                    artifact.extension_definition_uri or artifact.artifact.manifest_uri,
                    f"resolved extension set contains vocabulary conflict for {key}",
                )
        for conflict in vocabulary.condition_field_conflicts():
            artifact.add(
                "extension-vocabulary",
                artifact.extension_definition_uri or artifact.artifact.manifest_uri,
                f"resolved extension set contains vocabulary conflict for {conflict}",
            )
        for constant, value in artifact.manifest.constants.items():
            if vocabulary.field_value_value_count(value) == 0:
                artifact.add(
                    "package-manifest",
                    artifact.artifact.manifest_uri,
                    f'constant {constant} value "{value}" is not defined by resolved field values',
                )
        for declaration in artifact.manifest.declarations:
            self._validate_declaration(artifact, vocabulary, declaration)
        for option in artifact.manifest.options:
            self._validate_option(artifact, vocabulary, option, scopes_from_option(option, vocabulary))
        SourceInspector().validate(artifact)

    def _validate_declaration(
        self,
        artifact: ValidatedArtifact,
        vocabulary: ExtensionVocabulary,
        declaration: SymbolMapping,
    ) -> None:
        expect_exactly_one(
            artifact,
            vocabulary.kind_count(declaration.kind),
            f"declaration kind {declaration.kind}",
        )
        if declaration.interface_type:
            expect_exactly_one(
                artifact,
                vocabulary.interface_type_count(declaration.kind, declaration.interface_type),
                f"declaration interfaceType {declaration.kind}/{declaration.interface_type}",
            )
        scopes = [(declaration.kind, declaration.interface_type)]
        for option in declaration.options:
            self._validate_option(artifact, vocabulary, option, scopes)

    def _validate_option(
        self,
        artifact: ValidatedArtifact,
        vocabulary: ExtensionVocabulary,
        option: SymbolMapping,
        scopes: list[tuple[str, str]],
    ) -> None:
        nested_scopes = scopes
        if option.target == "interface.spec":
            for kind, interface_type in scopes:
                expect_exactly_one(
                    artifact,
                    vocabulary.interface_field_count(kind, interface_type, "spec"),
                    f"binding option interface.spec for {kind}/{interface_type}",
                )
        elif option.target == "interface.operations[]":
            for kind, interface_type in scopes:
                expect_exactly_one(
                    artifact,
                    vocabulary.interface_field_count(kind, interface_type, "operations"),
                    f"binding option interface.operations[] for {kind}/{interface_type}",
                )
                expect_exactly_one(
                    artifact,
                    vocabulary.field_value_count(
                        "interface.operations[].method",
                        kind,
                        interface_type,
                        option.method,
                    ),
                    f"binding option method {option.method} for {kind}/{interface_type}",
                )
        elif option.target == "interface.type":
            updated: list[tuple[str, str]] = []
            for kind, _ in scopes:
                expect_exactly_one(
                    artifact,
                    vocabulary.interface_type_count(kind, option.value),
                    f"binding option interface.type {kind}/{option.value}",
                )
                if option.enum_arg is not None:
                    expect_exactly_one(
                        artifact,
                        vocabulary.interface_field_count(kind, option.value, "engine"),
                        f"binding option engine for {kind}/{option.value}",
                    )
                updated.append((kind, option.value))
            nested_scopes = updated
        elif option.target == "configuration.env[]":
            validate_configuration_option(artifact, vocabulary, scopes, "configuration.env[].property")
        elif option.target == "configuration.alternatives[]":
            validate_configuration_option(
                artifact,
                vocabulary,
                scopes,
                "configuration.alternatives[].env[].property",
            )
        elif option.target in ("requestBodySchema", "responseSchema", "env.sensitive", "env.required"):
            pass
        elif not option.target:
            artifact.add("package-manifest", artifact.artifact.manifest_uri, f"binding option {option.member_name} is missing target")
        else:
            artifact.add(
                "package-manifest",
                artifact.artifact.manifest_uri,
                f"unsupported binding option target {option.target}",
            )
        for nested in option.options:
            self._validate_option(artifact, vocabulary, nested, nested_scopes)


def validate_configuration_option(
    artifact: ValidatedArtifact,
    vocabulary: ExtensionVocabulary,
    scopes: list[tuple[str, str]],
    property_field: str,
) -> None:
    if not scopes:
        artifact.add(
            "package-manifest",
            artifact.artifact.manifest_uri,
            "configuration binding option requires appliesToKinds/appliesToInterfaceTypes or a declaration scope",
        )
        return
    for kind, interface_type in scopes:
        expect_exactly_one(
            artifact,
            vocabulary.condition_field_count(kind, interface_type, "configuration"),
            f"binding option configuration for {kind}/{interface_type}",
        )
        expect_exactly_one(
            artifact,
            vocabulary.field_value_definition_count(property_field, kind, interface_type),
            f"binding option property field {property_field} for {kind}/{interface_type}",
        )


def scopes_from_option(option: SymbolMapping, vocabulary: ExtensionVocabulary) -> list[tuple[str, str]]:
    if not option.applies_to_kinds:
        return []
    if not option.applies_to_interface_types:
        return [(kind, "") for kind in option.applies_to_kinds]
    scopes: list[tuple[str, str]] = []
    for kind in option.applies_to_kinds:
        for interface_type in option.applies_to_interface_types:
            if vocabulary.interface_type_count(kind, interface_type) == 1:
                scopes.append((kind, interface_type))
    return scopes


def expect_exactly_one(artifact: ValidatedArtifact, count: int, message: str) -> None:
    if count != 1:
        artifact.add(
            "package-manifest",
            artifact.artifact.manifest_uri,
            f"{message}: expected exactly one definition, got {count}",
        )

