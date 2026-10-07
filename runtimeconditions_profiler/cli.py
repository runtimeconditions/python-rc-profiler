from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
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
from .sdk.resolver import BUNDLE_INDEX, bundle_artifact_paths, resolve_mappings
from .project.installed import InstalledBindingDiscovery
from .project.verify import InstalledBindingVerifier, VerifiedBindingSet
from .profile.generated import GeneratedBindingExtractor
from .profile.semantic import GeneratedProfileValidator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtimeconditions-python-profiler")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover")
    add_project_flags(discover)
    discover.add_argument("--json", action="store_true")

    generate = subparsers.add_parser("generate")
    add_project_flags(generate)
    generate.add_argument("--name", default="")
    generate.add_argument("--workload-uri", default="")
    generate.add_argument("--workload-version", default="dev")
    generate.add_argument("--out")

    profile = subparsers.add_parser("profile")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_generate = profile_commands.add_parser("generate")
    profile_generate.add_argument("--project", default=".")
    profile_generate.add_argument("--name", required=True)
    profile_generate.add_argument("--workload-uri", required=True)
    profile_generate.add_argument("--workload-version", required=True)
    profile_generate.add_argument("--out")
    profile_verify = profile_commands.add_parser(
        "verify-bindings",
        help="verify installed generated bindings without running application or binding code",
    )
    profile_verify.add_argument("--project", default=".")
    profile_verify.add_argument("--json", action="store_true")

    mappings = subparsers.add_parser("mappings")
    mapping_commands = mappings.add_subparsers(dest="mapping_command", required=True)
    resolve = mapping_commands.add_parser("resolve")
    resolve.add_argument("--project", default=".")
    resolve.add_argument("--catalog", required=True)
    resolve.add_argument("--out", default="")

    validate_one = subparsers.add_parser("validate-extension")
    validate_one.add_argument("--root", default=".")
    validate_one.add_argument("--catalog-root", action="append", default=[])

    validate_many = subparsers.add_parser("validate-extensions")
    validate_many.add_argument("--root", default=".")
    validate_many.add_argument("--catalog-root", action="append", default=[])

    args = parser.parse_args(argv)
    command = args.command
    try:
        if command == "discover":
            return run_discover(args)
        if command == "generate":
            return run_generate(args)
        if command == "profile" and args.profile_command == "generate":
            return run_profile_generate(args)
        if command == "profile" and args.profile_command == "verify-bindings":
            return run_profile_verify_bindings(args)
        if command == "mappings" and args.mapping_command == "resolve":
            return run_resolve(args)
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
    parser.add_argument("--mapping", action="append", default=[])
    parser.add_argument("--extension", action="append", default=[])
    parser.add_argument("--mappings", action="append", default=[])


def discovery_options(args: argparse.Namespace) -> DiscoveryOptions:
    package_paths: list[Path] = []
    for value in getattr(args, "package_path", []):
        for item in value.split(os.pathsep):
            if item.strip():
                package_paths.append(Path(item))
    project = Path(args.project).absolute().resolve()
    bundle_paths = [Path(item) for item in getattr(args, "mappings", [])]
    default_bundle = default_mapping_cache()
    if args.command != "profile" and not bundle_paths and (default_bundle / BUNDLE_INDEX).is_file():
        bundle_paths.append(default_bundle)
    bundle_mappings, bundle_extensions = bundle_artifact_paths(project, bundle_paths)
    return DiscoveryOptions(
        package_paths=package_paths,
        resolve_package_paths=getattr(args, "resolve_package_paths", False),
        mapping_paths=[Path(item) for item in getattr(args, "mapping", [])] + bundle_mappings,
        extension_paths=[Path(item) for item in getattr(args, "extension", [])] + bundle_extensions,
    )


def default_mapping_cache() -> Path:
    return Path.home() / ".cache" / "runtimeconditions" / "mappings"


def run_resolve(args: argparse.Namespace) -> int:
    destination = Path(args.out).absolute() if args.out else default_mapping_cache()
    resolved = resolve_mappings(Path(args.project), args.catalog, destination)
    for entry in resolved:
        print(
            f"resolved {entry['package']} {entry['packageVersion']} "
            f"to {entry['mapping']}"
        )
    print(f"runtimeconditions: resolved {len(resolved)} mapping(s) into {destination}", file=sys.stderr)
    return 0


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
    for artifact in result.sdk_mappings:
        print(
            "sdkMapping: "
            f"distribution={artifact.distribution} "
            f"version={artifact.distribution_version} "
            f"name={artifact.name} "
            f"mapping={artifact.mapping_path}"
        )
    for artifact in result.sdk_extensions:
        print(
            "sdkExtension: "
            f"id={artifact.id} "
            f"version={artifact.version} "
            f"semanticSha256={artifact.semantic_sha256} "
            f"path={artifact.path}"
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


def run_profile_generate(args: argparse.Namespace) -> int:
    verified = verified_installed_bindings(Path(args.project))
    extracted = GeneratedBindingExtractor(verified).extract(Path(args.project))
    profile = GeneratedProfileValidator(verified).build(
        extracted, args.name, args.workload_uri, args.workload_version,
    )
    yaml_text = ProfileYamlWriter.write(profile)
    if args.out:
        destination = Path(args.out)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(yaml_text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except OSError as exc:
            raise RuntimeConditionsError(f"cannot write profile {destination}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    else:
        print(yaml_text, end="")
    return 0


def verified_installed_bindings(project: Path) -> VerifiedBindingSet:
    discovery = InstalledBindingDiscovery().discover(project)
    return InstalledBindingVerifier().verify(discovery)


def run_profile_verify_bindings(args: argparse.Namespace) -> int:
    verified = verified_installed_bindings(Path(args.project))
    packages = [
        {
            "distribution": item.installed.distribution,
            "version": item.installed.version,
            "importPackage": item.installed.import_package,
            "extensionId": item.model["rootExtension"]["id"],
            "extensionVersion": item.model["rootExtension"]["version"],
            "modelSha256": item.model["metadata"]["semanticSha256"],
        }
        for item in verified.packages
    ]
    if args.json:
        print(json.dumps({"packages": packages}, sort_keys=True))
    else:
        for item in packages:
            print(f"verified {item['distribution']}=={item['version']} ({item['importPackage']})")
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
