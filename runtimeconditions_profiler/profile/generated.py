"""Coordinate-driven extraction from verified, installed Python bindings."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import RuntimeConditionsError
from ..extension.identity import parse_identifier
from ..project.verify import VerifiedBindingPackage, VerifiedBindingSet, resolve_marker_declaration
from .static_values import NativeSymbol, SourceCall, StaticCall, StaticProject


@dataclass(frozen=True)
class ExtractedCondition:
    source: Path
    line: int
    column: int
    condition: dict[str, Any]
    direct_extensions: tuple[tuple[str, str], ...]
    structural_fallbacks: tuple[str, ...] = ()


class StructuralError(ValueError):
    def __init__(self, coordinate: str, message: str) -> None:
        self.coordinate = coordinate
        super().__init__(message)


def _coordinate(item: dict[str, Any]) -> str:
    ref = item.get("modelRef", item)
    return ref["coordinate"] + ref.get("jsonPointer", "")


def _native(symbol: Any) -> tuple[str, str] | None:
    if not isinstance(symbol, NativeSymbol):
        return None
    path = symbol.path[1:] if symbol.path[:1] == ("bindings",) else symbol.path
    if len(path) != 1:
        return None
    return symbol.package, path[0]


def _json_value(value: Any, coordinate: str) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json_value(item, coordinate) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item, coordinate) for key, item in value.items()}
    raise StructuralError(coordinate, "value is not static JSON data")


class GeneratedBindingExtractor:
    def __init__(self, verified: VerifiedBindingSet) -> None:
        self.verified = verified
        self.packages = {item.installed.import_package: item for item in verified.packages}
        self.declarations: dict[tuple[str, str], tuple[VerifiedBindingPackage, dict[str, Any]]] = {}
        self.types: dict[str, dict[str, dict[str, Any]]] = {}
        self.roots: list[tuple[VerifiedBindingPackage, dict[str, Any]]] = []
        self._structural_fallbacks: set[str] = set()
        for package in verified.packages:
            root = package.installed.import_package
            self.types[root] = {item["nativeName"]: item for item in package.manifest["types"]}
            for declaration in package.manifest["declarations"]:
                key = (root, declaration["function"])
                if key in self.declarations:
                    raise RuntimeConditionsError(f"duplicate declaration symbol {root}.{key[1]}")
                self.declarations[key] = (package, declaration)
            for contract in package.manifest.get("importedMarkerContracts", []):
                provider, declaration = resolve_marker_declaration(
                    contract, verified.packages,
                    {parse_identifier(item) for item in package.model["extensions"]},
                )
                key = (root, declaration["function"])
                if key in self.declarations:
                    raise RuntimeConditionsError(f"duplicate declaration symbol {root}.{key[1]}")
                self.declarations[key] = (provider, declaration)
            self.roots.extend((package, item) for item in package.manifest["rootBindings"])

    def extract(self, project_root: Path) -> tuple[ExtractedCondition, ...]:
        project = StaticProject(project_root, set(self.packages))
        result: list[ExtractedCondition] = []
        for site in project.calls:
            candidate = project.maybe_native(site.node.func, site.environment)
            key = _native(candidate)
            if key not in self.declarations:
                continue
            package, declaration = self.declarations[key]
            coordinate = _coordinate(declaration)
            symbol = f"{key[0]}.{key[1]}"
            try:
                if site.dynamic:
                    raise StructuralError(coordinate, "declaration appears under dynamic control flow")
                value = project.evaluate(site.node, site.environment)
                if not isinstance(value, StaticCall) or value.callee != candidate:
                    raise StructuralError(coordinate, "declaration symbol is ambiguous or rebound")
                self._structural_fallbacks = set()
                result.append(self._condition(site, package, declaration, value))
            except (ValueError, RecursionError) as exc:
                detail = f"{exc.coordinate}: {exc}" if isinstance(exc, StructuralError) else f"{coordinate}: {exc}"
                raise RuntimeConditionsError(f"{site.location}: {symbol}: {detail}") from exc
        return tuple(result)

    def _condition(
        self, site: SourceCall, package: VerifiedBindingPackage,
        declaration: dict[str, Any], call: StaticCall,
    ) -> ExtractedCondition:
        coordinate = _coordinate(declaration)
        if call.kwargs:
            raise StructuralError(coordinate, "declaration accepts positional field objects only")
        condition: dict[str, Any] = {"kind": declaration["sourceName"]}
        contributing = {parse_identifier(package.model["rootExtension"])}
        selected_interface: str | None = None
        for argument in call.args:
            if not isinstance(argument, StaticCall):
                raise StructuralError(coordinate, "declaration argument is not a generated field object")
            native = _native(argument.callee)
            if native is None or native[0] not in self.types:
                raise StructuralError(coordinate, "declaration argument has no verified binding type")
            field_type = self.types[native[0]].get(native[1])
            if field_type is None:
                raise StructuralError(coordinate, f"unresolved generated field type {native[0]}.{native[1]}")
            edges = [
                (owner, edge)
                for owner, edge in self.roots
                if owner.installed.import_package == native[0]
                and edge["declarationCoordinate"] == declaration["modelRef"]["coordinate"]
                and edge["value"] == {"type": native[1]}
                and edge["scope"]["kind"] == declaration["sourceName"]
            ]
            if not edges:
                raise StructuralError(_coordinate(field_type), "field has no root binding for this declaration")
            if len(edges) != 1:
                raise StructuralError(_coordinate(field_type), "field has ambiguous root bindings")
            owner, edge = edges[0]
            implements = field_type.get("implements", [])
            if not any(item["declarationCoordinate"] == edge["declarationCoordinate"] for item in implements):
                raise StructuralError(_coordinate(field_type), "field does not implement the declaration marker")
            interface = edge["scope"].get("interfaceType")
            if selected_interface is not None and interface is not None and interface != selected_interface:
                raise StructuralError(_coordinate(edge), "fields have conflicting interface scopes")
            selected_interface = interface or selected_interface
            value = self._serialize_type(argument, owner, field_type, _coordinate(edge), set())
            if edge["role"] == "interface":
                if not isinstance(value, dict):
                    raise StructuralError(_coordinate(edge), "interface value must be an object")
                if "type" in value and value["type"] != edge["fixedInterfaceType"]:
                    raise StructuralError(_coordinate(edge), "interface type conflicts with fixed scope")
                value = {"type": edge["fixedInterfaceType"], **value}
            self._put_path(condition, edge["path"], value, _coordinate(edge))
            contributing.add(parse_identifier(owner.model["rootExtension"]))
        if selected_interface is not None:
            current = condition.get("interface")
            if current is None:
                condition["interface"] = {"type": selected_interface}
            elif not isinstance(current, dict) or current.get("type") != selected_interface:
                raise StructuralError(coordinate, "interface conflicts with field scope")
        return ExtractedCondition(
            site.source, site.node.lineno, site.node.col_offset + 1,
            condition, tuple(sorted(contributing)), tuple(sorted(self._structural_fallbacks)),
        )

    @staticmethod
    def _put_path(condition: dict[str, Any], path: list[dict[str, Any]], value: Any, coordinate: str) -> None:
        current = condition
        for segment in path[:-1]:
            if segment.get("array"):
                raise StructuralError(coordinate, "array-valued root path is ambiguous")
            name = segment["name"]
            existing = current.setdefault(name, {})
            if not isinstance(existing, dict):
                raise StructuralError(coordinate, f"root path {name!r} conflicts with another field")
            current = existing
        final = path[-1]
        if final.get("array"):
            raise StructuralError(coordinate, "array-valued root path is ambiguous")
        name = final["name"]
        if name in current:
            raise StructuralError(coordinate, f"duplicate serialized field {name!r}")
        current[name] = value

    def _serialize_reference(
        self, value: Any, package: VerifiedBindingPackage,
        reference: dict[str, Any], coordinate: str, active: set[tuple[int, int]],
    ) -> Any:
        if value is None and reference.get("nullable"):
            return None
        if "type" in reference:
            named = self.types[package.installed.import_package].get(reference["type"])
            if named is None:
                raise StructuralError(coordinate, f"unresolved native type {reference['type']}")
            return self._serialize_type(value, package, named, coordinate, active)
        builtin = reference["builtin"]
        if builtin in ("any", "JSONValue"):
            return _json_value(value, coordinate)
        expected = {"str": str, "string": str, "bool": bool, "int": int, "int64": int,
                    "float": (float, int), "float64": (float, int), "None": type(None), "struct{}": type(None)}[builtin]
        if not isinstance(value, expected) or (builtin in ("int", "int64", "float", "float64") and isinstance(value, bool)):
            raise StructuralError(coordinate, f"expected {builtin} value")
        if isinstance(value, float) and not math.isfinite(value):
            raise StructuralError(coordinate, "non-finite number is not serializable")
        return value

    def _serialize_type(
        self, value: Any, package: VerifiedBindingPackage,
        named: dict[str, Any], coordinate: str, active: set[tuple[int, int]],
    ) -> Any:
        token = (id(value), id(named))
        if token in active:
            raise StructuralError(coordinate, "cyclic structural value")
        if len(active) >= 256:
            raise StructuralError(coordinate, "structural value exceeds nesting limit")
        active.add(token)
        try:
            return self._serialize_type_body(value, package, named, coordinate, active)
        finally:
            active.remove(token)

    def _serialize_type_body(
        self, value: Any, package: VerifiedBindingPackage,
        named: dict[str, Any], coordinate: str, active: set[tuple[int, int]],
    ) -> Any:
        kind = named["construct"]
        name = named["nativeName"]
        package_name = package.installed.import_package
        if kind == "scalar":
            members = named.get("members", [])
            if members:
                symbol = value if isinstance(value, NativeSymbol) else None
                path = symbol.path[1:] if symbol and symbol.path[:1] == ("bindings",) else symbol.path if symbol else ()
                if symbol is None or symbol.package != package_name or len(path) != 2 or path[0] != name:
                    if isinstance(value, str):
                        self._structural_fallbacks.add(coordinate)
                        return value
                    if symbol is not None and len(path) == 2:
                        other_type = self.types.get(symbol.package, {}).get(path[0])
                        if other_type is not None and other_type["construct"] == "scalar":
                            other_member = next(
                                (item for item in other_type.get("members", []) if item["nativeName"] == path[1]),
                                None,
                            )
                            if other_member is not None:
                                self._structural_fallbacks.add(coordinate)
                                return other_member["value"]
                    raise StructuralError(coordinate, f"expected {package_name}.{name} enum member")
                member = next((item for item in members if item["nativeName"] == path[1]), None)
                if member is None:
                    raise StructuralError(coordinate, f"unknown {name} enum member {path[1]}")
                return member["value"]
            return self._serialize_reference(value, package, {"builtin": named["underlying"]}, coordinate, active)
        if kind in ("object", "wrapper"):
            if not isinstance(value, StaticCall) or _native(value.callee) != (package_name, name):
                raise StructuralError(coordinate, f"expected {package_name}.{name} keyword-only constructor")
            if value.args:
                raise StructuralError(coordinate, f"{name} constructor is keyword-only")
            fields = {item["nativeName"]: item for item in named["fields"]}
            unknown = set(value.kwargs) - set(fields)
            if unknown:
                raise StructuralError(coordinate, f"unknown {name} field {sorted(unknown)[0]}")
            result: dict[str, Any] = {}
            for native_name, field in fields.items():
                if native_name not in value.kwargs:
                    if field["required"]:
                        raise StructuralError(_coordinate(field), f"missing required field {native_name}")
                    continue
                item = value.kwargs[native_name]
                if item is None and not field["required"]:
                    if not field["value"].get("nullable"):
                        raise StructuralError(_coordinate(field), f"field {native_name} cannot be None")
                    continue
                result[field["sourceName"]] = self._serialize_reference(
                    item, package, field["value"], _coordinate(field), active,
                )
            if kind == "wrapper":
                if len(fields) != 1 or len(result) != 1:
                    raise StructuralError(coordinate, f"{name} wrapper must provide one value")
                return next(iter(result.values()))
            return result
        if kind == "collection":
            if not isinstance(value, list):
                raise StructuralError(coordinate, f"expected sequence for {name}")
            edge = named["element"]
            return [self._serialize_reference(item, package, edge["value"], _coordinate(edge), active) for item in value]
        if kind == "map":
            if not isinstance(value, dict):
                raise StructuralError(coordinate, f"expected map for {name}")
            edge = named["element"]
            return {key: self._serialize_reference(item, package, edge["value"], _coordinate(edge), active)
                    for key, item in value.items()}
        if kind == "union":
            successes: list[Any] = []
            for variant in named["variants"]:
                try:
                    successes.append(self._serialize_reference(
                        value, package, variant["value"], _coordinate(variant), active,
                    ))
                except StructuralError:
                    continue
            if not successes:
                if value is None or type(value) in (str, bool, int, float, list, dict):
                    self._structural_fallbacks.add(coordinate)
                    return _json_value(value, coordinate)
                raise StructuralError(coordinate, f"no structural variant of {name} accepts value")
            if len(successes) > 1:
                raise StructuralError(coordinate, f"ambiguous structural variants of {name}")
            return successes[0]
        if kind == "any":
            return _json_value(value, coordinate)
        raise StructuralError(coordinate, f"unsupported structural construct {kind}")
