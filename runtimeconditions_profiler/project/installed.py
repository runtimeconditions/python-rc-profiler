"""Discover generated bindings through installed Python distribution metadata.

This module never imports a workload or binding package. In particular, it does
not use ``importlib.resources.files(package)``, which can execute package code.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from ..errors import RuntimeConditionsError
from ..source.python import workload_source_files


RESOURCE_NAMES = (
    "runtimeconditions.bindings.yaml",
    "runtimeconditions.binding-model.yaml",
    "runtimeconditions.extension.yaml",
    "runtimeconditions.binding-release.yaml",
)
MAX_RESOURCE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class SourceImport:
    source: Path
    line: int
    column: int
    module: str
    symbol: str | None
    alias: str

    @property
    def package_root(self) -> str:
        return self.module.partition(".")[0]

    @property
    def location(self) -> str:
        return f"{self.source}:{self.line}:{self.column + 1}"


@dataclass(frozen=True)
class InstalledBindingPackage:
    import_package: str
    distribution: str
    version: str
    resource_paths: dict[str, Path]
    resources: dict[str, bytes]


@dataclass(frozen=True)
class ResolvedBindingImport:
    source_import: SourceImport
    package: InstalledBindingPackage


@dataclass(frozen=True)
class InstalledBindingDiscoveryResult:
    packages: tuple[InstalledBindingPackage, ...]
    imports: tuple[ResolvedBindingImport, ...]


class InstalledBindingDiscovery:
    def discover(self, project_root: Path) -> InstalledBindingDiscoveryResult:
        root = project_root.absolute().resolve()
        if not root.is_dir():
            raise RuntimeConditionsError(f"workload directory does not exist: {root}")
        source_files = workload_source_files(root)
        if not source_files:
            raise RuntimeConditionsError(f"no Python source files found under {root}")
        imports = _source_imports(source_files)
        imports_by_root: dict[str, list[SourceImport]] = {}
        for item in imports:
            imports_by_root.setdefault(item.package_root, []).append(item)

        package_distributions = metadata.packages_distributions()
        packages: list[InstalledBindingPackage] = []
        for package_root, package_imports in sorted(imports_by_root.items()):
            distribution_names = sorted(package_distributions.get(package_root, ()))
            candidates: list[
                tuple[str, metadata.Distribution, dict[str, metadata.PackagePath]]
            ] = []
            package_init_owners: list[str] = []
            for distribution_name in distribution_names:
                try:
                    distribution = metadata.distribution(distribution_name)
                except metadata.PackageNotFoundError as exc:
                    raise RuntimeConditionsError(
                        f"{package_imports[0].location}: installed distribution metadata for "
                        f"{distribution_name} is unavailable"
                    ) from exc
                declared_name = distribution.metadata.get("Name")
                if not declared_name or not distribution.version:
                    raise RuntimeConditionsError(
                        f"{package_imports[0].location}: installed distribution "
                        f"{distribution_name} has incomplete package metadata"
                    )
                recorded_files = distribution.files
                if recorded_files is None:
                    raise RuntimeConditionsError(
                        f"{package_imports[0].location}: installed distribution {distribution_name} "
                        "has no file record"
                    )
                records = {str(file).replace("\\", "/"): file for file in recorded_files}
                if f"{package_root}/__init__.py" in records:
                    package_init_owners.append(distribution_name)
                resources = {
                    name: records[f"{package_root}/{name}"]
                    for name in RESOURCE_NAMES
                    if f"{package_root}/{name}" in records
                }
                if not resources and f"{package_root}/__init__.py" in records:
                    misplaced = sorted(
                        path for path in records if path.rpartition("/")[2] in RESOURCE_NAMES
                    )
                    if misplaced:
                        raise RuntimeConditionsError(
                            f"{package_imports[0].location}: installed binding "
                            f"{distribution_name} has resources outside the fixed "
                            f"{package_root}/ package location: {', '.join(misplaced)}"
                        )
                if resources:
                    missing = [name for name in RESOURCE_NAMES if name not in resources]
                    if missing:
                        raise RuntimeConditionsError(
                            f"{package_imports[0].location}: installed binding {distribution_name} "
                            f"is missing package data: {', '.join(missing)}"
                        )
                    if f"{package_root}/__init__.py" not in records:
                        raise RuntimeConditionsError(
                            f"{package_imports[0].location}: installed binding {distribution_name} "
                            f"does not own {package_root}/__init__.py"
                        )
                    candidates.append((declared_name, distribution, resources))

            if not candidates:
                continue
            if len(candidates) != 1 or len(package_init_owners) != 1:
                owners = ", ".join(
                    sorted(set(package_init_owners + [item[0] for item in candidates]))
                )
                raise RuntimeConditionsError(
                    f"{package_imports[0].location}: import package {package_root} has "
                    f"ambiguous distribution ownership: {owners}"
                )
            if _shadows_installed_package(root, package_root):
                raise RuntimeConditionsError(
                    f"{package_imports[0].location}: workload source shadows installed "
                    f"binding package {package_root}"
                )
            declared_name, distribution, resource_records = candidates[0]
            resource_paths: dict[str, Path] = {}
            resource_bytes: dict[str, bytes] = {}
            for name in RESOURCE_NAMES:
                path = Path(distribution.locate_file(resource_records[name]))
                try:
                    with path.open("rb") as stream:
                        content = stream.read(MAX_RESOURCE_BYTES + 1)
                except OSError as exc:
                    raise RuntimeConditionsError(
                        f"{package_imports[0].location}: cannot read {name} from "
                        f"installed distribution {declared_name}: {exc}"
                    ) from exc
                if len(content) > MAX_RESOURCE_BYTES:
                    raise RuntimeConditionsError(
                        f"{package_imports[0].location}: {name} in installed distribution "
                        f"{declared_name} exceeds {MAX_RESOURCE_BYTES} bytes"
                    )
                resource_paths[name] = path
                resource_bytes[name] = content
            packages.append(
                InstalledBindingPackage(
                    import_package=package_root,
                    distribution=declared_name,
                    version=distribution.version,
                    resource_paths=resource_paths,
                    resources=resource_bytes,
                )
            )

        by_root = {package.import_package: package for package in packages}
        return InstalledBindingDiscoveryResult(
            packages=tuple(packages),
            imports=tuple(
                ResolvedBindingImport(item, by_root[item.package_root])
                for item in imports
                if item.package_root in by_root
            ),
        )


def _source_imports(source_files: list[Path]) -> list[SourceImport]:
    found: list[SourceImport] = []
    for source in source_files:
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise RuntimeConditionsError(f"cannot parse Python source {source}: {exc}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.append(
                        SourceImport(
                            source=source,
                            line=node.lineno,
                            column=node.col_offset,
                            module=alias.name,
                            symbol=None,
                            alias=alias.asname or alias.name.partition(".")[0],
                        )
                    )
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                for alias in node.names:
                    found.append(
                        SourceImport(
                            source=source,
                            line=node.lineno,
                            column=node.col_offset,
                            module=node.module,
                            symbol=alias.name,
                            alias=alias.asname or alias.name,
                        )
                    )
    return sorted(found, key=lambda item: (str(item.source), item.line, item.column, item.module, item.symbol or "", item.alias))


def _shadows_installed_package(project_root: Path, package_root: str) -> bool:
    for source_root in (project_root, project_root / "src"):
        if (source_root / f"{package_root}.py").exists():
            return True
        if (source_root / package_root / "__init__.py").exists():
            return True
    return False
