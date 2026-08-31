from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from jsonschema import Draft202012Validator

from ..errors import RuntimeConditionsError
from ..models import SDKExtensionArtifact, SDKMappingArtifact
from ..source.python import expression_name
from ..util import add_extension_closure


@dataclass(frozen=True)
class BoundCall:
    positional: list[ast.expr]
    keywords: dict[str, ast.expr]

    @staticmethod
    def from_call(call: ast.Call) -> "BoundCall":
        return BoundCall(list(call.args), {item.arg: item.value for item in call.keywords if item.arg is not None})

    def argument(self, binding: dict[str, Any]) -> ast.expr | None:
        position = binding.get("position")
        if isinstance(position, int) and position < len(self.positional):
            return self.positional[position]
        keyword = binding.get("keyword")
        return self.keywords.get(keyword) if isinstance(keyword, str) else None

    def argument_source(self, binding: dict[str, Any]) -> tuple[ast.expr | None, str]:
        position = binding.get("position")
        if isinstance(position, int) and position < len(self.positional):
            return self.positional[position], "positional"
        keyword = binding.get("keyword")
        if isinstance(keyword, str) and keyword in self.keywords:
            return self.keywords[keyword], "keyword"
        return None, ""


@dataclass(frozen=True)
class PythonTypeRef:
    module_root: str
    class_name: str


@dataclass(frozen=True)
class MappedMethod:
    artifact: SDKMappingArtifact
    method: dict[str, Any]


@dataclass(frozen=True)
class MappedDelegation:
    artifact: SDKMappingArtifact
    delegation: dict[str, Any]


@dataclass(frozen=True)
class ResolvedCondition:
    extension_id: str
    kind: str
    interface_type: str
    operation: dict[str, Any]


class SDKMappingIndex:
    def __init__(self, artifacts: list[SDKMappingArtifact]) -> None:
        self.methods: dict[tuple[str, str, str], list[MappedMethod]] = {}
        self.delegations: dict[tuple[str, str, str], list[MappedDelegation]] = {}
        self.classes: set[tuple[str, str]] = set()
        for artifact in artifacts:
            python = artifact.mapping.get("python")
            if not isinstance(python, dict):
                continue
            for method in python.get("apiMethods", []):
                if not isinstance(method, dict):
                    continue
                for symbol in method.get("symbols", []):
                    key = self._symbol_key(symbol)
                    self.methods.setdefault(key, []).append(MappedMethod(artifact, method))
                    self.classes.add((key[0], key[1]))
            for delegation in python.get("conditionDelegations", []):
                if not isinstance(delegation, dict):
                    continue
                for symbol in delegation.get("symbols", []):
                    key = self._symbol_key(symbol)
                    self.delegations.setdefault(key, []).append(MappedDelegation(artifact, delegation))
                    self.classes.add((key[0], key[1]))

    def _symbol_key(self, symbol: Any) -> tuple[str, str, str]:
        if not isinstance(symbol, dict):
            raise RuntimeConditionsError("SDK mapping contains an invalid Python symbol")
        module = symbol.get("module")
        class_name = symbol.get("class")
        method = symbol.get("method")
        if not all(isinstance(item, str) and item for item in (module, class_name, method)):
            raise RuntimeConditionsError("SDK mapping contains an incomplete Python symbol")
        return module.split(".")[0], class_name, method

    def has_class(self, reference: PythonTypeRef) -> bool:
        return (reference.module_root, reference.class_name) in self.classes

    def method(self, reference: PythonTypeRef, member: str) -> Optional[MappedMethod]:
        return self._one(self.methods.get((reference.module_root, reference.class_name, member), []), f"{reference.module_root}.{reference.class_name}.{member}")

    def delegation(self, reference: PythonTypeRef, member: str) -> Optional[MappedDelegation]:
        return self._one(self.delegations.get((reference.module_root, reference.class_name, member), []), f"{reference.module_root}.{reference.class_name}.{member}")

    def _one(self, candidates: list[Any], name: str) -> Any:
        unique: list[Any] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            key = (str(candidate.artifact.mapping_path), str(candidate.method if isinstance(candidate, MappedMethod) else candidate.delegation))
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
        if len(unique) > 1:
            raise RuntimeConditionsError(f"ambiguous installed SDK mapping for {name}")
        return unique[0] if unique else None


class ImportState:
    def __init__(self, other: "ImportState | None" = None) -> None:
        self.aliases = dict(other.aliases) if other else {}
        self.variables = dict(other.variables) if other else {}

    def normalize(self, name: str) -> str:
        parts = name.split(".")
        replacement = self.aliases.get(parts[0])
        return ".".join([replacement, *parts[1:]]) if replacement else name


class SDKSourceVisitor(ast.NodeVisitor):
    def __init__(self, mapping_index: SDKMappingIndex, resolver: "SDKConditionResolver") -> None:
        self.mapping_index = mapping_index
        self.resolver = resolver
        self.states = [ImportState()]
        self.resolved: list[ResolvedCondition] = []

    @property
    def state(self) -> ImportState:
        return self.states[-1]

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.state.aliases[alias.asname or alias.name.split(".")[0]] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not node.module:
            return
        for alias in node.names:
            if alias.name != "*":
                self.state.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.states.append(ImportState(self.state))
        for statement in node.body:
            self.visit(statement)
        self.states.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        reference = self.constructor_reference(node.value)
        if reference is None:
            return
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.state.variables[target.id] = reference

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            return
        self.visit(node.value)
        reference = self.constructor_reference(node.value)
        if reference is not None and isinstance(node.target, ast.Name):
            self.state.variables[node.target.id] = reference

    def visit_Call(self, node: ast.Call) -> None:
        identity = self.method_identity(node.func)
        if identity is not None:
            reference, member = identity
            delegation = self.mapping_index.delegation(reference, member)
            if delegation is not None:
                self._resolve_delegation(node, delegation)
            else:
                method = self.mapping_index.method(reference, member)
                if method is not None:
                    self._add(self.resolver.resolve(method, BoundCall.from_call(node)))
        self.generic_visit(node)

    def constructor_reference(self, expression: ast.expr) -> PythonTypeRef | None:
        if not isinstance(expression, ast.Call):
            return None
        name = expression_name(expression.func)
        if not name:
            return None
        normalized = self.state.normalize(name)
        parts = normalized.split(".")
        if len(parts) < 2:
            return None
        reference = PythonTypeRef(parts[0], parts[-1])
        return reference if self.mapping_index.has_class(reference) else None

    def method_identity(self, expression: ast.expr) -> tuple[PythonTypeRef, str] | None:
        if not isinstance(expression, ast.Attribute):
            return None
        if isinstance(expression.value, ast.Name):
            reference = self.state.variables.get(expression.value.id)
            if reference is not None:
                return reference, expression.attr
        reference = self.constructor_reference(expression.value)
        if reference is not None:
            return reference, expression.attr
        return None

    def _resolve_delegation(self, call: ast.Call, mapped: MappedDelegation) -> None:
        bound = BoundCall.from_call(call)
        delegate = mapped.delegation.get("delegate", {})
        callable_binding = delegate.get("callableArgument", {})
        callable_expression, source = bound.argument_source(callable_binding)
        if callable_expression is None:
            return
        identity = self.method_identity(callable_expression)
        if identity is None:
            return
        target = self.mapping_index.method(*identity)
        if target is None:
            return
        forwarded = delegate.get("forwardedArguments", {})
        positional = forwarded.get("positional", {}) if isinstance(forwarded, dict) else {}
        start = positional.get("fromPosition")
        if not isinstance(start, int):
            raise RuntimeConditionsError(f"{mapped.delegation.get('id')}: invalid forwarded positional arguments")
        forwarded_positionals = list(bound.positional[start if source == "positional" else max(0, start - 1) :])
        forwarded_keywords = dict(bound.keywords) if forwarded.get("keywords") is True else {}
        keyword_name = callable_binding.get("keyword")
        if isinstance(keyword_name, str):
            forwarded_keywords.pop(keyword_name, None)
        for activation in delegate.get("activateTargetConditionals", []):
            argument = activation.get("argument", {}) if isinstance(activation, dict) else {}
            keyword = argument.get("keyword")
            if isinstance(keyword, str) and activation.get("equals") is not None:
                forwarded_keywords[keyword] = ast.Constant(value=activation["equals"])
        self._add(self.resolver.resolve(target, BoundCall(forwarded_positionals, forwarded_keywords)))

    def _add(self, conditions: list[ResolvedCondition]) -> None:
        for condition in conditions:
            if condition not in self.resolved:
                self.resolved.append(condition)


class SDKConditionResolver:
    def __init__(self, extensions: list[SDKExtensionArtifact]) -> None:
        self.extensions = extensions

    def resolve(self, mapped: MappedMethod, call: BoundCall) -> list[ResolvedCondition]:
        mapping = mapped.artifact.mapping
        operations = {item.get("name"): item for item in mapping.get("operations", []) if isinstance(item, dict) and isinstance(item.get("name"), str)}
        reference = mapped.method.get("operationRef", {})
        record = operations.get(reference.get("operation"))
        if record is None:
            raise RuntimeConditionsError(f"unknown SDK operation reference {reference}")
        resolved: list[ResolvedCondition] = []
        if "conditions" in record:
            conditions = record.get("conditions")
            if not isinstance(conditions, list):
                raise RuntimeConditionsError("SDK operation conditions must be a list")
            for item in conditions:
                if not isinstance(item, dict) or not isinstance(item.get("operation"), dict):
                    raise RuntimeConditionsError("SDK operation condition is invalid")
                resolved.append(ResolvedCondition(mapping["extension"]["id"], item["kind"], item["interfaceType"], copy.deepcopy(item["operation"])))
        else:
            template = record.get("conditionTemplate")
            if not isinstance(template, dict):
                raise RuntimeConditionsError("SDK operation has neither conditions nor conditionTemplate")
            operation = self._resolve_template(call, template)
            if operation is None:
                return []
            resolved.append(ResolvedCondition(mapping["extension"]["id"], template["kind"], template["interfaceType"], operation))
        conditional = mapped.method.get("conditionalOperation")
        if isinstance(conditional, dict):
            predicate = call.argument(conditional.get("when", {}).get("argument", {}))
            if isinstance(predicate, ast.Constant) and predicate.value == conditional.get("when", {}).get("equals"):
                for item in resolved:
                    item.operation.update(conditional.get("operationOverride", {}))
            elif predicate is not None and not isinstance(predicate, ast.Constant):
                return []
        extension = self._extension_for(mapping)
        for item in resolved:
            self._validate_condition(extension, item)
        return resolved

    def resolve_static_operation(self, artifact: SDKMappingArtifact, operation_name: str) -> list[ResolvedCondition]:
        mapping = artifact.mapping
        matches = [item for item in mapping.get("operations", []) if isinstance(item, dict) and item.get("name") == operation_name]
        if len(matches) != 1:
            raise RuntimeConditionsError(f"{artifact.name}: expected one SDK operation {operation_name}, found {len(matches)}")
        conditions = matches[0].get("conditions")
        if not isinstance(conditions, list) or not conditions:
            raise RuntimeConditionsError(f"{artifact.name}: SDK operation {operation_name} has no static conditions")
        resolved: list[ResolvedCondition] = []
        for item in conditions:
            if not isinstance(item, dict) or not isinstance(item.get("operation"), dict) or not isinstance(item.get("kind"), str) or not isinstance(item.get("interfaceType"), str):
                raise RuntimeConditionsError(f"{artifact.name}: SDK operation {operation_name} contains an invalid condition")
            resolved.append(ResolvedCondition(mapping["extension"]["id"], item["kind"], item["interfaceType"], copy.deepcopy(item["operation"])))
        extension = self._extension_for(mapping)
        for item in resolved:
            self._validate_condition(extension, item)
        return resolved

    def resolve_stateful_operation(self, artifact: SDKMappingArtifact, operation_name: str, state: dict[str, Any], scope: str) -> list[ResolvedCondition]:
        mapping = artifact.mapping
        matches = [item for item in mapping.get("operations", []) if isinstance(item, dict) and item.get("name") == operation_name]
        if len(matches) != 1:
            raise RuntimeConditionsError(f"{artifact.name}: expected one stateful SDK operation {operation_name}, found {len(matches)}")
        template = matches[0].get("conditionTemplate")
        if not isinstance(template, dict) or not isinstance(template.get("operation"), dict):
            raise RuntimeConditionsError(f"{artifact.name}: stateful SDK operation {operation_name} has no condition template")
        operation = copy.deepcopy(template["operation"])
        bindings = template.get("stateBindings")
        if not isinstance(bindings, dict) or not bindings:
            raise RuntimeConditionsError(f"{artifact.name}: stateful SDK operation {operation_name} has no state bindings")
        for field, state_field in bindings.items():
            value = state.get(state_field) if isinstance(state_field, str) else None
            if not isinstance(value, str):
                return []
            operation[field] = value
        operation["scope"] = scope
        verb = operation.get("verb")
        supported = state.get("operations")
        if not isinstance(verb, str) or not isinstance(supported, list):
            return []
        allowed = any(isinstance(item, dict) and item.get("verb") == verb and scope in item.get("scopes", []) for item in supported)
        if not allowed:
            return []
        kind = template.get("kind")
        interface_type = template.get("interfaceType")
        if not isinstance(kind, str) or not isinstance(interface_type, str):
            raise RuntimeConditionsError(f"{artifact.name}: stateful SDK operation {operation_name} has invalid condition coordinates")
        resolved = ResolvedCondition(mapping["extension"]["id"], kind, interface_type, operation)
        self._validate_condition(self._extension_for(mapping), resolved)
        return [resolved]

    def _resolve_template(self, call: BoundCall, template: dict[str, Any]) -> dict[str, Any] | None:
        operation = copy.deepcopy(template.get("operation", {}))
        if "pathTemplate" in operation:
            path = operation.pop("pathTemplate")
            for variable, binding in template.get("pathVariables", {}).items():
                value = self._string_argument(call, binding)
                if value is None:
                    return None
                path = path.replace("{" + variable + "}", value)
            operation["path"] = path
            return operation
        for field, binding in template.get("operationBindings", {}).items():
            value = self._string_argument(call, binding)
            if value is None:
                return None
            operation[field] = value
        return operation

    def _string_argument(self, call: BoundCall, binding: dict[str, Any]) -> str | None:
        value = call.argument(binding)
        return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None

    def _extension_for(self, mapping: dict[str, Any]) -> SDKExtensionArtifact:
        coordinates = mapping.get("extension", {})
        matches = [item for item in self.extensions if item.id == coordinates.get("id") and item.version == str(coordinates.get("version")) and item.semantic_sha256 == coordinates.get("semanticSha256")]
        if len(matches) != 1:
            raise RuntimeConditionsError(f"SDK mapping requires one exact extension release {coordinates}, found {len(matches)}")
        return matches[0]

    def _validate_condition(self, extension: SDKExtensionArtifact, resolved: ResolvedCondition) -> None:
        schemas = extension.document.get("spec", {}).get("schemas", [])
        matches = [item.get("schema") for item in schemas if isinstance(item, dict) and item.get("appliesToKind") == resolved.kind and item.get("appliesToInterfaceType") == resolved.interface_type]
        if len(matches) != 1 or not isinstance(matches[0], dict):
            raise RuntimeConditionsError(f"extension {extension.id} does not define exactly one schema for {resolved.kind}/{resolved.interface_type}")
        condition = {"kind": resolved.kind, "interface": {"type": resolved.interface_type, "operations": [resolved.operation]}}
        errors = sorted(Draft202012Validator(matches[0]).iter_errors(condition), key=lambda error: list(error.path))
        if errors:
            raise RuntimeConditionsError(f"SDK-resolved condition does not align to extension {extension.id}: {errors[0].message}")


class SDKPythonExtractor:
    def __init__(self, mappings: list[SDKMappingArtifact], extensions: list[SDKExtensionArtifact]) -> None:
        self.mappings = mappings
        self.extensions = extensions

    def extract(self, source_files: list[Path], project_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
        mapping_index = SDKMappingIndex(self.mappings)
        resolver = SDKConditionResolver(self.extensions)
        resolved: list[ResolvedCondition] = []
        for source in source_files:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            visitor = SDKSourceVisitor(mapping_index, resolver)
            visitor.visit(tree)
            for item in visitor.resolved:
                if item not in resolved:
                    resolved.append(item)
        from .composition import SDKCompositionAnalyzer, SDKCompositionRegistry

        composition = SDKCompositionAnalyzer(source_files, project_root, SDKCompositionRegistry(self.mappings, resolver))
        for item in composition.analyze():
            if item not in resolved:
                resolved.append(item)
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for item in resolved:
            operations = grouped.setdefault((item.extension_id, item.kind, item.interface_type), [])
            if item.operation not in operations:
                operations.append(item.operation)
        conditions = [{"kind": kind, "interface": {"type": interface_type, "operations": operations}} for (_, kind, interface_type), operations in grouped.items()]
        dependencies = {item.id: item.definition.dependencies for item in self.extensions}
        extension_ids: set[str] = set()
        for extension_id, _, _ in grouped:
            add_extension_closure(extension_id, dependencies, extension_ids)
        return conditions, sorted(extension_ids)
