from __future__ import annotations

import ast
import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..constants import EXTENSION_KIND, SDK_MAPPING_API_VERSION, SDK_MAPPING_KIND
from ..errors import RuntimeConditionsError
from ..extension.identity import definition_identifier, parse_identifier
from ..extension.definition import parse_extension_definition
from ..models import Diagnostic, SDKExtensionArtifact, SDKMappingArtifact
from ..source.python import expression_name
from ..yamlio import Yaml


def _semantic_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_explicit_sdk_artifacts(
    mapping_paths: list[Path], extension_paths: list[Path]
) -> tuple[list[SDKMappingArtifact], list[SDKExtensionArtifact], list[Diagnostic]]:
    mappings: list[SDKMappingArtifact] = []
    extensions: list[SDKExtensionArtifact] = []
    diagnostics: list[Diagnostic] = []
    for raw_path in mapping_paths:
        path = raw_path.absolute().resolve()
        try:
            document = Yaml.load(path)
            _validate_mapping_shape(document, path)
            sdk = document["sdk"]
            metadata = document["metadata"]
            mappings.append(
                SDKMappingArtifact(
                    distribution=sdk["package"],
                    distribution_version="",
                    name=metadata["name"],
                    index_path=path,
                    mapping_path=path,
                    mapping_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    mapping=document,
                )
            )
        except Exception as exc:
            diagnostics.append(Diagnostic("error", "sdk-mapping", str(path), str(exc)))
    for raw_path in extension_paths:
        path = raw_path.absolute().resolve()
        try:
            document = Yaml.load(path)
            if document.get("kind") != EXTENSION_KIND:
                raise RuntimeConditionsError(f"expected {EXTENSION_KIND}")
            metadata = document.get("metadata", {})
            extension_id = definition_identifier(metadata)
            version = metadata.get("version", "")
            digest = metadata.get("semanticSha256")
            if not isinstance(version, str) or ("version" in metadata and not version):
                raise RuntimeConditionsError("extension metadata version, when present, must be a non-empty string")
            if digest is not None and (not isinstance(digest, str) or not digest):
                raise RuntimeConditionsError("extension metadata semanticSha256, when present, must be a non-empty string")
            if not version and not digest:
                raise RuntimeConditionsError("extension metadata requires either version or semanticSha256")
            actual = _semantic_sha256(document.get("spec", {}))
            if digest is not None and actual != digest:
                raise RuntimeConditionsError(f"extension semantic digest is {actual}, not {digest}")
            extensions.append(
                SDKExtensionArtifact(
                    id=extension_id,
                    version=version,
                    semantic_sha256=actual,
                    path=path,
                    definition=parse_extension_definition(document, extension_id, path.as_uri()),
                    document=document,
                )
            )
        except Exception as exc:
            diagnostics.append(Diagnostic("error", "sdk-extension", str(path), str(exc)))
    extension_ids = {(item.id, item.version) for item in extensions}
    for mapping in mappings:
        extension_id = parse_identifier(mapping.mapping["extension"])
        if extension_id not in extension_ids:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "sdk-mapping",
                    str(mapping.mapping_path),
                    f"selected extension {extension_id} was not supplied explicitly",
                )
            )
    return mappings, extensions, diagnostics


def _validate_mapping_shape(document: dict[str, Any], path: Path) -> None:
    if document.get("apiVersion") != SDK_MAPPING_API_VERSION or document.get("kind") != SDK_MAPPING_KIND:
        raise RuntimeConditionsError(f"{path}: unsupported SDK mapping contract")
    metadata = document.get("metadata")
    sdk = document.get("sdk")
    extension = document.get("extension")
    rules = document.get("rules")
    if not isinstance(metadata, dict) or not all(isinstance(metadata.get(key), str) and metadata[key] for key in ("name", "version")):
        raise RuntimeConditionsError("mapping metadata requires name and version")
    if not isinstance(sdk, dict) or sdk.get("language") != "python" or not all(isinstance(sdk.get(key), str) and sdk[key] for key in ("ecosystem", "package")):
        raise RuntimeConditionsError("mapping requires a Python SDK ecosystem and package")
    parse_identifier(extension)
    if not isinstance(rules, list) or not rules:
        raise RuntimeConditionsError("mapping requires at least one rule")
    rule_ids: set[str] = set()
    surfaces: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str):
            raise RuntimeConditionsError("every mapping rule requires an id")
        if rule["id"] in rule_ids:
            raise RuntimeConditionsError(f"duplicate mapping rule {rule['id']}")
        rule_ids.add(rule["id"])
        match = rule.get("match", {})
        if not isinstance(match, dict) or ("call" in match) == ("method" in match):
            raise RuntimeConditionsError(f"{rule['id']}: match must contain exactly one call or method")
        produced = rule.get("produces")
        condition = rule.get("condition")
        if (produced is None) == (condition is None):
            raise RuntimeConditionsError(f"{rule['id']}: rule must produce a surface or a Condition")
        if produced is not None:
            if not isinstance(produced, dict) or not isinstance(produced.get("surface"), str):
                raise RuntimeConditionsError(f"{rule['id']}: produced surface is required")
            surfaces.add(produced["surface"])
        if "method" in match:
            method = match["method"]
            if not isinstance(method, dict) or method.get("receiverSurface") not in surfaces or not isinstance(method.get("name"), str):
                raise RuntimeConditionsError(f"{rule['id']}: method must reference an earlier produced surface")
        if condition is not None and (not isinstance(condition, dict) or not isinstance(condition.get("writes"), list) or not condition["writes"]):
            raise RuntimeConditionsError(f"{rule['id']}: Condition writes are required")


@dataclass(frozen=True)
class Evidence:
    key: str
    value: Any = None
    static: bool = False


@dataclass(frozen=True)
class Surface:
    name: str
    identity: Evidence | None = None


@dataclass(frozen=True)
class External:
    symbol: str


UNKNOWN = Evidence("unknown")


@dataclass
class ConditionResult:
    extension_id: tuple[str, str]
    identity: str
    document: dict[str, Any]


class DirectSDKPythonExtractor:
    def __init__(self, mappings: list[SDKMappingArtifact], extensions: list[SDKExtensionArtifact]) -> None:
        self.mappings = [item for item in mappings if isinstance(item.mapping.get("sdk"), dict)]
        self.extensions = {(item.id, item.version): item for item in extensions}
        self.call_rules: list[tuple[SDKMappingArtifact, dict[str, Any]]] = []
        self.method_rules: dict[tuple[str, str], tuple[SDKMappingArtifact, dict[str, Any]]] = {}
        for artifact in self.mappings:
            extension_id = parse_identifier(artifact.mapping["extension"])
            extension = self.extensions.get(extension_id)
            if extension is None:
                raise RuntimeConditionsError(f"{artifact.name}: extension {extension_id} is unavailable")
            self._validate_writes(artifact, extension)
            for rule in artifact.mapping["rules"]:
                match = rule["match"]
                if "call" in match:
                    self.call_rules.append((artifact, rule))
                else:
                    method = match["method"]
                    key = (method["receiverSurface"], method["name"])
                    if key in self.method_rules:
                        raise RuntimeConditionsError(f"ambiguous SDK method rule for {key[0]}.{key[1]}")
                    self.method_rules[key] = (artifact, rule)

    def extract(self, source_files: list[Path], project_root: Path) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        results: list[ConditionResult] = []
        for source in source_files:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            interpreter = _PythonMappingInterpreter(self, source, tree)
            results.extend(interpreter.run())
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for result in results:
            interface = result.document.get("interface", {})
            key = (
                result.extension_id,
                str(result.document.get("kind")),
                str(interface.get("type")),
                result.identity,
            )
            existing = grouped.get(key)
            if existing is None:
                grouped[key] = copy.deepcopy(result.document)
            else:
                _merge_condition(existing, result.document)
        for (extension_id, _, _, _), condition in grouped.items():
            self._validate_materialized(self.extensions[extension_id], condition)
        extension_ids = sorted({key[0] for key in grouped})
        return list(grouped.values()), extension_ids

    def evaluate_call_rule(self, symbol: str, call: ast.Call, scope: str, values: dict[str, Any]) -> Surface | None:
        candidates: list[tuple[SDKMappingArtifact, dict[str, Any]]] = []
        for artifact, rule in self.call_rules:
            call_match = rule["match"]["call"]
            if symbol not in call_match.get("symbols", []):
                continue
            if all(_selector_matches(call, selector, scope, values) for selector in call_match.get("arguments", {}).values()):
                candidates.append((artifact, rule))
        if len(candidates) > 1:
            raise RuntimeConditionsError(f"ambiguous SDK call rule for {symbol}")
        if not candidates:
            return None
        produced = candidates[0][1]["produces"]
        identity = _resolve_identity(produced.get("identity"), call, None, scope, values)
        return Surface(produced["surface"], identity)

    def evaluate_method(
        self,
        receiver: Surface,
        name: str,
        call: ast.Call,
        scope: str,
        values: dict[str, Any],
        location: str,
    ) -> tuple[Surface | Evidence, list[ConditionResult]]:
        match = self.method_rules.get((receiver.name, name))
        if match is None:
            return UNKNOWN, []
        artifact, rule = match
        produced = rule.get("produces")
        if produced is not None:
            identity = _resolve_identity(produced.get("identity"), call, receiver, scope, values)
            return Surface(produced["surface"], identity), []
        condition = rule["condition"]
        identity = _resolve_identity(condition.get("identity"), call, receiver, scope, values)
        document: dict[str, Any] = {}
        for write in condition["writes"]:
            written = self._write_value(write, call, receiver, scope, values)
            if written is None:
                continue
            if "values" in write:
                for value in written:
                    _write_target(document, write["target"], value)
            else:
                _write_target(document, write["target"], written)
        result_identity = identity.key if identity is not None else location
        return UNKNOWN, [ConditionResult(parse_identifier(artifact.mapping["extension"]), result_identity, document)]

    def _write_value(
        self,
        write: dict[str, Any],
        call: ast.Call,
        receiver: Surface,
        scope: str,
        values: dict[str, Any],
    ) -> Any:
        if "value" in write:
            return copy.deepcopy(write["value"])
        if "values" in write:
            return copy.deepcopy(write["values"])
        if "argument" in write:
            evidence = _argument_evidence(call, write["argument"], scope, values)
        elif write.get("receiverIdentity") is True:
            evidence = receiver.identity or UNKNOWN
        else:
            raise RuntimeConditionsError(f"unsupported write instruction {write}")
        if not evidence.static:
            if write.get("whenStaticallyKnown") is True:
                return None
            raise RuntimeConditionsError(f"write target {write.get('target')} requires a statically known value")
        return copy.deepcopy(evidence.value)

    def _validate_writes(self, artifact: SDKMappingArtifact, extension: SDKExtensionArtifact) -> None:
        definition = extension.definition
        fields = set(definition.interface_fields)
        values = {
            (field, kind, interface_type): set(allowed)
            for field, kind, interface_type, allowed in definition.field_values
        }
        for rule in artifact.mapping["rules"]:
            condition = rule.get("condition")
            if not condition:
                continue
            constants: dict[str, Any] = {}
            for write in condition["writes"]:
                if "value" in write:
                    _write_target(constants, write["target"], copy.deepcopy(write["value"]))
                for value in write.get("values", []):
                    _write_target(constants, write["target"], copy.deepcopy(value))
            kind = constants.get("kind")
            interface_type = constants.get("interface", {}).get("type")
            if kind not in definition.kinds or (kind, interface_type) not in definition.interface_types:
                raise RuntimeConditionsError(f"{rule['id']}: writes do not select extension-defined kind and interface type")
            for write in condition["writes"]:
                target = write["target"]
                if target.startswith("interface.") and target != "interface.type":
                    field = target.split(".")[1].removesuffix("[]")
                    if (kind, interface_type, field) not in fields:
                        raise RuntimeConditionsError(f"{rule['id']}: target {target} is not extension vocabulary")
                candidates = write.get("values", [write.get("value")])
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    for child, child_value in candidate.items():
                        allowed = values.get((f"{target}.{child}", kind, interface_type))
                        if allowed is not None and str(child_value) not in allowed:
                            raise RuntimeConditionsError(f"{rule['id']}: value {child_value} is not allowed at {target}.{child}")

    def _validate_materialized(self, extension: SDKExtensionArtifact, condition: dict[str, Any]) -> None:
        kind = condition.get("kind")
        interface_type = condition.get("interface", {}).get("type")
        matches = [
            item.get("schema")
            for item in extension.document.get("spec", {}).get("schemas", [])
            if isinstance(item, dict)
            and item.get("appliesToKind") == kind
            and item.get("appliesToInterfaceType") == interface_type
        ]
        if len(matches) != 1 or not isinstance(matches[0], dict):
            raise RuntimeConditionsError(f"extension {extension.id} has no unique schema for {kind}/{interface_type}")
        errors = list(Draft202012Validator(matches[0]).iter_errors(condition))
        if errors:
            raise RuntimeConditionsError(f"mapped Condition violates extension {extension.id}: {errors[0].message}")


class _PythonMappingInterpreter:
    def __init__(self, extractor: DirectSDKPythonExtractor, source: Path, tree: ast.Module) -> None:
        self.extractor = extractor
        self.source = source
        self.tree = tree
        self.aliases: dict[str, str] = {}
        self.globals: dict[str, Any] = {}
        self.results: list[ConditionResult] = []

    def run(self) -> list[ConditionResult]:
        for statement in self.tree.body:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                self._import(statement)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                self._assign(statement, self.globals, "module")
        for statement in self.tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = f"{self.source}:{statement.name}"
                values = dict(self.globals)
                for argument in [*statement.args.posonlyargs, *statement.args.args, *statement.args.kwonlyargs]:
                    values[argument.arg] = Evidence(f"{scope}:parameter:{argument.arg}")
                for child in statement.body:
                    self._statement(child, values, scope)
            elif not isinstance(statement, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.ClassDef)):
                self._statement(statement, self.globals, "module")
        return self.results

    def _import(self, statement: ast.Import | ast.ImportFrom) -> None:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                self.aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif statement.module:
            for alias in statement.names:
                if alias.name != "*":
                    self.aliases[alias.asname or alias.name] = f"{statement.module}.{alias.name}"

    def _statement(self, statement: ast.stmt, values: dict[str, Any], scope: str) -> None:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            self._assign(statement, values, scope)
            return
        if isinstance(statement, ast.Expr):
            self._evaluate(statement.value, values, scope)
            return
        if isinstance(statement, ast.Return) and statement.value is not None:
            self._evaluate(statement.value, values, scope)
            return
        for field in ("body", "orelse", "finalbody"):
            children = getattr(statement, field, None)
            if isinstance(children, list):
                branch = dict(values)
                for child in children:
                    if isinstance(child, ast.stmt):
                        self._statement(child, branch, scope)

    def _assign(self, statement: ast.Assign | ast.AnnAssign, values: dict[str, Any], scope: str) -> None:
        expression = statement.value
        if expression is None:
            return
        value = self._evaluate(expression, values, scope)
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id] = value

    def _evaluate(self, expression: ast.expr, values: dict[str, Any], scope: str) -> Any:
        if isinstance(expression, ast.Await):
            return self._evaluate(expression.value, values, scope)
        if isinstance(expression, ast.Constant):
            return Evidence(f"literal:{expression.value!r}", expression.value, True)
        if isinstance(expression, ast.Name):
            return values.get(expression.id, Evidence(f"{scope}:name:{expression.id}"))
        if not isinstance(expression, ast.Call):
            return Evidence(f"{scope}:expression:{ast.dump(expression, include_attributes=False)}")
        for argument in expression.args:
            self._evaluate(argument, values, scope)
        for keyword in expression.keywords:
            self._evaluate(keyword.value, values, scope)
        location = f"{self.source}:{getattr(expression, 'lineno', 0)}:{getattr(expression, 'col_offset', 0)}"
        if isinstance(expression.func, ast.Attribute):
            receiver = self._evaluate(expression.func.value, values, scope)
            if isinstance(receiver, Surface):
                value, results = self.extractor.evaluate_method(
                    receiver, expression.func.attr, expression, scope, values, location
                )
                self.results.extend(results)
                return value
        symbol = self._callable_symbol(expression.func, values, scope)
        if symbol:
            produced = self.extractor.evaluate_call_rule(symbol, expression, scope, values)
            if produced is not None:
                return produced
            return External(symbol)
        return UNKNOWN

    def _callable_symbol(self, expression: ast.expr, values: dict[str, Any], scope: str) -> str | None:
        if isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Name):
            receiver = values.get(expression.value.id)
            if isinstance(receiver, External):
                return f"{receiver.symbol}.{expression.attr}"
        name = expression_name(expression)
        if name:
            parts = name.split(".")
            replacement = self.aliases.get(parts[0])
            return ".".join([replacement, *parts[1:]]) if replacement else name
        if isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Call):
            constructor = self._callable_symbol(expression.value.func, values, scope)
            if constructor:
                return f"{constructor}.{expression.attr}"
        return None


def _binding_expression(call: ast.Call, binding: dict[str, Any]) -> ast.expr | None:
    keyword = binding.get("keyword")
    if isinstance(keyword, str):
        for item in call.keywords:
            if item.arg == keyword:
                return item.value
    position = binding.get("position")
    if isinstance(position, int) and position < len(call.args):
        return call.args[position]
    return None


def _evidence(expression: ast.expr | None, scope: str, values: dict[str, Any]) -> Evidence:
    if expression is None:
        return Evidence(f"{scope}:missing")
    if isinstance(expression, ast.Constant):
        return Evidence(f"literal:{expression.value!r}", expression.value, True)
    if isinstance(expression, ast.Name):
        value = values.get(expression.id)
        if isinstance(value, Evidence):
            return value
        return Evidence(f"{scope}:name:{expression.id}")
    return Evidence(f"{scope}:expression:{ast.dump(expression, include_attributes=False)}")


def _argument_evidence(call: ast.Call, binding: dict[str, Any], scope: str, values: dict[str, Any]) -> Evidence:
    return _evidence(_binding_expression(call, binding), scope, values)


def _selector_matches(call: ast.Call, selector: dict[str, Any], scope: str, values: dict[str, Any]) -> bool:
    evidence = _argument_evidence(call, selector, scope, values)
    return evidence.static and evidence.value == selector.get("equals")


def _resolve_identity(
    identity: Any,
    call: ast.Call,
    receiver: Surface | None,
    scope: str,
    values: dict[str, Any],
) -> Evidence | None:
    if not isinstance(identity, dict):
        return None
    if isinstance(identity.get("argument"), dict):
        return _argument_evidence(call, identity["argument"], scope, values)
    if identity.get("receiver") is True:
        return receiver.identity if receiver else None
    raise RuntimeConditionsError(f"unsupported Condition identity {identity}")


def _write_target(document: dict[str, Any], target: str, value: Any) -> None:
    current: dict[str, Any] = document
    parts = target.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    leaf = parts[-1]
    if leaf.endswith("[]"):
        current.setdefault(leaf[:-2], []).append(value)
    else:
        current[leaf] = value


def _merge_condition(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _merge_condition(target[key], value)
        elif isinstance(target[key], list) and isinstance(value, list):
            for item in value:
                if item not in target[key]:
                    target[key].append(copy.deepcopy(item))
        elif target[key] != value:
            raise RuntimeConditionsError(f"conflicting SDK mapping writes at {key}")
