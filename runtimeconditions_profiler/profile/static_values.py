"""Small, non-executing Python source evaluator for generated declarations.

Only straight-line bindings and literal construction are interpreted. An
unrelated dynamic expression is harmless until a declaration depends on it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import RuntimeConditionsError
from ..source.python import workload_source_files


@dataclass(frozen=True)
class NativeSymbol:
    package: str
    path: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalModule:
    name: str


@dataclass(frozen=True)
class LocalExport:
    module: str
    name: str


@dataclass(frozen=True)
class Unknown:
    reason: str


@dataclass(frozen=True)
class Rebound:
    name: str
    previous: Any


@dataclass(frozen=True)
class BoundExpr:
    expression: ast.expr
    environment: dict[str, Any]


@dataclass(frozen=True)
class StaticCall:
    callee: Any
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class SourceCall:
    source: Path
    node: ast.Call
    environment: dict[str, Any]
    dynamic: bool = False

    @property
    def location(self) -> str:
        return f"{self.source}:{self.node.lineno}:{self.node.col_offset + 1}"


@dataclass
class _Module:
    name: str
    path: Path
    tree: ast.Module
    is_package: bool
    environment: dict[str, Any]


class StaticProject:
    def __init__(self, root: Path, binding_packages: set[str]) -> None:
        self.root = root.resolve()
        self.binding_packages = binding_packages
        self.modules: dict[str, _Module] = {}
        self.calls: list[SourceCall] = []
        self._functions: list[tuple[_Module, ast.FunctionDef | ast.AsyncFunctionDef, dict[str, Any] | None, bool]] = []
        for path in workload_source_files(self.root):
            relative = path.relative_to(self.root / "src" if path.is_relative_to(self.root / "src") else self.root)
            parts = relative.with_suffix("").parts
            name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
            if name in self.modules:
                raise RuntimeConditionsError(f"ambiguous workload module {name}: {path}")
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, UnicodeError, SyntaxError, RecursionError) as exc:
                raise RuntimeConditionsError(f"cannot parse Python source {path}: {exc}") from exc
            self.modules[name] = _Module(name, path, tree, path.name == "__init__.py", {})
        for module in self.modules.values():
            for stmt in module.tree.body:
                self._scan(module, stmt, module.environment)
        cursor = 0
        while cursor < len(self._functions):
            module, function, captured, dynamic = self._functions[cursor]
            self._scan_function(
                module, function, (captured if captured is not None else module.environment).copy(), dynamic,
            )
            cursor += 1
        self.calls.sort(key=lambda item: (str(item.source), item.node.lineno, item.node.col_offset))

    def _import_target(self, module: _Module, name: str | None, level: int) -> str:
        if not level:
            return name or ""
        package = module.name if module.is_package else module.name.rpartition(".")[0]
        parts = package.split(".") if package else []
        if level > len(parts) + 1:
            return ""
        base = parts[: len(parts) - level + 1]
        return ".".join((*base, *((name or "").split(".") if name else ())))

    def _import_value(self, name: str) -> Any:
        if name in self.modules:
            return LocalModule(name)
        root = name.partition(".")[0]
        if root in self.binding_packages:
            return NativeSymbol(root, tuple(name.split(".")[1:]))
        return Unknown(f"unresolved import {name}")

    def _bind(self, environment: dict[str, Any], name: str, value: Any) -> None:
        if name in environment:
            environment[name] = Rebound(name, environment[name])
        else:
            environment[name] = value

    def _calls_in(self, source: Path, node: ast.AST, environment: dict[str, Any], dynamic: bool = False) -> None:
        dynamic_expressions = (
            ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
            ast.IfExp, ast.BoolOp, ast.Await, ast.Yield, ast.YieldFrom,
        )

        def visit(child: ast.AST, uncertain: bool) -> None:
            uncertain = uncertain or isinstance(child, dynamic_expressions)
            if isinstance(child, ast.Call):
                self.calls.append(SourceCall(source, child, environment.copy(), uncertain))
            for descendant in ast.iter_child_nodes(child):
                visit(descendant, uncertain)

        visit(node, dynamic)

    def _scan(self, module: _Module, stmt: ast.stmt, environment: dict[str, Any], dynamic: bool = False) -> None:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                name = alias.asname or alias.name.split(".")[0]
                target = alias.name if alias.asname else alias.name.split(".")[0]
                self._bind(environment, name, self._import_value(target))
            return
        if isinstance(stmt, ast.ImportFrom):
            target = self._import_target(module, stmt.module, stmt.level)
            for alias in stmt.names:
                name = alias.asname or alias.name
                if alias.name == "*":
                    raise RuntimeConditionsError(
                        f"{module.path}:{stmt.lineno}:{stmt.col_offset + 1}: "
                        "star import cannot be resolved statically for generated declarations"
                    )
                submodule = f"{target}.{alias.name}" if target else alias.name
                if submodule in self.modules:
                    value: Any = LocalModule(submodule)
                elif target in self.modules:
                    value = LocalExport(target, alias.name)
                elif target.partition(".")[0] in self.binding_packages:
                    value = NativeSymbol(target.partition(".")[0], tuple(target.split(".")[1:] + [alias.name]))
                else:
                    value = Unknown(f"unresolved import {submodule}")
                self._bind(environment, name, value)
            return
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for expression in (*stmt.decorator_list, *stmt.args.defaults,
                               *(item for item in stmt.args.kw_defaults if item is not None)):
                self._calls_in(module.path, expression, environment, dynamic=True)
            self._functions.append((
                module, stmt, None if environment is module.environment else environment.copy(), dynamic,
            ))
            self._bind(environment, stmt.name, Unknown(f"function {stmt.name} is dynamic"))
            return
        if isinstance(stmt, ast.ClassDef):
            for expression in (*stmt.decorator_list, *stmt.bases):
                self._calls_in(module.path, expression, environment, dynamic=True)
            class_environment = environment.copy()
            for child in stmt.body:
                self._scan(module, child, class_environment, dynamic)
            self._bind(environment, stmt.name, Unknown(f"class {stmt.name} is dynamic"))
            return
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            value = stmt.value
            if value is not None:
                self._calls_in(module.path, value, environment, dynamic)
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                if isinstance(target, ast.Name) and value is not None:
                    self._bind(environment, target.id, BoundExpr(value, environment.copy()))
                elif isinstance(target, ast.Name):
                    self._bind(environment, target.id, Unknown(f"{target.id} has no static value"))
            return
        if isinstance(stmt, (ast.Expr, ast.Return)):
            if stmt.value is not None:
                self._calls_in(module.path, stmt.value, environment, dynamic)
            return
        if isinstance(stmt, ast.Pass):
            return
        # The body of a control-flow statement is not a straight-line value.
        self._calls_in(module.path, stmt, environment, dynamic=True)
        branch = environment.copy()
        def scan_children(parent: ast.AST) -> None:
            for child in ast.iter_child_nodes(parent):
                if isinstance(child, ast.stmt):
                    self._scan(module, child, branch, dynamic=True)
                elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                    scan_children(child)

        scan_children(stmt)
        for name, value in branch.items():
            previous = environment.get(name)
            if previous is not value:
                environment[name] = Rebound(name, previous if previous is not None else value)

    def _scan_function(
        self, module: _Module, function: ast.FunctionDef | ast.AsyncFunctionDef,
        environment: dict[str, Any], dynamic: bool,
    ) -> None:
        arguments = function.args.posonlyargs + function.args.args + function.args.kwonlyargs
        for arg in arguments:
            environment[arg.arg] = Unknown(f"parameter {arg.arg} is dynamic")
        if function.args.vararg:
            environment[function.args.vararg.arg] = Unknown("variadic parameter is dynamic")
        if function.args.kwarg:
            environment[function.args.kwarg.arg] = Unknown("keyword parameter is dynamic")
        for stmt in function.body:
            self._scan(module, stmt, environment, dynamic)

    def evaluate(self, expression: ast.expr, environment: dict[str, Any], active: set[int] | None = None) -> Any:
        active = set() if active is None else active
        if isinstance(expression, ast.Constant) and type(expression.value) in (str, bool, int, float, type(None)):
            return expression.value
        if isinstance(expression, ast.Name):
            return self._resolve(environment.get(expression.id, Unknown(f"unresolved name {expression.id}")), active)
        if isinstance(expression, ast.Attribute):
            base = self.evaluate(expression.value, environment, active)
            if isinstance(base, NativeSymbol):
                return NativeSymbol(base.package, base.path + (expression.attr,))
            if isinstance(base, LocalModule):
                submodule = f"{base.name}.{expression.attr}"
                return self._resolve(LocalModule(submodule) if submodule in self.modules else LocalExport(base.name, expression.attr), active)
            raise ValueError(f"attribute {expression.attr} has no static binding")
        if isinstance(expression, (ast.List, ast.Tuple)):
            return [self.evaluate(item, environment, active) for item in expression.elts]
        if isinstance(expression, ast.Dict):
            result: dict[str, Any] = {}
            for key, value in zip(expression.keys, expression.values, strict=True):
                if key is None:
                    raise ValueError("dictionary unpacking is dynamic")
                resolved = self.evaluate(key, environment, active)
                if not isinstance(resolved, str) or resolved in result:
                    raise ValueError("map keys must be unique static strings")
                result[resolved] = self.evaluate(value, environment, active)
            return result
        if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, (ast.UAdd, ast.USub)):
            value = self.evaluate(expression.operand, environment, active)
            if type(value) in (int, float):
                return value if isinstance(expression.op, ast.UAdd) else -value
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
            left = self.evaluate(expression.left, environment, active)
            right = self.evaluate(expression.right, environment, active)
            if type(left) is type(right) and isinstance(left, (str, list)):
                return left + right
        if isinstance(expression, ast.Call):
            callee = self.evaluate(expression.func, environment, active)
            if any(isinstance(arg, ast.Starred) for arg in expression.args):
                raise ValueError("starred call arguments are dynamic")
            args = tuple(self.evaluate(arg, environment, active) for arg in expression.args)
            kwargs: dict[str, Any] = {}
            for keyword in expression.keywords:
                if keyword.arg is None or keyword.arg in kwargs:
                    raise ValueError("unpacked or duplicate keyword arguments are dynamic")
                kwargs[keyword.arg] = self.evaluate(keyword.value, environment, active)
            return StaticCall(callee, args, kwargs)
        raise ValueError(f"unsupported static expression {type(expression).__name__}")

    def _resolve(self, value: Any, active: set[int]) -> Any:
        if isinstance(value, Rebound):
            raise ValueError(f"{value.name} is rebound")
        if isinstance(value, Unknown):
            raise ValueError(value.reason)
        if isinstance(value, BoundExpr):
            if id(value) in active:
                raise ValueError("cyclic static reference")
            active.add(id(value))
            try:
                return self.evaluate(value.expression, value.environment, active)
            finally:
                active.remove(id(value))
        if isinstance(value, LocalExport):
            module = self.modules.get(value.module)
            if module is None:
                raise ValueError(f"unresolved module {value.module}")
            if id(module) in active:
                raise ValueError("cyclic module reference")
            active.add(id(module))
            try:
                return self._resolve(module.environment.get(value.name, Unknown(f"unresolved {value.module}.{value.name}")), active)
            finally:
                active.remove(id(module))
        return value

    def maybe_native(self, expression: ast.expr, environment: dict[str, Any]) -> NativeSymbol | None:
        """Recover an imported symbol even when a later binding made it invalid."""
        if isinstance(expression, ast.Name):
            value = environment.get(expression.id)
            while isinstance(value, Rebound):
                value = value.previous
            try:
                resolved = self._resolve(value, set())
            except ValueError:
                return None
            return resolved if isinstance(resolved, NativeSymbol) else None
        if isinstance(expression, ast.Attribute):
            base = self.maybe_native(expression.value, environment)
            if base is not None:
                return NativeSymbol(base.package, base.path + (expression.attr,))
        return None
