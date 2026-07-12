from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..models import SymbolMapping, ValidatedArtifact
from ..util import ignored_path


@dataclass
class PythonClass:
    name: str
    functions: dict[str, list[str]]
    annotations: dict[str, dict[str, str]]


class PythonSourceIndex:
    def __init__(self) -> None:
        self.classes: dict[str, PythonClass] = {}
        self.constants: dict[str, str] = {}
        self.schemas: dict[str, dict[str, Any]] = {}
        self.string_constants: dict[str, str] = {}

    @staticmethod
    def from_paths(paths: list[Path]) -> "PythonSourceIndex":
        index = PythonSourceIndex()
        for path in paths:
            module = module_name_from_path(path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            index._visit_module(tree, module)
        return index

    def validate_mapping(self, artifact: ValidatedArtifact, mapping: SymbolMapping) -> None:
        class_info = self.classes.get(mapping.class_name)
        if class_info is None:
            artifact.add(
                "package-source",
                artifact.artifact.manifest_uri,
                f"{mapping.class_name}.{mapping.member_name} is not declared in Python package",
            )
            return
        params = class_info.functions.get(mapping.member_name)
        if params is None:
            artifact.add(
                "package-source",
                artifact.artifact.manifest_uri,
                f"{mapping.class_name}.{mapping.member_name} is not declared in Python package",
            )
            return
        annotations = class_info.annotations.get(mapping.member_name, {})
        for label, index in arg_indexes(mapping):
            if index >= len(params):
                artifact.add(
                    "package-source",
                    artifact.artifact.manifest_uri,
                    f"{mapping.class_name}.{mapping.member_name} {label} index {index} is out of range",
                )
                continue
            name = params[index]
            annotation = annotations.get(name, "")
            if label in ("nameArg", "stringArg") and annotation and annotation not in ("str", "typing.Optional[str]", "Optional[str]"):
                artifact.add(
                    "package-source",
                    artifact.artifact.manifest_uri,
                    f"{mapping.class_name}.{mapping.member_name} {label} index {index} must point to a string parameter",
                )
            if label == "classArg" and annotation and annotation not in ("type", "object", "Any", "typing.Any"):
                artifact.add(
                    "package-source",
                    artifact.artifact.manifest_uri,
                    f"{mapping.class_name}.{mapping.member_name} classArg index {index} must point to a class/type parameter",
                )

    def _visit_module(self, tree: ast.Module, module: str) -> None:
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                self._collect_assignment(node, "")
            if isinstance(node, ast.ClassDef):
                self._collect_class(node, module)

    def _collect_class(self, node: ast.ClassDef, module: str) -> None:
        functions: dict[str, list[str]] = {}
        annotations: dict[str, dict[str, str]] = {}
        fields: dict[str, Any] = {}
        for child in node.body:
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                self._collect_assignment(child, node.name)
                field_name, field_type = class_field_annotation(child)
                if field_name and field_type:
                    fields[field_name] = schema_for_annotation(field_type, self.schemas)
            if isinstance(child, ast.FunctionDef):
                params = [arg.arg for arg in child.args.args]
                if params and params[0] in ("self", "cls"):
                    params = params[1:]
                functions[child.name] = params
                annotations[child.name] = {
                    arg.arg: annotation_name(arg.annotation)
                    for arg in child.args.args
                    if arg.annotation is not None
                }
            if isinstance(child, ast.ClassDef):
                self._collect_nested_class(child, node.name)
        self.classes[node.name] = PythonClass(node.name, functions, annotations)
        if fields:
            self.schemas[node.name] = fields
            if module:
                self.schemas[f"{module}.{node.name}"] = fields

    def _collect_nested_class(self, node: ast.ClassDef, parent: str) -> None:
        prefix = f"{parent}.{node.name}"
        for child in node.body:
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                self._collect_assignment(child, prefix)

    def _collect_assignment(self, node: ast.Assign | ast.AnnAssign, prefix: str) -> None:
        value_node = node.value
        if value_node is None:
            return
        value = literal_string(value_node)
        if value is None:
            return
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            targets = [node.target]
        for target in targets:
            name = target_name(target)
            if name:
                key = f"{prefix}.{name}" if prefix else name
                self.constants[key] = value
                self.string_constants[key] = value
                self.string_constants[name] = value


def resolve_package_source_root(artifact_root: Path, package: str) -> Optional[Path]:
    package_path = Path(*package.split("."))
    candidates = [
        artifact_root / package_path,
        artifact_root / "src" / package_path,
        artifact_root / "src" / "main" / "python" / package_path,
    ]
    for candidate in candidates:
        if (candidate / "__init__.py").is_file():
            return candidate
        if candidate.with_suffix(".py").is_file():
            return candidate.with_suffix(".py")
    return None


def python_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return [path for path in sorted(root.rglob("*.py")) if not ignored_path(path)]


def workload_source_files(root: Path) -> list[Path]:
    source_roots = [root / "src", root]
    files: list[Path] = []
    seen: set[Path] = set()
    for source_root in source_roots:
        if not source_root.is_dir():
            continue
        for path in sorted(source_root.rglob("*.py")):
            if ignored_path(path):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(resolved)
    return files


def literal_string(expr: ast.expr) -> Optional[str]:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    return None


def target_name(expr: ast.expr) -> Optional[str]:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = target_name(expr.value)
        return f"{base}.{expr.attr}" if base else expr.attr
    return None


def expression_name(expr: ast.expr) -> Optional[str]:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = expression_name(expr.value)
        return f"{base}.{expr.attr}" if base else expr.attr
    return None


def annotation_name(annotation: ast.expr | None) -> str:
    if annotation is None:
        return ""
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return expression_name(annotation) or ""
    if isinstance(annotation, ast.Subscript):
        return annotation_name(annotation.value)
    if isinstance(annotation, ast.Constant):
        return str(annotation.value)
    return ""


def class_field_annotation(node: ast.Assign | ast.AnnAssign) -> tuple[str, str]:
    if isinstance(node, ast.AnnAssign):
        name = target_name(node.target)
        return name or "", annotation_name(node.annotation)
    return "", ""


def schema_for_annotation(name: str, schemas: dict[str, dict[str, Any]]) -> Any:
    return {
        "str": "string",
        "int": "integer",
        "bool": "boolean",
        "float": "number",
    }.get(name, schemas.get(name, {}))


def module_name_from_path(path: Path) -> str:
    if path.name == "__init__.py":
        return path.parent.name
    return path.stem


def arg_indexes(mapping: SymbolMapping) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    if mapping.name_arg is not None:
        result.append(("nameArg", mapping.name_arg))
    if mapping.class_arg is not None:
        result.append(("classArg", mapping.class_arg))
    if mapping.enum_arg is not None:
        result.append(("enumArg", mapping.enum_arg))
    for _, index in mapping.string_args.items():
        result.append(("stringArg", index))
    return result

