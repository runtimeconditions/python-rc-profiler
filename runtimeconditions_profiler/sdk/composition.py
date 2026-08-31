from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import RuntimeConditionsError
from ..models import SDKMappingArtifact
from .python import ResolvedCondition, SDKConditionResolver


MappingKey = tuple[str, str]


@dataclass(frozen=True)
class UnknownValue:
    pass


UNKNOWN = UnknownValue()


@dataclass(frozen=True)
class LiteralValue:
    value: Any


@dataclass(frozen=True)
class ExternalSymbol:
    name: str


@dataclass(frozen=True)
class ExternalInstance:
    type_name: str


@dataclass(frozen=True)
class FunctionValue:
    name: str


@dataclass(frozen=True)
class ClassValue:
    name: str


@dataclass(frozen=True)
class AppInstance:
    class_name: str
    fields: tuple[tuple[str, Any], ...] = ()

    def field(self, name: str) -> Any:
        return dict(self.fields).get(name, UNKNOWN)


@dataclass(frozen=True)
class BoundAppMethod:
    instance: AppInstance
    function_name: str


@dataclass(frozen=True)
class SDKClientValue:
    mapping: MappingKey
    factory_owner: MappingKey


@dataclass(frozen=True)
class BoundSDKClientMethod:
    client: SDKClientValue
    method: str


@dataclass(frozen=True)
class SDKResourceValue:
    mapping: MappingKey
    resource: str


@dataclass(frozen=True)
class BoundSDKResourceMember:
    resource: SDKResourceValue
    member: str


@dataclass(frozen=True)
class SDKStatefulFlowValue:
    mapping: MappingKey
    flow_id: str
    member_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoundSDKStatefulProducer:
    flow: SDKStatefulFlowValue


@dataclass(frozen=True)
class SDKStatefulResourceValue:
    mapping: MappingKey
    flow_id: str
    state: tuple[tuple[str, Any], ...]

    def field(self, name: str) -> Any:
        return dict(self.state).get(name, UNKNOWN)


@dataclass(frozen=True)
class BoundSDKStatefulResourceMethod:
    resource: SDKStatefulResourceValue
    method: str


@dataclass
class ModuleInfo:
    name: str
    path: Path
    tree: ast.Module
    imports: dict[str, str] = field(default_factory=dict)
    literals: dict[str, LiteralValue] = field(default_factory=dict)
    runtime_values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FunctionInfo:
    name: str
    module: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    class_name: str | None = None


@dataclass(frozen=True)
class ClassInfo:
    name: str
    module: str
    node: ast.ClassDef


@dataclass
class Frame:
    module: ModuleInfo
    values: dict[str, Any]
    self_fields: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExecutionResult:
    value: Any
    instance: AppInstance | None = None


class SDKCompositionRegistry:
    def __init__(self, artifacts: list[SDKMappingArtifact], resolver: SDKConditionResolver) -> None:
        self.artifacts: dict[MappingKey, SDKMappingArtifact] = {}
        self.resolver = resolver
        self.aliases: dict[str, str] = {}
        self.client_factories: list[tuple[SDKMappingArtifact, dict[str, Any], str]] = []
        self.resource_factories: list[tuple[SDKMappingArtifact, dict[str, Any], str]] = []
        self.stateful_factories: list[tuple[SDKMappingArtifact, dict[str, Any], str]] = []
        for artifact in artifacts:
            key = (artifact.distribution, artifact.name)
            previous = self.artifacts.get(key)
            if previous is not None and (previous.distribution_version != artifact.distribution_version or previous.mapping_sha256 != artifact.mapping_sha256):
                raise RuntimeConditionsError(f"ambiguous SDK mapping versions for {key[0]}/{key[1]}")
            self.artifacts[key] = artifact
            python = artifact.mapping.get("python", {})
            if not isinstance(python, dict):
                continue
            for alias in python.get("aliases", []):
                if isinstance(alias, dict) and isinstance(alias.get("symbol"), str) and isinstance(alias.get("target"), str):
                    previous_alias = self.aliases.get(alias["symbol"])
                    if previous_alias is not None and previous_alias != alias["target"]:
                        raise RuntimeConditionsError(f"conflicting SDK aliases for {alias['symbol']}")
                    self.aliases[alias["symbol"]] = alias["target"]
            for factory in python.get("clientFactories", []):
                if isinstance(factory, dict):
                    for symbol in factory.get("symbols", []):
                        if isinstance(symbol, str):
                            self.client_factories.append((artifact, factory, symbol))
            for factory in python.get("resourceFactories", []):
                if isinstance(factory, dict):
                    for symbol in factory.get("symbols", []):
                        if isinstance(symbol, str):
                            self.resource_factories.append((artifact, factory, symbol))
            for flow in python.get("statefulResourceFlows", []):
                if not isinstance(flow, dict) or not isinstance(flow.get("id"), str):
                    raise RuntimeConditionsError(f"{artifact.name}: invalid stateful resource flow")
                for symbol in flow.get("constructorSymbols", []):
                    if not isinstance(symbol, str) or not symbol:
                        raise RuntimeConditionsError(f"{artifact.name}: invalid stateful resource constructor symbol")
                    self.stateful_factories.append((artifact, flow, symbol))

    def canonical_symbol(self, name: str) -> str:
        seen: set[str] = set()
        while name in self.aliases and name not in seen:
            seen.add(name)
            name = self.aliases[name]
        return name

    def factory(self, symbol: str, positional: list[Any], keywords: dict[str, Any]) -> Any:
        canonical = self.canonical_symbol(symbol)
        matched_symbol = False
        for artifact, factory, declared_symbol in self.client_factories:
            if self.canonical_symbol(declared_symbol) != canonical:
                continue
            matched_symbol = True
            if not self._service_matches(factory, positional, keywords):
                continue
            self._ensure_dependencies(artifact)
            produced = factory.get("produces", {})
            target = self._mapping_key(produced, artifact)
            self._artifact(target)
            return SDKClientValue(target, (artifact.distribution, artifact.name))
        for artifact, factory, declared_symbol in self.resource_factories:
            if self.canonical_symbol(declared_symbol) != canonical:
                continue
            matched_symbol = True
            if not self._service_matches(factory, positional, keywords):
                continue
            self._ensure_dependencies(artifact)
            produced = factory.get("produces", {})
            target = self._mapping_key(produced, artifact)
            self._artifact(target)
            resource = produced.get("resource")
            if not isinstance(resource, str) or not resource:
                raise RuntimeConditionsError(f"{artifact.name}: resource factory has no produced resource")
            return SDKResourceValue(target, resource)
        for artifact, flow, declared_symbol in self.stateful_factories:
            if self.canonical_symbol(declared_symbol) != canonical:
                continue
            matched_symbol = True
            self._ensure_dependencies(artifact)
            return SDKStatefulFlowValue((artifact.distribution, artifact.name), flow["id"])
        return UNKNOWN if matched_symbol else None

    def stateful_member(self, value: SDKStatefulFlowValue, member: str) -> Any:
        artifact, flow = self._stateful_flow(value)
        producer = flow.get("producer", {})
        member_path = producer.get("memberPath")
        if not isinstance(member_path, list) or any(not isinstance(item, str) or not item for item in member_path):
            raise RuntimeConditionsError(f"{artifact.name}: {flow.get('id')} has an invalid producer member path")
        next_path = (*value.member_path, member)
        expected = tuple(member_path)
        if next_path == expected:
            return BoundSDKStatefulProducer(value)
        if expected[: len(next_path)] == next_path:
            return SDKStatefulFlowValue(value.mapping, value.flow_id, next_path)
        return UNKNOWN

    def produce_stateful_resource(self, value: SDKStatefulFlowValue, positional: list[Any], keywords: dict[str, Any]) -> Any:
        artifact, flow = self._stateful_flow(value)
        producer = flow.get("producer", {})
        bindings = producer.get("selectorArguments", {})
        if not isinstance(bindings, dict):
            raise RuntimeConditionsError(f"{artifact.name}: {flow.get('id')} has invalid selector bindings")
        selected = {field: self._literal_string(self._argument(binding, positional, keywords)) for field, binding in bindings.items()}
        api_version = selected.get("apiVersion")
        kind = selected.get("kind")
        group = selected.get("group")
        if not isinstance(api_version, str) or not isinstance(kind, str):
            return UNKNOWN
        if group is None and "/" in api_version:
            group, api_version = api_version.split("/", 1)
        catalog = producer.get("catalog")
        if not isinstance(catalog, list):
            raise RuntimeConditionsError(f"{artifact.name}: {flow.get('id')} has no resource catalog")
        candidates = []
        for case in catalog:
            selector = case.get("selector", {}) if isinstance(case, dict) else {}
            if selector.get("apiVersion") != api_version or selector.get("kind") != kind:
                continue
            if group is not None and selector.get("apiGroup") != group:
                continue
            candidates.append(case)
        if len(candidates) != 1 or not isinstance(candidates[0].get("state"), dict):
            return UNKNOWN
        return SDKStatefulResourceValue(value.mapping, value.flow_id, tuple(sorted(candidates[0]["state"].items())))

    def resolve_stateful_resource_method(self, value: SDKStatefulResourceValue, method_name: str, positional: list[Any], keywords: dict[str, Any]) -> tuple[list[ResolvedCondition], Any]:
        artifact, flow = self._stateful_flow(value)
        methods = [item for item in flow.get("methods", []) if isinstance(item, dict) and item.get("method") == method_name]
        if len(methods) != 1:
            return [], UNKNOWN
        method = methods[0]
        branches = method.get("operations")
        if not isinstance(branches, list) or not branches:
            raise RuntimeConditionsError(f"{artifact.name}: {flow.get('id')}.{method_name} has no operations")
        selected = []
        for branch in branches:
            when = branch.get("when") if isinstance(branch, dict) else None
            if isinstance(when, dict):
                if self._stateful_branch_matches(when, positional, keywords):
                    selected.append(branch)
            elif branch.get("otherwise") is True:
                selected.append(branch)
            else:
                selected.append(branch)
            if selected:
                break
        if len(selected) != 1:
            return [], UNKNOWN
        reference = selected[0].get("operationRef")
        target = self._artifact(self._mapping_key(reference, artifact))
        operation_name = reference.get("operation") if isinstance(reference, dict) else None
        if not isinstance(operation_name, str):
            raise RuntimeConditionsError(f"{artifact.name}: {flow.get('id')}.{method_name} has an invalid operation reference")
        state = dict(value.state)
        template = next((item.get("conditionTemplate") for item in target.mapping.get("operations", []) if isinstance(item, dict) and item.get("name") == operation_name), None)
        if not isinstance(template, dict):
            raise RuntimeConditionsError(f"{target.name}: stateful operation {operation_name} has no template")
        scope = self._resolve_stateful_scope(template, state, method.get("scopeArgument"), positional, keywords)
        if scope is None:
            return [], UNKNOWN
        return self.resolver.resolve_stateful_operation(target, operation_name, state, scope), UNKNOWN

    def client_method(self, value: SDKClientValue, method: str) -> tuple[list[ResolvedCondition], Any]:
        artifact = self._artifact(value.mapping)
        client = artifact.mapping.get("python", {}).get("client", {})
        if isinstance(client, dict):
            matches = [item for item in client.get("methods", []) if isinstance(item, dict) and item.get("method") == method]
            if len(matches) > 1:
                raise RuntimeConditionsError(f"{artifact.name}: duplicate client method {method}")
            if matches:
                operation = matches[0].get("operation")
                if not isinstance(operation, str):
                    raise RuntimeConditionsError(f"{artifact.name}: client method {method} has no operation")
                return self.resolver.resolve_static_operation(artifact, operation), UNKNOWN
        owner = self._artifact(value.factory_owner)
        wrappers = owner.mapping.get("python", {}).get("clientWrappers", [])
        matches = [item for item in wrappers if isinstance(item, dict) and item.get("method") == method]
        if len(matches) > 1:
            raise RuntimeConditionsError(f"{owner.name}: duplicate client wrapper {method}")
        if matches:
            return self.resolve_call(matches[0].get("callRef"), owner), UNKNOWN
        return [], UNKNOWN

    def resource_member(self, value: SDKResourceValue, member: str) -> tuple[str, Any] | None:
        artifact = self._artifact(value.mapping)
        record = self._resource(artifact, value.resource)
        for key, kind in (("actions", "operation"), ("waiters", "waiter"), ("wrappers", "call"), ("relations", "resource")):
            matches = [item for item in record.get(key, []) if isinstance(item, dict) and item.get("method", item.get("member")) == member]
            if len(matches) > 1:
                raise RuntimeConditionsError(f"{artifact.name}: duplicate {value.resource}.{member}")
            if matches:
                return kind, matches[0]
        return None

    def resolve_resource_member(self, value: SDKResourceValue, member: str) -> tuple[list[ResolvedCondition], Any]:
        artifact = self._artifact(value.mapping)
        found = self.resource_member(value, member)
        if found is None:
            return [], UNKNOWN
        kind, record = found
        if kind == "operation":
            return self.resolve_operation_ref(record.get("operationRef"), artifact), UNKNOWN
        if kind == "call":
            return self.resolve_call(record.get("callRef"), artifact), UNKNOWN
        if kind == "resource":
            target = record.get("resource")
            if not isinstance(target, str) or not target:
                raise RuntimeConditionsError(f"{artifact.name}: relation {value.resource}.{member} has no resource")
            return [], SDKResourceValue(value.mapping, target)
        if kind == "waiter":
            return self.resolve_waiter_ref(record.get("waiterRef"), artifact), UNKNOWN
        return [], UNKNOWN

    def resolve_operation_ref(self, reference: Any, owner: SDKMappingArtifact) -> list[ResolvedCondition]:
        if not isinstance(reference, dict):
            raise RuntimeConditionsError(f"{owner.name}: invalid operationRef")
        artifact = self._artifact(self._mapping_key(reference, owner))
        operation = reference.get("operation")
        if not isinstance(operation, str) or not operation:
            raise RuntimeConditionsError(f"{owner.name}: operationRef has no operation")
        return self.resolver.resolve_static_operation(artifact, operation)

    def resolve_waiter_ref(self, reference: Any, owner: SDKMappingArtifact) -> list[ResolvedCondition]:
        if not isinstance(reference, dict):
            raise RuntimeConditionsError(f"{owner.name}: invalid waiterRef")
        artifact = self._artifact(self._mapping_key(reference, owner))
        waiter_name = reference.get("waiter")
        client = artifact.mapping.get("python", {}).get("client", {})
        factory = client.get("waiterFactory", {}) if isinstance(client, dict) else {}
        matches = [item for item in factory.get("items", []) if isinstance(item, dict) and item.get("name") == waiter_name]
        if len(matches) != 1 or not isinstance(matches[0].get("operation"), str):
            raise RuntimeConditionsError(f"{artifact.name}: unresolved waiter {waiter_name}")
        return self.resolver.resolve_static_operation(artifact, matches[0]["operation"])

    def resolve_call(self, reference: Any, owner: SDKMappingArtifact) -> list[ResolvedCondition]:
        if not isinstance(reference, dict):
            raise RuntimeConditionsError(f"{owner.name}: invalid callRef")
        artifact = self._artifact(self._mapping_key(reference, owner))
        call_name = reference.get("call")
        calls = artifact.mapping.get("python", {}).get("calls", [])
        matches = [item for item in calls if isinstance(item, dict) and item.get("name") == call_name]
        if len(matches) != 1:
            raise RuntimeConditionsError(f"{artifact.name}: unresolved call {call_name}")
        conditions: list[ResolvedCondition] = []
        for operation_ref in self._operation_refs(matches[0]):
            for condition in self.resolve_operation_ref(operation_ref, artifact):
                if condition not in conditions:
                    conditions.append(condition)
        return conditions

    def _operation_refs(self, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [reference for item in value for reference in self._operation_refs(item)]
        if not isinstance(value, dict):
            return []
        result = [value["operationRef"]] if isinstance(value.get("operationRef"), dict) else []
        for key, item in value.items():
            if key != "operationRef":
                result.extend(self._operation_refs(item))
        return result

    def _service_matches(self, factory: dict[str, Any], positional: list[Any], keywords: dict[str, Any]) -> bool:
        selector = factory.get("serviceSelector", {})
        value = self._argument(selector, positional, keywords)
        return isinstance(value, LiteralValue) and value.value == factory.get("service")

    def _argument(self, binding: Any, positional: list[Any], keywords: dict[str, Any]) -> Any:
        if not isinstance(binding, dict):
            return UNKNOWN
        position = binding.get("position")
        if isinstance(position, int) and position < len(positional):
            return positional[position]
        keyword = binding.get("keyword")
        return keywords.get(keyword, UNKNOWN) if isinstance(keyword, str) else UNKNOWN

    def _argument_provided(self, binding: Any, positional: list[Any], keywords: dict[str, Any]) -> bool:
        if not isinstance(binding, dict):
            return False
        position = binding.get("position")
        if isinstance(position, int) and position < len(positional):
            value = positional[position]
            return not (isinstance(value, LiteralValue) and not value.value)
        keyword = binding.get("keyword")
        if not isinstance(keyword, str) or keyword not in keywords:
            return False
        value = keywords[keyword]
        return not (isinstance(value, LiteralValue) and not value.value)

    def _stateful_branch_matches(self, when: dict[str, Any], positional: list[Any], keywords: dict[str, Any]) -> bool:
        if isinstance(when.get("argument"), dict):
            return self._argument_provided(when["argument"], positional, keywords) == bool(when.get("provided"))
        alternatives = when.get("any")
        if isinstance(alternatives, list) and alternatives:
            return any(
                isinstance(item, dict)
                and isinstance(item.get("argument"), dict)
                and self._argument_provided(item["argument"], positional, keywords) == bool(item.get("provided"))
                for item in alternatives
            )
        return False

    def _literal_string(self, value: Any) -> str | None:
        return value.value if isinstance(value, LiteralValue) and isinstance(value.value, str) else None

    def _resolve_stateful_scope(self, template: dict[str, Any], state: dict[str, Any], argument: Any, positional: list[Any], keywords: dict[str, Any]) -> str | None:
        resolution = template.get("scopeResolution")
        if not isinstance(resolution, dict) or not isinstance(resolution.get("stateField"), str):
            raise RuntimeConditionsError("stateful operation has no declarative scope resolution")
        state_value = state.get(resolution["stateField"], UNKNOWN)
        cases = resolution.get("cases")
        if not isinstance(cases, list) or not cases:
            raise RuntimeConditionsError("stateful operation has no scope resolution cases")
        matches = [case for case in cases if isinstance(case, dict) and case.get("equals", UNKNOWN) == state_value]
        if len(matches) != 1:
            return None
        case = matches[0]
        if isinstance(case.get("value"), str):
            return case["value"]
        provided = case.get("argumentProvided")
        if not isinstance(provided, dict):
            raise RuntimeConditionsError("stateful operation scope case has no result")
        key = "provided" if self._argument_provided(argument, positional, keywords) else "omitted"
        return provided.get(key) if isinstance(provided.get(key), str) else None

    def _stateful_flow(self, value: SDKStatefulFlowValue | SDKStatefulResourceValue) -> tuple[SDKMappingArtifact, dict[str, Any]]:
        artifact = self._artifact(value.mapping)
        flows = [item for item in artifact.mapping.get("python", {}).get("statefulResourceFlows", []) if isinstance(item, dict) and item.get("id") == value.flow_id]
        if len(flows) != 1:
            raise RuntimeConditionsError(f"{artifact.name}: unresolved stateful resource flow {value.flow_id}")
        return artifact, flows[0]

    def _mapping_key(self, reference: Any, owner: SDKMappingArtifact) -> MappingKey:
        if not isinstance(reference, dict):
            raise RuntimeConditionsError(f"{owner.name}: invalid mapping reference")
        distribution = reference.get("distribution", owner.distribution)
        mapping = reference.get("mapping")
        if not isinstance(distribution, str) or not isinstance(mapping, str):
            raise RuntimeConditionsError(f"{owner.name}: incomplete mapping reference")
        key = (distribution, mapping)
        owner_key = (owner.distribution, owner.name)
        if key != owner_key:
            declared = {
                (item.get("distribution"), item.get("mapping"))
                for item in owner.mapping.get("dependencies", [])
                if isinstance(item, dict) and item.get("kind") == "sdkMapping"
            }
            if key not in declared:
                raise RuntimeConditionsError(f"{owner.name}: reference to undeclared SDK mapping {key[0]}/{key[1]}")
        return key

    def _ensure_dependencies(self, artifact: SDKMappingArtifact, visiting: set[MappingKey] | None = None) -> None:
        key = (artifact.distribution, artifact.name)
        visiting = set() if visiting is None else visiting
        if key in visiting:
            raise RuntimeConditionsError(f"SDK mapping dependency cycle at {key[0]}/{key[1]}")
        visiting.add(key)
        for dependency in artifact.mapping.get("dependencies", []):
            if not isinstance(dependency, dict) or dependency.get("kind") != "sdkMapping":
                continue
            target = self._mapping_key(dependency, artifact)
            self._ensure_dependencies(self._artifact(target), visiting)
        visiting.remove(key)

    def _artifact(self, key: MappingKey) -> SDKMappingArtifact:
        artifact = self.artifacts.get(key)
        if artifact is None:
            raise RuntimeConditionsError(f"required SDK mapping {key[0]}/{key[1]} was not discovered")
        return artifact

    def _resource(self, artifact: SDKMappingArtifact, name: str) -> dict[str, Any]:
        python = artifact.mapping.get("python", {})
        candidates = []
        service = python.get("serviceResource") if isinstance(python, dict) else None
        if isinstance(service, dict) and service.get("name") == name:
            candidates.append(service)
        if isinstance(python, dict):
            candidates.extend(item for item in python.get("resources", []) if isinstance(item, dict) and item.get("name") == name)
        if len(candidates) != 1:
            raise RuntimeConditionsError(f"{artifact.name}: unresolved resource {name}")
        return candidates[0]


class SDKCompositionAnalyzer:
    def __init__(self, source_files: list[Path], project_root: Path, registry: SDKCompositionRegistry) -> None:
        self.project_root = project_root.resolve()
        self.registry = registry
        self.modules: dict[str, ModuleInfo] = {}
        self.functions: dict[str, FunctionInfo] = {}
        self.classes: dict[str, ClassInfo] = {}
        self.conditions: list[ResolvedCondition] = []
        self.active_calls: set[str] = set()
        for path in source_files:
            self._index_module(path)
        self._index_imports()

    def analyze(self) -> list[ResolvedCondition]:
        for module in self.modules.values():
            frame = Frame(module, {})
            self._exec_block([node for node in module.tree.body if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom))], frame)
            module.runtime_values.update(frame.values)
        for function in self.functions.values():
            if function.class_name is None:
                self._execute(function, [], {})
        return self.conditions

    def _index_module(self, path: Path) -> None:
        module_name = self._module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = ModuleInfo(module_name, path, tree)
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, (str, int, float, bool)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            module.literals[target.id] = LiteralValue(value.value)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{module_name}.{node.name}"
                self.functions[name] = FunctionInfo(name, module_name, node)
            elif isinstance(node, ast.ClassDef):
                class_name = f"{module_name}.{node.name}"
                self.classes[class_name] = ClassInfo(class_name, module_name, node)
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        name = f"{class_name}.{child.name}"
                        self.functions[name] = FunctionInfo(name, module_name, child, class_name)
        self.modules[module_name] = module

    def _index_imports(self) -> None:
        for module in self.modules.values():
            package = module.name if module.path.name == "__init__.py" else module.name.rpartition(".")[0]
            for node in module.tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module.imports[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
                elif isinstance(node, ast.ImportFrom):
                    base = self._import_base(package, node.module or "", node.level)
                    for alias in node.names:
                        if alias.name != "*":
                            module.imports[alias.asname or alias.name] = f"{base}.{alias.name}" if base else alias.name

    def _module_name(self, path: Path) -> str:
        resolved = path.resolve()
        for base in (self.project_root / "src", self.project_root):
            try:
                relative = resolved.relative_to(base.resolve())
            except ValueError:
                continue
            parts = list(relative.parts)
            if parts[-1] == "__init__.py":
                parts.pop()
            else:
                parts[-1] = Path(parts[-1]).stem
            return ".".join(parts)
        raise RuntimeConditionsError(f"Python source {path} is outside project {self.project_root}")

    def _import_base(self, package: str, module: str, level: int) -> str:
        if level == 0:
            return module
        parts = package.split(".") if package else []
        keep = max(0, len(parts) - (level - 1))
        prefix = parts[:keep]
        if module:
            prefix.extend(module.split("."))
        return ".".join(prefix)

    def _execute(self, function: FunctionInfo, positional: list[Any], keywords: dict[str, Any], instance: AppInstance | None = None) -> ExecutionResult:
        if function.name in self.active_calls:
            return ExecutionResult(UNKNOWN, instance)
        self.active_calls.add(function.name)
        try:
            values = self._bind_arguments(function.node, positional, keywords, instance)
            fields = dict(instance.fields) if instance is not None else None
            frame = Frame(self.modules[function.module], values, fields)
            returned, value = self._exec_block(function.node.body, frame)
            updated = AppInstance(instance.class_name, tuple(sorted((frame.self_fields or {}).items()))) if instance is not None else None
            return ExecutionResult(value if returned else UNKNOWN, updated)
        finally:
            self.active_calls.remove(function.name)

    def _bind_arguments(self, node: ast.FunctionDef | ast.AsyncFunctionDef, positional: list[Any], keywords: dict[str, Any], instance: AppInstance | None) -> dict[str, Any]:
        parameters = [*node.args.posonlyargs, *node.args.args]
        values: dict[str, Any] = {}
        offset = 0
        if parameters and parameters[0].arg in {"self", "cls"}:
            values[parameters[0].arg] = instance or UNKNOWN
            offset = 1
        for index, parameter in enumerate(parameters[offset:]):
            values[parameter.arg] = positional[index] if index < len(positional) else keywords.get(parameter.arg, UNKNOWN)
        for parameter in node.args.kwonlyargs:
            values[parameter.arg] = keywords.get(parameter.arg, UNKNOWN)
        return values

    def _exec_block(self, statements: list[ast.stmt], frame: Frame) -> tuple[bool, Any]:
        for statement in statements:
            if isinstance(statement, ast.Return):
                return True, self._eval(statement.value, frame) if statement.value is not None else UNKNOWN
            if isinstance(statement, ast.Assign):
                value = self._eval(statement.value, frame)
                for target in statement.targets:
                    self._assign(target, value, frame)
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                self._assign(statement.target, self._eval(statement.value, frame), frame)
            elif isinstance(statement, ast.Expr):
                self._eval(statement.value, frame)
            elif isinstance(statement, ast.If):
                returned, value = self._exec_block(statement.body, frame)
                if returned:
                    return True, value
                returned, value = self._exec_block(statement.orelse, frame)
                if returned:
                    return True, value
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    self._eval(item.context_expr, frame)
                returned, value = self._exec_block(statement.body, frame)
                if returned:
                    return True, value
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While, ast.Try)):
                bodies = [getattr(statement, "body", []), getattr(statement, "orelse", []), getattr(statement, "finalbody", [])]
                if isinstance(statement, ast.Try):
                    bodies.extend(handler.body for handler in statement.handlers)
                for body in bodies:
                    returned, value = self._exec_block(body, frame)
                    if returned:
                        return True, value
        return False, UNKNOWN

    def _assign(self, target: ast.expr, value: Any, frame: Frame) -> None:
        if isinstance(target, ast.Name):
            frame.values[target.id] = value
        elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self" and frame.self_fields is not None:
            frame.self_fields[target.attr] = value

    def _eval(self, expression: ast.expr | None, frame: Frame) -> Any:
        if expression is None:
            return UNKNOWN
        if isinstance(expression, ast.Constant):
            return LiteralValue(expression.value)
        if isinstance(expression, ast.Name):
            return self._name(expression.id, frame)
        if isinstance(expression, ast.Attribute):
            owner = self._eval(expression.value, frame)
            if isinstance(owner, ExternalSymbol):
                return ExternalSymbol(f"{owner.name}.{expression.attr}")
            if isinstance(owner, ExternalInstance):
                return ExternalSymbol(f"{owner.type_name}.{expression.attr}")
            if isinstance(owner, AppInstance):
                field_value = owner.field(expression.attr)
                if field_value is not UNKNOWN:
                    return field_value
                method = f"{owner.class_name}.{expression.attr}"
                return BoundAppMethod(owner, method) if method in self.functions else UNKNOWN
            if isinstance(owner, SDKClientValue):
                return BoundSDKClientMethod(owner, expression.attr)
            if isinstance(owner, SDKResourceValue):
                return BoundSDKResourceMember(owner, expression.attr)
            if isinstance(owner, SDKStatefulFlowValue):
                return self.registry.stateful_member(owner, expression.attr)
            if isinstance(owner, SDKStatefulResourceValue):
                return BoundSDKStatefulResourceMethod(owner, expression.attr)
            return UNKNOWN
        if isinstance(expression, ast.Call):
            callee = self._eval(expression.func, frame)
            positional = [self._eval(item, frame) for item in expression.args]
            keywords = {item.arg: self._eval(item.value, frame) for item in expression.keywords if item.arg is not None}
            return self._call(callee, positional, keywords)
        if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
            for item in expression.elts:
                self._eval(item, frame)
            return UNKNOWN
        if isinstance(expression, ast.Dict):
            for item in expression.values:
                self._eval(item, frame)
            return UNKNOWN
        if isinstance(expression, ast.Subscript):
            self._eval(expression.value, frame)
            self._eval(expression.slice, frame)
            return UNKNOWN
        if isinstance(expression, ast.BinOp):
            self._eval(expression.left, frame)
            self._eval(expression.right, frame)
            return UNKNOWN
        return UNKNOWN

    def _name(self, name: str, frame: Frame) -> Any:
        if name in frame.values:
            return frame.values[name]
        if name in frame.module.runtime_values:
            return frame.module.runtime_values[name]
        if name in frame.module.literals:
            return frame.module.literals[name]
        target = frame.module.imports.get(name)
        if target:
            return self._qualified(target)
        local_function = f"{frame.module.name}.{name}"
        if local_function in self.functions:
            return FunctionValue(local_function)
        local_class = f"{frame.module.name}.{name}"
        if local_class in self.classes:
            return ClassValue(local_class)
        return ExternalSymbol(name)

    def _qualified(self, name: str) -> Any:
        if name in self.functions:
            return FunctionValue(name)
        if name in self.classes:
            return ClassValue(name)
        module_name, _, member = name.rpartition(".")
        module = self.modules.get(module_name)
        if module:
            if member in module.literals:
                return module.literals[member]
            if member in module.runtime_values:
                return module.runtime_values[member]
        return ExternalSymbol(name)

    def _call(self, callee: Any, positional: list[Any], keywords: dict[str, Any]) -> Any:
        if isinstance(callee, FunctionValue):
            return self._execute(self.functions[callee.name], positional, keywords).value
        if isinstance(callee, ClassValue):
            instance = AppInstance(callee.name)
            initializer = self.functions.get(f"{callee.name}.__init__")
            return self._execute(initializer, positional, keywords, instance).instance if initializer else instance
        if isinstance(callee, BoundAppMethod):
            function = self.functions.get(callee.function_name)
            return self._execute(function, positional, keywords, callee.instance).value if function else UNKNOWN
        if isinstance(callee, ExternalSymbol):
            produced = self.registry.factory(callee.name, positional, keywords)
            if produced is not None:
                return produced
            return ExternalInstance(self.registry.canonical_symbol(callee.name))
        if isinstance(callee, BoundSDKClientMethod):
            conditions, value = self.registry.client_method(callee.client, callee.method)
            self._add(conditions)
            return value
        if isinstance(callee, BoundSDKResourceMember):
            conditions, value = self.registry.resolve_resource_member(callee.resource, callee.member)
            self._add(conditions)
            return value
        if isinstance(callee, BoundSDKStatefulProducer):
            return self.registry.produce_stateful_resource(callee.flow, positional, keywords)
        if isinstance(callee, BoundSDKStatefulResourceMethod):
            conditions, value = self.registry.resolve_stateful_resource_method(callee.resource, callee.method, positional, keywords)
            self._add(conditions)
            return value
        return UNKNOWN

    def _add(self, conditions: list[ResolvedCondition]) -> None:
        for condition in conditions:
            if condition not in self.conditions:
                self.conditions.append(condition)
