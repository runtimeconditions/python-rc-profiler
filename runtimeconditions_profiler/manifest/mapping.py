from __future__ import annotations

from typing import Optional

from ..models import ManifestModel, SymbolMapping


def all_mappings(manifest: ManifestModel) -> list[SymbolMapping]:
    result: list[SymbolMapping] = []

    def walk(mapping: SymbolMapping) -> None:
        result.append(mapping)
        for option in mapping.options:
            walk(option)

    for mapping in manifest.declarations:
        walk(mapping)
    for mapping in manifest.options:
        walk(mapping)
    return result


def find_option(options: list[SymbolMapping], class_name: str, member_name: str) -> Optional[SymbolMapping]:
    for option in options:
        if class_matches(class_name, option.class_name) and option.member_name == member_name:
            return option
    return None


def class_matches(actual: str, expected: str) -> bool:
    return actual == expected or simple_name(actual) == expected


def simple_name(value: str) -> str:
    return value.rsplit(".", 1)[-1]


def strip_package_class(value: str, package: str) -> str:
    prefix = f"{package}."
    return value[len(prefix) :] if value.startswith(prefix) else value

