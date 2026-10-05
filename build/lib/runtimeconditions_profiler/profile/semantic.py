"""Validate generated profiles against installed extension definitions.

The core schema is a pinned wheel resource. Extension schemas and vocabulary
come only from the already verified binding distributions, never from a source
checkout, runtime import, or an implicit network fetch.
"""

from __future__ import annotations

import hashlib
from importlib import resources
from typing import Any

import rfc8785
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource, Unresolvable
from referencing.jsonschema import DRAFT202012

from ..constants import API_VERSION
from ..errors import RuntimeConditionsError
from ..project.verify import VerifiedBindingPackage, VerifiedBindingSet
from .generated import ExtractedCondition


CORE_RESOURCE = "runtimeconditions.profile.v0.2.0.schema.yaml"
CORE_ID = "https://runtimeconditions.io/schemas/profile/0.2.0/runtimeconditions.profile.schema.yaml"
CORE_VERSION = "0.2.0"
CORE_SEMANTIC_SHA256 = "a090a8016d045f9c3fa872a67f8df293b77ca2809a1bea5ae9fa31a27a06109a"
CORE_SOURCE_SHA256 = "342bf20bce479f5012b9fc2c6238dc1fb0935e327ecb0fbca6e647362563c73c"
CORE_FIELDS = frozenset({"kind", "interface", "name", "optional"})


def _fail(location: str, message: str) -> None:
    raise RuntimeConditionsError(f"{location}: {message}")


def _source(item: ExtractedCondition, index: int, path: str = "") -> str:
    suffix = f"/{path}" if path else ""
    return f"{item.source}:{item.line}:{item.column}: conditions[{index}]{suffix}"


def _core_schema() -> dict[str, Any]:
    try:
        data = resources.files("runtimeconditions_profiler").joinpath(CORE_RESOURCE).read_bytes()
        if hashlib.sha256(data).hexdigest() != CORE_SOURCE_SHA256:
            raise ValueError("source digest mismatch")
        schema = yaml.safe_load(data)
        if not isinstance(schema, dict):
            raise ValueError("schema is not a mapping")
        if schema.get("$id") != CORE_ID or schema.get("x-runtimeconditions-version") != CORE_VERSION:
            raise ValueError("schema identity mismatch")
        if hashlib.sha256(rfc8785.dumps(schema)).hexdigest() != CORE_SEMANTIC_SHA256:
            raise ValueError("semantic digest mismatch")
        Draft202012Validator.check_schema(schema)
        return schema
    except (OSError, UnicodeError, ValueError, yaml.YAMLError, SchemaError) as exc:
        raise RuntimeConditionsError(f"bundled core profile schema is unavailable: {exc}") from exc


def _strict_equal(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _path_values(value: Any, path: str) -> list[Any]:
    current = [value]
    for raw in path.split("."):
        array = raw.endswith("[]")
        key = raw[:-2] if array else raw
        following: list[Any] = []
        for item in current:
            if not isinstance(item, dict) or key not in item:
                continue
            child = item[key]
            if array:
                if isinstance(child, list):
                    following.extend(child)
            else:
                following.append(child)
        current = following
    return current


class GeneratedProfileValidator:
    def __init__(self, verified: VerifiedBindingSet) -> None:
        self.verified = verified
        self.by_id: dict[str, VerifiedBindingPackage] = {}
        expected_core = {
            "id": CORE_ID, "version": CORE_VERSION,
            "semanticSha256": CORE_SEMANTIC_SHA256,
        }
        _core_schema()
        for package in verified.packages:
            name = package.installed.distribution
            if package.model["coreProfileSchema"] != expected_core:
                _fail(name, "binding model core profile schema identity or digest mismatch")
            extension_id = package.model["rootExtension"]["id"]
            if extension_id in self.by_id:
                _fail(name, f"duplicate installed extension {extension_id}")
            self.by_id[extension_id] = package

    def build(
        self, extracted: tuple[ExtractedCondition, ...],
        name: str, workload_uri: str, workload_version: str,
    ) -> dict[str, Any]:
        direct = {extension for item in extracted for extension in item.direct_extensions}
        profile = {
            "apiVersion": API_VERSION,
            "kind": "RuntimeConditionsProfile",
            "metadata": {"name": name},
            "workload": {"uri": workload_uri, "version": workload_version},
            "extensions": sorted(direct),
            "conditions": [item.condition for item in extracted],
        }
        self._validate_core(profile, extracted)
        closure = self._closure(direct)
        added: set[str] = set()
        for index, item in enumerate(extracted):
            added.update(self._validate_vocabulary(item, index, closure))
        direct.update(added)
        closure = self._closure(direct)
        self._validate_model_closures(direct, closure)
        profile["extensions"] = sorted(direct)
        self._validate_core(profile, extracted)
        self._validate_extension_schemas(extracted, closure)
        for index, item in enumerate(extracted):
            if item.structural_fallbacks:
                _fail(
                    _source(item, index),
                    "static value is outside binding structure at " + ", ".join(item.structural_fallbacks),
                )
        return profile

    def _closure(self, direct: set[str]) -> dict[str, VerifiedBindingPackage]:
        found: dict[str, VerifiedBindingPackage] = {}
        visiting: set[str] = set()

        def visit(extension_id: str) -> None:
            if extension_id in visiting:
                _fail(extension_id, "extension dependency cycle")
            if extension_id in found:
                return
            package = self.by_id.get(extension_id)
            if package is None:
                _fail(extension_id, "extension has no verified installed binding package")
            visiting.add(extension_id)
            for dependency in sorted(package.extension["spec"].get("dependencies", [])):
                visit(dependency)
            visiting.remove(extension_id)
            found[extension_id] = package

        for extension_id in sorted(direct):
            visit(extension_id)
        return found

    def _validate_model_closures(
        self, direct: set[str], closure: dict[str, VerifiedBindingPackage],
    ) -> None:
        for extension_id in sorted(direct):
            package = closure[extension_id]
            expected = set(self._closure({extension_id}))
            model = package.model["extensions"]
            actual = {item["id"] for item in model}
            if len(actual) != len(model) or actual != expected:
                _fail(extension_id, "binding model extension closure differs from installed dependency graph")
            for item in model:
                owner = self.by_id[item["id"]]
                identity = owner.model["rootExtension"]
                if any(item.get(key) != identity.get(key) for key in ("version", "semanticSha256")):
                    _fail(extension_id, f"extension identity mismatch for {item['id']}")

    def _validate_core(
        self, profile: dict[str, Any], extracted: tuple[ExtractedCondition, ...],
    ) -> None:
        validator = Draft202012Validator(_core_schema(), format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(profile), key=lambda item: (list(map(str, item.absolute_path)), item.message))
        if errors:
            issue = errors[0]
            path = list(issue.absolute_path)
            if len(path) >= 2 and path[0] == "conditions" and isinstance(path[1], int) and path[1] < len(extracted):
                location = _source(extracted[path[1]], path[1], "/".join(map(str, path[2:])))
            else:
                location = "profile/" + "/".join(map(str, path))
            _fail(location, f"core schema: {issue.message}")
        seen: set[str] = set()
        for index, item in enumerate(extracted):
            condition_name = item.condition.get("name")
            if condition_name is not None:
                if condition_name in seen:
                    _fail(_source(item, index, "name"), f"duplicate Condition name {condition_name!r}")
                seen.add(condition_name)

    @staticmethod
    def _matching_schemas(
        condition: dict[str, Any], closure: dict[str, VerifiedBindingPackage],
    ) -> list[tuple[str, str, dict[str, Any]]]:
        kind = condition.get("kind")
        interface = condition.get("interface")
        interface_type = interface.get("type") if isinstance(interface, dict) else None
        matched: list[tuple[str, str, dict[str, Any]]] = []
        for owner in sorted(closure):
            for item in closure[owner].extension["spec"].get("schemas", []):
                if item.get("appliesToKind") not in (None, kind):
                    continue
                if item.get("appliesToInterfaceType") not in (None, interface_type):
                    continue
                matched.append((owner, item["id"], item["schema"]))
        return matched

    def _validate_vocabulary(
        self, item: ExtractedCondition, index: int,
        closure: dict[str, VerifiedBindingPackage],
    ) -> set[str]:
        condition = item.condition
        kind = condition.get("kind")
        interface = condition.get("interface")
        interface_type = interface.get("type") if isinstance(interface, dict) else None
        location = _source(item, index)
        kind_owners = {
            owner for owner, package in closure.items()
            for definition in package.extension["spec"].get("kinds", [])
            if definition["name"] == kind
        }
        if len(kind_owners) != 1:
            _fail(location + "/kind", f"kind {kind!r} must have exactly one vocabulary owner")
        contributors = set(kind_owners)
        interface_owners = {
            owner for owner, package in closure.items()
            for definition in package.extension["spec"].get("interfaceTypes", [])
            if definition["name"] == interface_type and definition["targetKind"] == kind
        }
        # The core schema reports a missing interface with its source location.
        if interface is not None and len(interface_owners) != 1:
            _fail(location + "/interface/type", f"interface type {interface_type!r} must have exactly one vocabulary owner")
        contributors.update(interface_owners)
        schemas = self._matching_schemas(condition, closure)

        def manifest_owners(path: tuple[str, ...]) -> set[str]:
            owners: set[str] = set()
            for owner, package in closure.items():
                types = {entry["nativeName"]: entry for entry in package.manifest["types"]}
                for binding in package.manifest["rootBindings"]:
                    if binding["scope"]["kind"] != kind:
                        continue
                    if binding["scope"].get("interfaceType") not in (None, interface_type):
                        continue
                    if not binding["path"] or binding["path"][0]["name"] != path[0]:
                        continue
                    if len(path) == 1:
                        owners.add(owner)
                    elif len(path) == 2 and binding["value"].get("type") in types:
                        named = types[binding["value"]["type"]]
                        if any(field["sourceName"] == path[1] for field in named.get("fields", [])):
                            owners.add(owner)
            return owners

        def schema_owners(path: tuple[str, ...]) -> set[str]:
            owners: set[str] = set()
            for owner, _, schema in schemas:
                current: Any = schema
                for name in path:
                    if not isinstance(current, dict):
                        break
                    current = current.get("properties", {}).get(name)
                if current is not None:
                    owners.add(owner)
            return owners

        for field in condition:
            if field in CORE_FIELDS:
                continue
            owners = {
                owner for owner, package in closure.items()
                for definition in package.extension["spec"].get("conditionFields", [])
                if definition["name"] == field
                and kind in definition["appliesToKinds"]
                and (not definition.get("appliesToInterfaceTypes")
                     or interface_type in definition["appliesToInterfaceTypes"])
            }
            if not owners:
                owners = manifest_owners((field,))
            if not owners:
                owners = schema_owners((field,))
            if len(owners) != 1:
                _fail(location + f"/{field}", f"condition field {field!r} must have exactly one owner")
            contributors.update(owners)
        if isinstance(interface, dict):
            for field in interface:
                if field == "type":
                    continue
                owners = {
                    owner for owner, package in closure.items()
                    for definition in package.extension["spec"].get("interfaceFields", [])
                    if definition["name"] == field
                    and definition["targetKind"] == kind
                    and definition["targetType"] == interface_type
                }
                if not owners:
                    owners = manifest_owners(("interface", field))
                if not owners:
                    owners = schema_owners(("interface", field))
                if len(owners) != 1:
                    _fail(location + f"/interface/{field}", f"interface field {field!r} must have exactly one owner")
                contributors.update(owners)
        domains: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for owner, package in closure.items():
            for definition in package.extension["spec"].get("fieldValues", []):
                if definition["targetKind"] == kind and definition.get("targetType") in (None, interface_type):
                    domains.setdefault(definition["field"], []).append((owner, definition))
        for path, entries in sorted(domains.items()):
            values = _path_values(condition, path)
            if not values:
                continue
            if len(entries) != 1:
                _fail(location + f"/{path}", "field value domain must have exactly one owner")
            owner, definition = entries[0]
            for value in values:
                if not any(_strict_equal(value, allowed) for allowed in definition["values"]):
                    _fail(location + f"/{path}", f"value {value!r} is outside the owned domain")
            contributors.add(owner)
        if not set(item.direct_extensions) <= contributors:
            extra = sorted(set(item.direct_extensions) - contributors)
            _fail(location, f"binding contributors have no matching vocabulary ownership: {extra}")
        return contributors

    def _validate_extension_schemas(
        self, extracted: tuple[ExtractedCondition, ...],
        closure: dict[str, VerifiedBindingPackage],
    ) -> None:
        schema_items = [
            (owner, item["id"], item["schema"])
            for owner, package in sorted(closure.items())
            for item in package.extension["spec"].get("schemas", [])
        ]
        def reject_external(uri: str) -> Resource[Any]:
            raise NoSuchResource(uri)

        registry = Registry(retrieve=reject_external)
        registered: set[str] = set()
        for owner, schema_id, schema in schema_items:
            try:
                Draft202012Validator.check_schema(schema)
                if "$id" in schema:
                    if schema["$id"] in registered:
                        _fail(owner, f"duplicate extension schema $id {schema['$id']}")
                    registered.add(schema["$id"])
                    registry = registry.with_resource(
                        schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012),
                    )
            except (SchemaError, ValueError) as exc:
                _fail(owner, f"extension schema {schema_id} is invalid: {exc}")
        for index, item in enumerate(extracted):
            for owner, schema_id, schema in self._matching_schemas(item.condition, closure):
                try:
                    validator = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
                    errors = sorted(validator.iter_errors(item.condition), key=lambda issue: (list(map(str, issue.absolute_path)), issue.message))
                except (Unresolvable, RecursionError) as exc:
                    _fail(_source(item, index), f"extension schema {owner}/{schema_id} could not resolve: {exc}")
                if errors:
                    issue = errors[0]
                    path = "/".join(map(str, issue.absolute_path))
                    _fail(_source(item, index, path), f"extension schema {owner}/{schema_id}: {issue.message}")
