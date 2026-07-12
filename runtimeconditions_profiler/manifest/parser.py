from __future__ import annotations

from typing import Any, Optional

from ..models import ManifestModel, SymbolMapping, ValidatedArtifact
from ..util import scalar


class ManifestParser:
    def parse(self, section: dict[str, Any], source: str, artifact: ValidatedArtifact) -> ManifestModel:
        package = scalar(section.get("package")) or ""
        if not package:
            artifact.add("package-manifest", source, "python.package is required")
        constants = parse_constants(section.get("constants"), source, artifact)
        declarations = parse_mappings(section.get("declarations"), "python.declarations", True, source, artifact)
        options = parse_mappings(section.get("options"), "python.options", False, source, artifact)
        if not declarations and not options:
            artifact.add(
                "package-manifest",
                source,
                "at least one python.declarations or python.options entry is required",
            )
        return ManifestModel(package=package, constants=constants, declarations=declarations, options=options)


def parse_mappings(
    value: Any,
    path: str,
    declaration: bool,
    source: str,
    artifact: ValidatedArtifact,
) -> list[SymbolMapping]:
    if value is None:
        return []
    if not isinstance(value, list):
        artifact.add("package-manifest", source, f"{path} must be a sequence")
        return []
    mappings: list[SymbolMapping] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            artifact.add("package-manifest", source, f"{item_path} must be a mapping")
            continue
        class_name = require_scalar(artifact, item, source, f"{item_path}.class") or ""
        member = scalar(item.get("function")) or scalar(item.get("method")) or ""
        if not member:
            artifact.add("package-manifest", source, f"{item_path} must define function or method")
        target = scalar(item.get("target")) or ""
        kind = scalar(item.get("kind")) or ""
        if declaration and not kind:
            artifact.add("package-manifest", source, f"{item_path}.kind is required")
        if not declaration and not target:
            artifact.add("package-manifest", source, f"{item_path}.target is required")
        mappings.append(
            SymbolMapping(
                class_name=class_name,
                member_name=member,
                target=target,
                kind=kind,
                interface_type=scalar(item.get("interfaceType")) or "",
                value=scalar(item.get("value")) or "",
                method=scalar(item.get("method")) or "",
                name_arg=parse_int(item.get("nameArg"), f"{item_path}.nameArg", source, artifact),
                class_arg=parse_int(item.get("classArg"), f"{item_path}.classArg", source, artifact),
                enum_arg=parse_int(item.get("enumArg"), f"{item_path}.enumArg", source, artifact),
                string_args=parse_int_map(item.get("stringArgs"), f"{item_path}.stringArgs", source, artifact),
                applies_to_kinds=parse_string_list(item.get("appliesToKinds"), f"{item_path}.appliesToKinds", source, artifact),
                applies_to_interface_types=parse_string_list(
                    item.get("appliesToInterfaceTypes"),
                    f"{item_path}.appliesToInterfaceTypes",
                    source,
                    artifact,
                ),
                options=parse_mappings(item.get("options"), f"{item_path}.options", False, source, artifact),
            )
        )
    return mappings


def parse_constants(value: Any, source: str, artifact: ValidatedArtifact) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        artifact.add("package-manifest", source, "python.constants must be a mapping")
        return {}
    constants: dict[str, str] = {}
    for key, item in value.items():
        parsed = scalar(item)
        if parsed is None:
            artifact.add("package-manifest", source, f"python.constants.{key} must be scalar")
        else:
            constants[str(key)] = parsed
    return constants


def require_scalar(
    artifact: ValidatedArtifact,
    input_map: dict[str, Any],
    source: str,
    path: str,
) -> Optional[str]:
    key = path.split(".")[-1]
    value = scalar(input_map.get(key))
    if not value:
        artifact.add("package-manifest", source, f"{path} is required")
        return None
    return value


def parse_int(value: Any, path: str, source: str, artifact: ValidatedArtifact) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int) and value >= 0:
        return value
    artifact.add("package-manifest", source, f"{path} must be an integer zero or greater")
    return None


def parse_int_map(value: Any, path: str, source: str, artifact: ValidatedArtifact) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        artifact.add("package-manifest", source, f"{path} must be a mapping")
        return {}
    result: dict[str, int] = {}
    for key, item in value.items():
        parsed = parse_int(item, f"{path}.{key}", source, artifact)
        if parsed is not None:
            result[str(key)] = parsed
    return result


def parse_string_list(value: Any, path: str, source: str, artifact: ValidatedArtifact) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        artifact.add("package-manifest", source, f"{path} must be a sequence")
        return []
    result: list[str] = []
    for index, item in enumerate(value):
        parsed = scalar(item)
        if parsed is None:
            artifact.add("package-manifest", source, f"{path}[{index}] must be scalar")
        else:
            result.append(parsed)
    return result

