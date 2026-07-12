from __future__ import annotations

from typing import Any

from ..constants import API_VERSION
from ..extension import ExtensionVocabulary
from ..models import Diagnostic, DiscoveryResult
from ..util import add_extension_closure, as_map, string_list


class ProfileValidator:
    def validate(self, profile: dict[str, Any], discovery: DiscoveryResult) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        add = lambda msg: diagnostics.append(Diagnostic("error", "profile-validation", "profile", msg))
        if profile.get("apiVersion") != API_VERSION:
            add(f"apiVersion must be {API_VERSION}")
        if profile.get("kind") != "RuntimeConditionsProfile":
            add("kind must be RuntimeConditionsProfile")
        extensions = string_list(profile.get("extensions"))
        conditions = profile.get("conditions") if isinstance(profile.get("conditions"), list) else []
        if not extensions and conditions:
            add("extensions must declare the extension dependency closure used by conditions")

        definitions = {
            artifact.extension_id: artifact.extension_definition
            for artifact in discovery.validated_artifacts
            if artifact.extension_id and artifact.extension_definition
        }
        declared: set[str] = set()
        for extension in extensions:
            if extension in declared:
                add(f"duplicate extension id {extension}")
            declared.add(extension)
            if extension not in definitions:
                add(f"missing extension definition for {extension}")
        closure: set[str] = set()
        for extension in extensions:
            add_extension_closure(extension, {key: value.dependencies for key, value in definitions.items()}, closure)
        for extension in closure:
            if extension not in declared:
                add(f"extensions missing dependency {extension}")
        vocabulary = ExtensionVocabulary([definitions[item] for item in closure if item in definitions])
        for key, count in vocabulary.counts().items():
            if count > 1:
                add(f"resolved extension set contains vocabulary conflict for {key}")
        for conflict in vocabulary.condition_field_conflicts():
            add(f"resolved extension set contains vocabulary conflict for {conflict}")
        for index, condition in enumerate(conditions):
            if isinstance(condition, dict):
                validate_condition(index, vocabulary, condition, add)
            else:
                add(f"conditions[{index}] must be a mapping")
        return diagnostics


def validate_condition(
    index: int,
    vocabulary: ExtensionVocabulary,
    condition: dict[str, Any],
    add: Any,
) -> None:
    prefix = f"conditions[{index}]"
    kind = str(condition.get("kind", ""))
    interface = as_map(condition.get("interface"))
    interface_type = str(interface.get("type", ""))
    expect_profile_count(vocabulary.kind_count(kind), f"{prefix}.kind {kind}", add)
    expect_profile_count(
        vocabulary.interface_type_count(kind, interface_type),
        f"{prefix}.interface.type {kind}/{interface_type}",
        add,
    )
    if "spec" in interface:
        expect_profile_count(
            vocabulary.interface_field_count(kind, interface_type, "spec"),
            f"{prefix}.interface.spec for {kind}/{interface_type}",
            add,
        )
        spec = as_map(interface.get("spec"))
        fmt = str(spec.get("format", ""))
        expect_profile_count(
            vocabulary.field_value_count("interface.spec.format", kind, interface_type, fmt),
            f"{prefix}.interface.spec.format {fmt} for {kind}/{interface_type}",
            add,
        )
    operations = interface.get("operations")
    if isinstance(operations, list) and operations:
        expect_profile_count(
            vocabulary.interface_field_count(kind, interface_type, "operations"),
            f"{prefix}.interface.operations for {kind}/{interface_type}",
            add,
        )
        for op_index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                continue
            method = str(operation.get("method", ""))
            expect_profile_count(
                vocabulary.field_value_count("interface.operations[].method", kind, interface_type, method),
                f"{prefix}.interface.operations[{op_index}].method {method} for {kind}/{interface_type}",
                add,
            )
    engine = str(interface.get("engine", ""))
    if engine:
        expect_profile_count(
            vocabulary.interface_field_count(kind, interface_type, "engine"),
            f"{prefix}.interface.engine for {kind}/{interface_type}",
            add,
        )
        expect_profile_count(
            vocabulary.field_value_count("interface.engine", kind, interface_type, engine),
            f"{prefix}.interface.engine {engine} for {kind}/{interface_type}",
            add,
        )
    if "configuration" in condition:
        expect_profile_count(
            vocabulary.condition_field_count(kind, interface_type, "configuration"),
            f"{prefix}.configuration for {kind}/{interface_type}",
            add,
        )
        configuration = as_map(condition.get("configuration"))
        for env_index, env in enumerate(configuration.get("env", []) if isinstance(configuration.get("env"), list) else []):
            if isinstance(env, dict):
                prop = str(env.get("property", ""))
                expect_profile_count(
                    vocabulary.field_value_count("configuration.env[].property", kind, interface_type, prop),
                    f"{prefix}.configuration.env[{env_index}].property {prop} for {kind}/{interface_type}",
                    add,
                )
        alternatives = configuration.get("alternatives", [])
        if isinstance(alternatives, list):
            for alt_index, alternative in enumerate(alternatives):
                if not isinstance(alternative, dict):
                    continue
                env_items = alternative.get("env", [])
                if not isinstance(env_items, list):
                    continue
                for env_index, env in enumerate(env_items):
                    if isinstance(env, dict):
                        prop = str(env.get("property", ""))
                        expect_profile_count(
                            vocabulary.field_value_count(
                                "configuration.alternatives[].env[].property",
                                kind,
                                interface_type,
                                prop,
                            ),
                            f"{prefix}.configuration.alternatives[{alt_index}].env[{env_index}].property {prop} for {kind}/{interface_type}",
                            add,
                        )


def expect_profile_count(count: int, message: str, add: Any) -> None:
    if count != 1:
        add(f"{message}: expected exactly one definition, got {count}")

