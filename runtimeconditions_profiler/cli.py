from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .main import (
    DiscoveryOptions,
    ProjectDiscovery,
    ProfileExtractor,
    ProfileOptions,
    ProfileYamlWriter,
    RuntimeConditionsError,
    ArtifactDiscovery,
    ArtifactValidator,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtimeconditions-python-profiler")
    subparsers = parser.add_subparsers(dest="command")

    discover = subparsers.add_parser("discover")
    add_project_flags(discover)
    discover.add_argument("--json", action="store_true")

    generate = subparsers.add_parser("generate")
    add_project_flags(generate)
    generate.add_argument("--name", default="")
    generate.add_argument("--workload-uri", default="")
    generate.add_argument("--workload-version", default="dev")
    generate.add_argument("--out")

    validate_one = subparsers.add_parser("validate-extension")
    validate_one.add_argument("--root", default=".")
    validate_one.add_argument("--catalog-root", action="append", default=[])

    validate_many = subparsers.add_parser("validate-extensions")
    validate_many.add_argument("--root", default=".")
    validate_many.add_argument("--catalog-root", action="append", default=[])

    args = parser.parse_args(argv)
    command = args.command or "discover"
    try:
        if command == "discover":
            return run_discover(args)
        if command == "generate":
            return run_generate(args)
        if command == "validate-extension":
            return run_validate(args, plural=False)
        if command == "validate-extensions":
            return run_validate(args, plural=True)
    except RuntimeConditionsError as exc:
        print(f"runtimeconditions: {exc}", file=sys.stderr)
        return 1
    return 0


def add_project_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default=".")
    parser.add_argument("--package-path", action="append", default=[])
    parser.add_argument("--resolve-package-paths", action="store_true")


def discovery_options(args: argparse.Namespace) -> DiscoveryOptions:
    package_paths: list[Path] = []
    for value in args.package_path:
        for item in value.split(os.pathsep):
            if item.strip():
                package_paths.append(Path(item))
    return DiscoveryOptions(package_paths=package_paths, resolve_package_paths=args.resolve_package_paths)


def run_discover(args: argparse.Namespace) -> int:
    result = ProjectDiscovery().discover(Path(args.project), discovery_options(args))
    if args.json:
        print(json.dumps(result.to_json(), indent=2))
        return 0
    print(f"project: {result.project_root}")
    print(f"buildTool: {result.project_type}")
    for path in result.package_paths:
        print(f"packagePath: {path}")
    for artifact in result.artifacts:
        print(
            "artifact: "
            f"kind={artifact.kind} "
            f"manifest={artifact.manifest_uri} "
            f"extension={artifact.extension_uri or ''} "
            f"origin={artifact.origin}"
        )
    for artifact in result.validated_artifacts:
        manifest = artifact.manifest
        package = manifest.package if manifest is not None else ""
        print(
            "validatedArtifact: "
            f"kind={artifact.artifact.kind} "
            f"manifestExtensionId={artifact.manifest_extension_id or ''} "
            f"extensionId={artifact.extension_id or ''} "
            f"extensionDefinition={artifact.extension_definition_uri or ''} "
            f"pythonPackage={package} "
            f"declarations={len(manifest.declarations) if manifest else 0} "
            f"options={len(manifest.options) if manifest else 0} "
            f"constants={len(manifest.constants) if manifest else 0}"
        )
    for diagnostic in result.diagnostics:
        print(
            "diagnostic: "
            f"severity={diagnostic.severity} "
            f"code={diagnostic.code} "
            f"source={diagnostic.source} "
            f"message={diagnostic.message}"
        )
    return 1 if result.has_errors else 0


def run_generate(args: argparse.Namespace) -> int:
    project = Path(args.project).absolute()
    name = args.name or project.name
    workload_uri = args.workload_uri or str(project)
    profile = ProfileExtractor().extract(
        project,
        ProfileOptions(
            name=name,
            workload_uri=workload_uri,
            workload_version=args.workload_version,
            discovery_options=discovery_options(args),
        ),
    )
    yaml_text = ProfileYamlWriter.write(profile)
    if args.out:
        Path(args.out).write_text(yaml_text, encoding="utf-8")
    else:
        print(yaml_text, end="")
    return 0


def run_validate(args: argparse.Namespace, plural: bool) -> int:
    discovery = ArtifactDiscovery()
    artifacts = [item for item in discovery.discover_artifacts_under(Path(args.root)) if item.language in ("", "python")]
    for root in args.catalog_root:
        artifacts.extend(
            item for item in discovery.discover_artifacts_under(Path(root)) if item.language in ("", "python")
        )
    if not artifacts:
        raise RuntimeConditionsError(
            f"no Runtime Conditions Python artifacts discovered under {Path(args.root).absolute()}"
        )
    validated = ArtifactValidator().validate(artifacts)
    diagnostics = [diagnostic for artifact in validated for diagnostic in artifact.diagnostics]
    if diagnostics:
        lines = "\n".join(f"- {diagnostic.source}: {diagnostic.message}" for diagnostic in diagnostics)
        raise RuntimeConditionsError(f"extension validation failed:\n{lines}")
    suffix = "s" if plural else ""
    print(f"runtimeconditions: extension{suffix} validation passed", file=sys.stderr)
    return 0
