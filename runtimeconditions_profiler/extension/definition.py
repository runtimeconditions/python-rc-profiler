from __future__ import annotations

from typing import Any

from ..models import ExtensionDefinition
from ..util import as_list, as_map, parse_plain_string_list, scalar


def parse_extension_definition(doc: dict[str, Any], extension_id: str, uri: str) -> ExtensionDefinition:
    spec = as_map(doc.get("spec"))
    kinds = [scalar(item.get("name")) or "" for item in as_list(spec.get("kinds")) if isinstance(item, dict)]
    interface_types = [
        (scalar(item.get("targetKind")) or "", scalar(item.get("name")) or "")
        for item in as_list(spec.get("interfaceTypes"))
        if isinstance(item, dict)
    ]
    interface_fields = [
        (scalar(item.get("targetKind")) or "", scalar(item.get("targetType")) or "", scalar(item.get("name")) or "")
        for item in as_list(spec.get("interfaceFields"))
        if isinstance(item, dict)
    ]
    condition_fields = [
        (
            scalar(item.get("name")) or "",
            tuple(parse_plain_string_list(item.get("appliesToKinds"))),
            tuple(parse_plain_string_list(item.get("appliesToInterfaceTypes"))),
        )
        for item in as_list(spec.get("conditionFields"))
        if isinstance(item, dict)
    ]
    field_values = [
        (
            scalar(item.get("field")) or "",
            scalar(item.get("targetKind")) or "",
            scalar(item.get("targetType")) or "",
            tuple(parse_plain_string_list(item.get("values"))),
        )
        for item in as_list(spec.get("fieldValues"))
        if isinstance(item, dict)
    ]
    return ExtensionDefinition(
        id=extension_id,
        uri=uri,
        dependencies=parse_plain_string_list(spec.get("dependencies")),
        kinds=[item for item in kinds if item],
        interface_types=[item for item in interface_types if item[0] and item[1]],
        interface_fields=[item for item in interface_fields if item[0] and item[1] and item[2]],
        condition_fields=[item for item in condition_fields if item[0]],
        field_values=[item for item in field_values if item[0] and item[1] and item[2]],
    )


def resolve_definitions(extension_id: str, definitions_by_id: dict[str, ExtensionDefinition]) -> list[ExtensionDefinition]:
    resolved: list[ExtensionDefinition] = []
    seen: set[str] = set()

    def visit(current: str) -> None:
        if current in seen:
            return
        seen.add(current)
        definition = definitions_by_id.get(current)
        if definition is None:
            return
        for dependency in definition.dependencies:
            visit(dependency)
        resolved.append(definition)

    visit(extension_id)
    return resolved


def dependency_cycle_errors(definitions_by_id: dict[str, ExtensionDefinition]) -> list[str]:
    errors: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(extension_id: str) -> None:
        if extension_id in visited:
            return
        if extension_id in visiting:
            errors.append(f"extension dependency cycle includes {extension_id}")
            return
        visiting.add(extension_id)
        definition = definitions_by_id.get(extension_id)
        if definition is not None:
            for dependency in definition.dependencies:
                visit(dependency)
        visiting.remove(extension_id)
        visited.add(extension_id)

    for extension_id in definitions_by_id:
        visit(extension_id)
    return errors

