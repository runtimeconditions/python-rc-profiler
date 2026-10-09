from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Optional

from ..constants import API_VERSION
from ..extension.identity import parse_identifier, profile_reference
from ..errors import RuntimeConditionsError
from ..manifest.mapping import find_option, simple_name, strip_package_class
from ..models import DiscoveryResult, ProfileOptions, SymbolMapping
from ..project import ProjectDiscovery
from ..sdk.direct import DirectSDKPythonExtractor
from ..source.python import PythonSourceIndex, expression_name, literal_string, workload_source_files
from ..util import add_extension_closure, add_unique, deep_copy, diagnostics_text
from .binding import BindingArtifact
from .imports import ImportIndex
from .validator import ProfileValidator


class ProfileExtractor:
    def extract(self, project_root: Path, options: ProfileOptions) -> dict[str, Any]:
        discovery = ProjectDiscovery().discover(project_root, options.discovery_options)
        if discovery.has_errors:
            raise RuntimeConditionsError("Runtime Conditions artifact validation failed: " + diagnostics_text(discovery.diagnostics))
        bindings = [
            BindingArtifact(artifact)
            for artifact in discovery.validated_artifacts
            if artifact.artifact.kind == "binding" and artifact.manifest is not None
        ]
        source_files = workload_source_files(discovery.project_root)
        if not source_files:
            raise RuntimeConditionsError(f"no Python source files found under {discovery.project_root}")
        if bindings:
            extractor = PythonExtractionScanner(bindings, discovery, options)
            extractor.collect(source_files)
            extractor.extract(source_files)
            profile = extractor.profile()
        else:
            workload: dict[str, Any] = {}
            if options.workload_uri:
                workload["uri"] = options.workload_uri
            if options.workload_version:
                workload["version"] = options.workload_version
            profile = {"apiVersion": API_VERSION, "kind": "RuntimeConditionsProfile", "metadata": {"name": options.name}, "workload": workload, "extensions": [], "conditions": []}
        sdk_conditions: list[dict[str, Any]] = []
        sdk_extensions: list[tuple[str, str]] = []
        if discovery.sdk_mappings:
            direct_conditions, direct_extensions = DirectSDKPythonExtractor(
                discovery.sdk_mappings, discovery.sdk_extensions
            ).extract(source_files, discovery.project_root)
            sdk_conditions.extend(direct_conditions)
            sdk_extensions.extend(direct_extensions)
        for extension in sdk_extensions:
            add_unique(profile["extensions"], profile_reference(extension))
        for condition in sdk_conditions:
            if condition not in profile["conditions"]:
                profile["conditions"].append(condition)
        profile["extensions"].sort(key=parse_identifier)
        profile_diagnostics = ProfileValidator().validate(profile, discovery)
        if profile_diagnostics:
            raise RuntimeConditionsError(
                "generated Runtime Conditions Profile validation failed: "
                + diagnostics_text(profile_diagnostics)
            )
        return profile


class PythonExtractionScanner:
    def __init__(self, bindings: list[BindingArtifact], discovery: DiscoveryResult, options: ProfileOptions) -> None:
        self.bindings = bindings
        self.discovery = discovery
        self.options = options
        self.binding_by_package: dict[str, BindingArtifact] = {}
        self.binding_by_class: dict[str, BindingArtifact] = {}
        for binding in bindings:
            self.binding_by_package[binding.manifest.package] = binding
            for mapping in binding.all_mappings():
                self.binding_by_class[mapping.class_name] = binding
                self.binding_by_class[f"{binding.manifest.package}.{mapping.class_name}"] = binding
        self.used_extensions: list[tuple[str, str]] = []
        self.conditions: list[dict[str, Any]] = []
        self.source_index = PythonSourceIndex()

    def collect(self, source_files: list[Path]) -> None:
        self.source_index = PythonSourceIndex.from_paths(source_files)

    def extract(self, source_files: list[Path]) -> None:
        for path in source_files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports = ImportIndex.from_module(tree, self.bindings)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                identity = imports.call_identity(node.func)
                if identity is None:
                    continue
                binding = self.binding_for(identity.class_name)
                if binding is None:
                    continue
                declaration = binding.find_declaration(identity.class_name, identity.member_name)
                if declaration is None:
                    continue
                self.conditions.append(self.parse_condition(binding, declaration, node, imports))
                add_unique(self.used_extensions, binding.extension_id)

    def profile(self) -> dict[str, Any]:
        workload: dict[str, Any] = {}
        if self.options.workload_uri:
            workload["uri"] = self.options.workload_uri
        if self.options.workload_version:
            workload["version"] = self.options.workload_version
        return {
            "apiVersion": API_VERSION,
            "kind": "RuntimeConditionsProfile",
            "metadata": {"name": self.options.name},
            "workload": workload,
            "extensions": self.extension_closure(),
            "conditions": self.conditions,
        }

    def parse_condition(
        self,
        binding: BindingArtifact,
        declaration: SymbolMapping,
        call: ast.Call,
        imports: ImportIndex,
    ) -> dict[str, Any]:
        if declaration.writes:
            condition: dict[str, Any] = {}
            self.apply_generic_writes(condition, binding, declaration, call, imports)
            for argument in call.args:
                if isinstance(argument, ast.Call):
                    self.apply_generic_option(condition, binding, declaration.options, argument, imports)
            return condition
        name = ""
        if declaration.name_arg is not None:
            name = self.string_arg(call.args, declaration.name_arg, declaration.member_name, "name", binding, imports)
        condition: dict[str, Any] = {
            "name": name,
            "kind": declaration.kind,
            "interface": {"type": declaration.interface_type or ""},
        }
        for index, arg in enumerate(call.args):
            if declaration.name_arg is not None and index == declaration.name_arg:
                continue
            if not isinstance(arg, ast.Call):
                continue
            match = self.condition_option_for_call(binding, declaration, condition, arg, imports)
            if match is None:
                continue
            option_binding, option = match
            self.apply_option(condition, option_binding, option, arg, imports)
            add_unique(self.used_extensions, option_binding.extension_id)
        remove_empty_configuration(condition)
        return condition

    def apply_generic_option(
        self,
        condition: dict[str, Any],
        binding: BindingArtifact,
        options: list[SymbolMapping],
        call: ast.Call,
        imports: ImportIndex,
    ) -> None:
        identity = imports.call_identity(call.func)
        if identity is None:
            raise RuntimeConditionsError("generated binding option must be a directly imported call")
        option = next(
            (
                item
                for item in options
                if item.member_name == identity.member_name
                and (identity.class_name == item.class_name or identity.class_name.endswith(f".{item.class_name}"))
            ),
            None,
        )
        if option is None:
            raise RuntimeConditionsError(f"{identity.class_name}.{identity.member_name} is not valid in this binding position")
        self.apply_generic_writes(condition, binding, option, call, imports)
        for argument in call.args:
            if isinstance(argument, ast.Call):
                self.apply_generic_option(condition, binding, option.options, argument, imports)

    def apply_generic_writes(
        self,
        condition: dict[str, Any],
        binding: BindingArtifact,
        mapping: SymbolMapping,
        call: ast.Call,
        imports: ImportIndex,
    ) -> None:
        for write in mapping.writes:
            if "value" in write:
                values = [deep_copy(write["value"])]
            elif "values" in write and isinstance(write["values"], list):
                values = [deep_copy(item) for item in write["values"]]
            else:
                argument = write.get("argument", {})
                position = argument.get("position") if isinstance(argument, dict) else None
                if not isinstance(position, int) or position >= len(call.args):
                    raise RuntimeConditionsError(f"{mapping.member_name} requires argument {position}")
                expression = call.args[position]
                if isinstance(expression, ast.Constant):
                    values = [deep_copy(expression.value)]
                else:
                    values = [self.binding_value(expression, binding, imports)]
            for value in values:
                self.write_generic_target(condition, write["target"], value)

    def write_generic_target(self, condition: dict[str, Any], target: str, value: Any) -> None:
        current = condition
        parts = target.split(".")
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        leaf = parts[-1]
        if leaf.endswith("[]"):
            current.setdefault(leaf[:-2], []).append(value)
        else:
            current[leaf] = value

    def condition_option_for_call(
        self,
        declaration_binding: BindingArtifact,
        declaration: SymbolMapping,
        condition: dict[str, Any],
        call: ast.Call,
        imports: ImportIndex,
    ) -> Optional[tuple[BindingArtifact, SymbolMapping]]:
        identity = imports.call_identity(call.func)
        if identity is None:
            return None
        binding = self.binding_for(identity.class_name)
        if binding is None:
            return None
        if binding is declaration_binding:
            option = find_option(declaration.options, identity.class_name, identity.member_name)
            if option is not None:
                return binding, option
        option = binding.find_root_option(identity.class_name, identity.member_name, condition)
        if option is None:
            return None
        return binding, option

    def apply_option(
        self,
        condition: dict[str, Any],
        binding: BindingArtifact,
        option: SymbolMapping,
        call: ast.Call,
        imports: ImportIndex,
    ) -> None:
        if option.target == "interface.spec":
            spec = {
                "format": self.string_arg(call.args, option.string_args.get("format"), option.member_name, "format", binding, imports),
                "uri": self.string_arg(call.args, option.string_args.get("uri"), option.member_name, "uri", binding, imports),
            }
            version = self.string_arg(call.args, option.string_args.get("version"), option.member_name, "version", binding, imports)
            if version:
                spec["version"] = version
            condition["interface"]["spec"] = spec
        elif option.target == "interface.operations[]":
            operation: dict[str, Any] = {
                "method": option.method,
                "path": self.string_arg(call.args, option.string_args.get("path"), option.member_name, "path", binding, imports),
            }
            for arg in call.args:
                if not isinstance(arg, ast.Call):
                    continue
                match = self.nested_option_for_call(binding, option.options, arg, imports)
                if match is None:
                    continue
                _, nested = match
                if nested.class_arg is None or nested.class_arg >= len(arg.args):
                    raise RuntimeConditionsError(f"{nested.member_name} requires classArg in the binding manifest")
                schema = self.schema_for_class_arg(arg.args[nested.class_arg], imports)
                if nested.target == "requestBodySchema":
                    operation["requestBodySchema"] = schema
                elif nested.target == "responseSchema":
                    operation["responseSchema"] = schema
                else:
                    raise RuntimeConditionsError(f"unsupported operation option target {nested.target}")
            condition["interface"].setdefault("operations", []).append(operation)
        elif option.target == "interface.type":
            condition["interface"]["type"] = option.value
            if option.enum_arg is not None and option.enum_arg < len(call.args):
                condition["interface"]["engine"] = self.binding_value(call.args[option.enum_arg], binding, imports)
        elif option.target == "configuration.env[]":
            configuration = condition.setdefault("configuration", {})
            if "alternatives" in configuration:
                raise RuntimeConditionsError(f"{option.member_name} cannot be combined with configuration alternatives")
            configuration.setdefault("env", []).append(self.env_input(binding, option, call, imports))
        elif option.target == "configuration.alternatives[]":
            configuration = condition.setdefault("configuration", {})
            if "env" in configuration:
                raise RuntimeConditionsError(f"{option.member_name} cannot be combined with configuration env")
            env: list[dict[str, Any]] = []
            for arg in call.args:
                if not isinstance(arg, ast.Call):
                    raise RuntimeConditionsError(f"{option.member_name} arguments must be nested env calls")
                match = self.nested_option_for_call(binding, option.options, arg, imports)
                if match is None:
                    raise RuntimeConditionsError(f"{option.member_name} arguments must match nested option calls")
                nested_binding, nested = match
                env.append(self.env_input(nested_binding, nested, arg, imports))
            configuration.setdefault("alternatives", []).append({"env": env})
        else:
            raise RuntimeConditionsError(f"unsupported Python binding target {option.target}")

    def env_input(
        self,
        binding: BindingArtifact,
        option: SymbolMapping,
        call: ast.Call,
        imports: ImportIndex,
    ) -> dict[str, Any]:
        env = {
            "property": self.string_arg(call.args, option.string_args.get("property"), option.member_name, "property", binding, imports),
            "name": self.string_arg(call.args, option.string_args.get("name"), option.member_name, "name", binding, imports),
        }
        for arg in call.args:
            if not isinstance(arg, ast.Call):
                continue
            match = self.nested_option_for_call(binding, option.options, arg, imports)
            if match is None:
                continue
            _, nested = match
            value = nested.value.lower() == "true"
            if nested.target == "env.sensitive":
                if value:
                    env["sensitive"] = True
            elif nested.target == "env.required":
                env["required"] = value
            else:
                raise RuntimeConditionsError(f"unsupported env input option target {nested.target}")
        return env

    def nested_option_for_call(
        self,
        expected_binding: BindingArtifact,
        options: list[SymbolMapping],
        call: ast.Call,
        imports: ImportIndex,
    ) -> Optional[tuple[BindingArtifact, SymbolMapping]]:
        identity = imports.call_identity(call.func)
        if identity is None:
            return None
        binding = self.binding_for(identity.class_name)
        if binding is not expected_binding:
            return None
        option = find_option(options, identity.class_name, identity.member_name)
        if option is None:
            return None
        return binding, option

    def binding_for(self, class_name: str) -> Optional[BindingArtifact]:
        return self.binding_by_class.get(class_name) or self.binding_by_class.get(simple_name(class_name))

    def string_arg(
        self,
        args: list[ast.expr],
        index: Optional[int],
        function: str,
        name: str,
        binding: BindingArtifact,
        imports: ImportIndex,
    ) -> str:
        if index is None or index >= len(args):
            raise RuntimeConditionsError(f"{function} requires {name} argument")
        value = self.string_value(args[index], binding, imports)
        if value is None:
            raise RuntimeConditionsError(f"{function} {name} must be a string literal or string constant")
        return value

    def string_value(self, expr: ast.expr, binding: BindingArtifact, imports: ImportIndex) -> Optional[str]:
        value = literal_string(expr)
        if value is not None:
            return value
        name = expression_name(expr)
        if name is None:
            return None
        normalized = imports.normalize_name(name)
        return (
            self.source_index.string_constants.get(normalized)
            or self.source_index.string_constants.get(simple_name(normalized))
            or binding.manifest.constants.get(normalized)
            or binding.manifest.constants.get(strip_package_class(normalized, binding.manifest.package))
        )

    def binding_value(self, expr: ast.expr, binding: BindingArtifact, imports: ImportIndex) -> str:
        value = self.string_value(expr, binding, imports)
        if value is not None:
            return value
        name = expression_name(expr)
        if name is not None:
            normalized = imports.normalize_name(name)
            value = binding.manifest.constants.get(normalized) or binding.manifest.constants.get(
                strip_package_class(normalized, binding.manifest.package)
            )
            if value is not None:
                return value
        raise RuntimeConditionsError("value must be a string literal, string constant, or binding constant")

    def schema_for_class_arg(self, expr: ast.expr, imports: ImportIndex) -> Any:
        name = expression_name(expr)
        if name is None:
            raise RuntimeConditionsError("schema option requires a class/type argument")
        normalized = imports.normalize_name(name)
        schema = (
            self.source_index.schemas.get(normalized)
            or self.source_index.schemas.get(simple_name(normalized))
        )
        if schema is None:
            raise RuntimeConditionsError(f"unsupported schema class {normalized}")
        return deep_copy(schema)

    def extension_closure(self) -> list[dict[str, str]]:
        dependencies: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for artifact in self.discovery.validated_artifacts:
            if artifact.extension_id:
                dependencies[artifact.reference] = artifact.dependencies
        resolved: set[tuple[str, str]] = set()
        for extension in self.used_extensions:
            add_extension_closure(extension, dependencies, resolved)
        return [profile_reference(ref) for ref in sorted(resolved)]


def remove_empty_configuration(condition: dict[str, Any]) -> None:
    configuration = condition.get("configuration")
    if isinstance(configuration, dict) and not configuration:
        condition.pop("configuration")
