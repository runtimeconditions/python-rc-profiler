from __future__ import annotations

from ..models import ExtensionDefinition
from ..util import condition_scopes_overlap, increment


class ExtensionVocabulary:
    def __init__(self, definitions: list[ExtensionDefinition]) -> None:
        self.definitions = definitions

    def kind_count(self, name: str) -> int:
        return sum(1 for definition in self.definitions for item in definition.kinds if item == name)

    def interface_type_count(self, kind: str, name: str) -> int:
        return sum(
            1
            for definition in self.definitions
            for item_kind, item_name in definition.interface_types
            if item_kind == kind and item_name == name
        )

    def interface_field_count(self, kind: str, interface_type: str, name: str) -> int:
        return sum(
            1
            for definition in self.definitions
            for item_kind, item_type, item_name in definition.interface_fields
            if item_kind == kind and item_type == interface_type and item_name == name
        )

    def condition_field_count(self, kind: str, interface_type: str, name: str) -> int:
        return sum(
            1
            for definition in self.definitions
            for item_name, kinds, interface_types in definition.condition_fields
            if item_name == name and kind in kinds and (not interface_types or interface_type in interface_types)
        )

    def field_value_count(self, field_name: str, kind: str, interface_type: str, value: str) -> int:
        return sum(
            1
            for definition in self.definitions
            for item_field, item_kind, item_type, values in definition.field_values
            if item_field == field_name and item_kind == kind and item_type == interface_type and value in values
        )

    def field_value_definition_count(self, field_name: str, kind: str, interface_type: str) -> int:
        return sum(
            1
            for definition in self.definitions
            for item_field, item_kind, item_type, _ in definition.field_values
            if item_field == field_name and item_kind == kind and item_type == interface_type
        )

    def field_value_value_count(self, value: str) -> int:
        return sum(
            1
            for definition in self.definitions
            for _, _, _, values in definition.field_values
            if value in values
        )

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for definition in self.definitions:
            for kind in definition.kinds:
                increment(counts, f"kind:{kind}")
            for kind, name in definition.interface_types:
                increment(counts, f"interfaceType:{kind}:{name}")
            for kind, interface_type, name in definition.interface_fields:
                increment(counts, f"interfaceField:{kind}:{interface_type}:{name}")
            for field_name, kind, interface_type, _ in definition.field_values:
                increment(counts, f"fieldValues:{kind}:{interface_type}:{field_name}")
        return counts

    def condition_field_conflicts(self) -> list[str]:
        fields: list[tuple[ExtensionDefinition, str, tuple[str, ...], tuple[str, ...]]] = []
        for definition in self.definitions:
            for name, kinds, interface_types in definition.condition_fields:
                fields.append((definition, name, kinds, interface_types))
        conflicts: list[str] = []
        for index, left in enumerate(fields):
            for right in fields[index + 1 :]:
                if left[1] == right[1] and condition_scopes_overlap(left[2], left[3], right[2], right[3]):
                    conflicts.append(f"conditionField:{left[1]} between {left[0].id} and {right[0].id}")
        return conflicts

