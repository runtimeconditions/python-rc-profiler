from __future__ import annotations

import ast
from typing import Optional

from ..models import CallIdentity
from ..source.python import expression_name
from .binding import BindingArtifact


class ImportIndex:
    def __init__(self, bindings: list[BindingArtifact]) -> None:
        self.bindings = bindings
        self.package_aliases: dict[str, str] = {}
        self.class_aliases: dict[str, str] = {}
        self.wildcard_packages: set[str] = set()

    @staticmethod
    def from_module(tree: ast.Module, bindings: list[BindingArtifact]) -> "ImportIndex":
        index = ImportIndex(bindings)
        packages = {binding.manifest.package for binding in bindings}
        classes_by_package = {
            binding.manifest.package: {mapping.class_name for mapping in binding.all_mappings()}
            for binding in bindings
        }
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in packages:
                        index.package_aliases[alias.asname or alias.name] = alias.name
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module not in packages:
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        index.wildcard_packages.add(node.module)
                        for class_name in classes_by_package[node.module]:
                            index.class_aliases[class_name] = f"{node.module}.{class_name}"
                        continue
                    if alias.name in classes_by_package[node.module]:
                        index.class_aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        return index

    def call_identity(self, expr: ast.expr) -> Optional[CallIdentity]:
        name = expression_name(expr)
        if not name or "." not in name:
            return None
        normalized = self.normalize_name(name)
        parts = normalized.split(".")
        if len(parts) < 2:
            return None
        member = parts[-1]
        class_name = ".".join(parts[:-1])
        return CallIdentity(class_name=class_name, member_name=member)

    def normalize_name(self, name: str) -> str:
        parts = name.split(".")
        if not parts:
            return name
        first = parts[0]
        if first in self.class_aliases:
            return ".".join([self.class_aliases[first], *parts[1:]])
        if first in self.package_aliases:
            return ".".join([self.package_aliases[first], *parts[1:]])
        return name

