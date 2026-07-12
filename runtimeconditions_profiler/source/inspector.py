from __future__ import annotations

from .python import PythonSourceIndex, python_files, resolve_package_source_root
from ..manifest.mapping import all_mappings
from ..models import ValidatedArtifact


class SourceInspector:
    def validate(self, artifact: ValidatedArtifact) -> None:
        manifest = artifact.manifest
        if manifest is None:
            return
        package_root = resolve_package_source_root(artifact.artifact.root, manifest.package)
        if package_root is None:
            artifact.add(
                "package-source",
                artifact.artifact.manifest_uri,
                f"python package {manifest.package} is not declared in the artifact source",
            )
            return
        source = PythonSourceIndex.from_paths(python_files(package_root))
        for mapping in all_mappings(manifest):
            source.validate_mapping(artifact, mapping)
        for constant, value in manifest.constants.items():
            actual = source.constants.get(constant)
            if actual is not None and actual != value:
                artifact.add(
                    "package-source",
                    artifact.artifact.manifest_uri,
                    f"constant {constant} value {value} does not match Python value {actual}",
                )

