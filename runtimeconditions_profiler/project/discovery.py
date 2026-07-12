from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from ..constants import BINDINGS_MANIFEST, EXTENSION_DEFINITION, PACKAGE_MANIFEST
from ..extension import ArtifactValidator
from ..models import DiscoveryOptions, DiscoveryResult, RuntimeConditionsArtifact
from ..util import as_map, dedupe_artifacts, dedupe_paths, ignored_path, scalar
from ..yamlio import Yaml


class ArtifactDiscovery:
    def discover_project_artifacts(self, project_root: Path) -> list[RuntimeConditionsArtifact]:
        root = project_root.absolute().resolve()
        candidates = [root]
        src = root / "src"
        if src.is_dir():
            candidates.append(src)
        artifacts: list[RuntimeConditionsArtifact] = []
        seen: set[Path] = set()
        for candidate in candidates:
            for artifact in self._discover_directory_tree(candidate, f"project:{root}"):
                if artifact.source_path and artifact.source_path in seen:
                    continue
                if artifact.source_path:
                    seen.add(artifact.source_path)
                artifacts.append(artifact)
        return artifacts

    def discover_path_artifact(self, package_path: Path) -> list[RuntimeConditionsArtifact]:
        root = package_path.absolute().resolve()
        return self._discover_directory_tree(root, f"package:{root}")

    def discover_artifacts_under(self, root: Path) -> list[RuntimeConditionsArtifact]:
        resolved = root.absolute().resolve()
        if not resolved.is_dir():
            return []
        artifacts: list[RuntimeConditionsArtifact] = []
        for manifest in sorted(resolved.rglob("*")):
            if manifest.name not in (BINDINGS_MANIFEST, PACKAGE_MANIFEST):
                continue
            if ignored_path(manifest):
                continue
            artifacts.extend(self._discover_manifest_dir(manifest.parent, f"root:{resolved}"))
        return dedupe_artifacts(artifacts)

    def _discover_directory_tree(self, root: Path, origin: str) -> list[RuntimeConditionsArtifact]:
        if not root.is_dir():
            return []
        artifacts: list[RuntimeConditionsArtifact] = []
        for manifest in sorted(root.rglob("*")):
            if manifest.name not in (BINDINGS_MANIFEST, PACKAGE_MANIFEST):
                continue
            if ignored_path(manifest):
                continue
            artifacts.extend(self._discover_manifest_dir(manifest.parent, origin))
        return dedupe_artifacts(artifacts)

    def _discover_manifest_dir(self, directory: Path, origin: str) -> list[RuntimeConditionsArtifact]:
        extension = directory / EXTENSION_DEFINITION
        artifacts: list[RuntimeConditionsArtifact] = []
        for filename, kind in ((BINDINGS_MANIFEST, "binding"), (PACKAGE_MANIFEST, "package")):
            manifest = directory / filename
            if not manifest.is_file():
                continue
            artifacts.append(
                RuntimeConditionsArtifact(
                    kind=kind,
                    manifest_uri=manifest.absolute().resolve().as_uri(),
                    extension_uri=extension.absolute().resolve().as_uri() if extension.is_file() else None,
                    origin=origin,
                    source_path=manifest.absolute().resolve(),
                    root=directory.absolute().resolve(),
                    language=manifest_language(manifest),
                )
            )
        return artifacts


class ProjectDiscovery:
    def discover(self, project_root: Path, options: DiscoveryOptions) -> DiscoveryResult:
        root = project_root.absolute().resolve()
        project_type = detect_project_type(root)
        package_paths = [path.absolute().resolve() for path in options.package_paths]
        if options.resolve_package_paths:
            package_paths.extend(resolve_pyproject_package_paths(root))
        package_paths = dedupe_paths(package_paths)

        discovery = ArtifactDiscovery()
        artifacts = discovery.discover_project_artifacts(root)
        for package_path in package_paths:
            artifacts.extend(discovery.discover_path_artifact(package_path))
        artifacts = dedupe_artifacts(artifacts)
        validated = ArtifactValidator().validate(artifacts)
        return DiscoveryResult(root, project_type, package_paths, artifacts, validated)


def resolve_pyproject_package_paths(root: Path) -> list[Path]:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    paths: list[Path] = []
    project = data.get("project", {})
    if isinstance(project, dict):
        for dependency in project.get("dependencies", []) or []:
            if isinstance(dependency, str):
                parsed = path_from_dependency(dependency, root)
                if parsed is not None:
                    paths.append(parsed)
    tool = data.get("tool", {})
    if isinstance(tool, dict):
        runtimeconditions = tool.get("runtimeconditions", {})
        if isinstance(runtimeconditions, dict):
            for value in runtimeconditions.get("package-paths", []) or []:
                if isinstance(value, str):
                    paths.append((root / value).resolve())
        uv = tool.get("uv", {})
        if isinstance(uv, dict):
            sources = uv.get("sources", {})
            if isinstance(sources, dict):
                for source in sources.values():
                    if isinstance(source, dict) and isinstance(source.get("path"), str):
                        paths.append((root / source["path"]).resolve())
            workspace = uv.get("workspace", {})
            if isinstance(workspace, dict):
                for member in workspace.get("members", []) or []:
                    if isinstance(member, str):
                        paths.append((root / member).resolve())
        poetry = tool.get("poetry", {})
        if isinstance(poetry, dict):
            dependencies = poetry.get("dependencies", {})
            if isinstance(dependencies, dict):
                for dependency in dependencies.values():
                    if isinstance(dependency, dict) and isinstance(dependency.get("path"), str):
                        paths.append((root / dependency["path"]).resolve())
    return dedupe_paths(paths)


def path_from_dependency(dependency: str, root: Path) -> Optional[Path]:
    match = re.search(r"@\s*file://([^ ;]+)", dependency)
    if match:
        return Path(match.group(1)).resolve()
    match = re.search(r"@\s*(\.\.?/[^ ;]+)", dependency)
    if match:
        return (root / match.group(1)).resolve()
    return None


def detect_project_type(root: Path) -> str:
    if (root / "pyproject.toml").is_file():
        return "pyproject"
    if (root / "setup.py").is_file():
        return "setuptools"
    return "source_only"


def manifest_language(path: Path) -> str:
    try:
        data = Yaml.load(path)
    except Exception:
        return ""
    return scalar(as_map(data.get("metadata")).get("language")) or ""

