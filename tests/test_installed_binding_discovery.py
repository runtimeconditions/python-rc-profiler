from __future__ import annotations

from runtimeconditions_profiler.extension.identity import parse_identifier, reference_object

import sys
import json
import base64
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path, PurePosixPath

import pytest
import yaml

from runtimeconditions_profiler import cli
from runtimeconditions_profiler.errors import RuntimeConditionsError
from runtimeconditions_profiler.project import installed
from runtimeconditions_profiler.project import verify
from runtimeconditions_profiler.profile.generated import ExtractedCondition, GeneratedBindingExtractor
from runtimeconditions_profiler.profile.semantic import (
    CORE_ID, CORE_SEMANTIC_SHA256, CORE_VERSION, GeneratedProfileValidator,
)


class DistributionRecord:
    def __init__(self, root: Path, name: str, files: list[PurePosixPath], import_package: str = "example_binding") -> None:
        self.root = root
        self.metadata = {"Name": name, "Requires-Python": ">=3.11"}
        self.version = "1.2.3"
        self.files = files
        self.requires: list[str] = []
        self.import_package = import_package

    def locate_file(self, file: PurePosixPath) -> Path:
        return self.root.joinpath(*file.parts)


def binding_distribution(
    tmp_path: Path, name: str = "example-binding", missing: str | None = None,
    import_package: str = "example_binding",
) -> DistributionRecord:
    root = tmp_path / name
    package = root / import_package
    package.mkdir(parents=True)
    # Importing the binding package during discovery would execute this file.
    (package / "__init__.py").write_text("raise AssertionError('package was imported')\n")
    files = [PurePosixPath(import_package) / "__init__.py"]
    for resource in installed.RESOURCE_NAMES:
        if resource != missing:
            (package / resource).write_bytes(resource.encode())
            files.append(PurePosixPath(import_package) / resource)
    return DistributionRecord(root, name, files, import_package)


def set_distributions(monkeypatch: pytest.MonkeyPatch, *records: DistributionRecord) -> None:
    owners: dict[str, list[str]] = {}
    for record in records:
        owners.setdefault(record.import_package, []).append(record.metadata["Name"])
    monkeypatch.setattr(
        installed.metadata,
        "packages_distributions",
        lambda: owners,
    )
    monkeypatch.setattr(
        installed.metadata,
        "distribution",
        lambda name: next(record for record in records if record.metadata["Name"] == name),
    )


def test_imports_resolve_to_recorded_resources_without_importing_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text(
        "import example_binding as binding\n"
        "from example_binding import service as declare\n"
        "from example_binding.bindings import Region\n"
        "from example_binding import *\n"
        "import json\n"
    )
    record = binding_distribution(tmp_path)
    monkeypatch.syspath_prepend(str(record.root))
    set_distributions(monkeypatch, record)

    result = installed.InstalledBindingDiscovery().discover(workload)

    assert len(result.packages) == 1
    package = result.packages[0]
    assert (package.import_package, package.distribution, package.version) == (
        "example_binding",
        "example-binding",
        "1.2.3",
    )
    assert package.resources == {
        name: name.encode() for name in installed.RESOURCE_NAMES
    }
    assert set(package.resource_paths) == set(installed.RESOURCE_NAMES)
    assert [(item.source_import.module, item.source_import.symbol, item.source_import.alias) for item in result.imports] == [
        ("example_binding", None, "binding"),
        ("example_binding", "service", "declare"),
        ("example_binding.bindings", "Region", "Region"),
        ("example_binding", "*", "*"),
    ]
    assert all(item.package is package for item in result.imports)
    assert "example_binding" not in sys.modules


def test_missing_package_resource_fails_before_profile_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("from example_binding import service\n")
    set_distributions(
        monkeypatch,
        binding_distribution(tmp_path, missing="runtimeconditions.binding-model.yaml"),
    )
    output = tmp_path / "profile.yaml"

    status = cli.main(
        [
            "profile", "generate", "--project", str(workload), "--name", "sample",
            "--workload-uri", "example/sample", "--workload-version", "1", "--out", str(output),
        ]
    )

    assert status == 1
    assert "missing package data: runtimeconditions.binding-model.yaml" in capsys.readouterr().err
    assert not output.exists()


def test_profile_command_rejects_placeholder_core_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("from example_binding import service\n")
    set_distributions(monkeypatch, verified_distribution(tmp_path))
    output = tmp_path / "profile.yaml"

    status = cli.main(
        [
            "profile", "generate", "--project", str(workload), "--name", "sample",
            "--workload-uri", "example/sample", "--workload-version", "1", "--out", str(output),
        ]
    )

    assert status == 1
    assert "binding model core profile schema identity or digest mismatch" in capsys.readouterr().err
    assert not output.exists()


def test_profile_command_reports_unresolved_declaration_without_writing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text(
        "from example_binding import service\nservice(dynamic_value())\n"
    )
    set_distributions(monkeypatch, verified_distribution(tmp_path))
    output = tmp_path / "profile.yaml"
    status = cli.main([
        "profile", "generate", "--project", str(workload), "--name", "sample",
        "--workload-uri", "example/sample", "--workload-version", "1", "--out", str(output),
    ])
    assert status == 1
    diagnostic = capsys.readouterr().err
    assert f"{workload / 'app.py'}:2:1" in diagnostic
    assert "kind:service: unresolved name dynamic_value" in diagnostic
    assert not output.exists()


def test_ambiguous_installed_package_owner_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("import example_binding\n")
    set_distributions(
        monkeypatch,
        binding_distribution(tmp_path, "first-binding"),
        binding_distribution(tmp_path, "second-binding"),
    )

    with pytest.raises(RuntimeConditionsError, match="ambiguous distribution ownership"):
        installed.InstalledBindingDiscovery().discover(workload)


def test_resources_at_wrong_package_location_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("import example_binding\n")
    record = binding_distribution(tmp_path)
    package = record.root / "example_binding"
    other = package / "resources"
    other.mkdir()
    for name in installed.RESOURCE_NAMES:
        (package / name).rename(other / name)
    record.files = [
        PurePosixPath("example_binding/__init__.py"),
        *(PurePosixPath("example_binding/resources") / name for name in installed.RESOURCE_NAMES),
    ]
    set_distributions(monkeypatch, record)

    with pytest.raises(RuntimeConditionsError, match="outside the fixed example_binding/ package location"):
        installed.InstalledBindingDiscovery().discover(workload)


def test_workload_package_shadowing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload = tmp_path / "workload"
    package = workload / "example_binding"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("\n")
    (workload / "app.py").write_text("import example_binding\n")
    set_distributions(monkeypatch, binding_distribution(tmp_path))

    with pytest.raises(RuntimeConditionsError, match="shadows installed binding package"):
        installed.InstalledBindingDiscovery().discover(workload)


class HashedRecord:
    def __init__(self, path: str, data: bytes) -> None:
        self.path = PurePosixPath(path)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        self.hash = SimpleNamespace(mode="sha256", value=digest)

    def __str__(self) -> str:
        return str(self.path)

    @property
    def parts(self) -> tuple[str, ...]:
        return self.path.parts


class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


def yaml_bytes(document: dict[str, object]) -> bytes:
    return yaml.dump(document, Dumper=NoAliasDumper, sort_keys=False).encode()


def replace_resource(record: DistributionRecord, resource: str, document: dict[str, object]) -> None:
    path = f"{record.import_package}/{resource}"
    data = yaml_bytes(document)
    (record.root / path).write_bytes(data)
    record.files = [item for item in record.files if str(item) != path]
    record.files.append(HashedRecord(path, data))


def verified_distribution(
    tmp_path: Path, name: str = "example-binding", import_package: str = "example_binding",
    extension_id: str = "https://example.test/extension:1.0.0", kind: str = "service",
) -> DistributionRecord:
    record = binding_distribution(tmp_path, name=name, import_package=import_package)
    package = record.root / import_package
    (package / "bindings.py").write_text("raise AssertionError('binding code was imported')\n")
    for source in ("__init__.py", "bindings.py"):
        path = f"{import_package}/{source}"
        record.files = [item for item in record.files if str(item) != path]
        record.files.append(HashedRecord(path, (package / source).read_bytes()))
    extension = {
        "apiVersion": "runtimeconditions.io/v1alpha1",
        "kind": "RuntimeConditionsExtensionDefinition",
        "metadata": {"id": extension_id, "version": "1.0.0"},
        "spec": {"kinds": [{"name": kind}]},
    }
    semantic = verify._semantic_value(
        extension,
        verify._schemas()[verify.SCHEMAS["runtimeconditions.extension.yaml"]],
        verify._schemas()[verify.SCHEMAS["runtimeconditions.extension.yaml"]],
    )
    extension_digest = verify._canonical_sha256(semantic)
    root = {"id": extension["metadata"]["id"], "version": extension["metadata"]["version"], "semanticSha256": extension_digest}
    package_id = {"language": "python", "coordinate": name, "name": import_package, "version": "1.2.3", "minimumPythonVersion": "3.11"}
    model = {
        "apiVersion": "runtimeconditions.io/binding-model/v1alpha1",
        "kind": "RuntimeConditionsBindingModel",
        "metadata": {"normalizer": {"name": "test-normalizer", "version": "1", "sha256": "0" * 64}},
        "coreProfileSchema": {"id": "https://example.test/core", "version": "1", "semanticSha256": "0" * 64},
        "rootExtension": root,
        "extensions": [root],
        "vocabulary": {"ownedDeclarations": [{"coordinate": f"kind:{kind}", "owner": root["id"], "kind": kind, "provenance": {"owner": root["id"], "extensionSha256": extension_digest, "coordinate": f"kind:{kind}"}}]},
    }
    model_digest = verify._canonical_sha256(model)
    model["metadata"]["semanticSha256"] = model_digest
    manifest = {
        "apiVersion": "runtimeconditions.io/bindings/v1alpha2",
        "kind": "RuntimeConditionsBindingManifest",
        "generated": {"nonEditable": True, "emitter": "test-emitter", "version": "1"},
        "model": {"apiVersion": model["apiVersion"], "semanticSha256": model_digest},
        "extension": dict(root),
        "package": package_id,
        "declarations": [{"modelRef": {"coordinate": f"kind:{kind}"}, "owner": root["id"], "sourceName": kind, "function": kind, "markerInterface": f"{kind.title()}Marker", "markerMethod": f"{kind}_marker", "file": "bindings.py"}],
        "importedMarkerContracts": [],
        "rootBindings": [],
        "types": [],
    }
    release = {
        "apiVersion": "runtimeconditions.io/binding-release/v1alpha1",
        "kind": "RuntimeConditionsBindingRelease",
        "package": {**package_id, "packageKey": name, "publicationMode": "registry", "registryId": "local-test"},
        "model": manifest["model"],
        "rootExtension": root,
        "dependencyLock": {"extensions": [{"id": root["id"], "version": root["version"], "semanticSha256": extension_digest, "sourceSha256": "", "sourceBackend": "package", "sourceLocator": f"installed:{name}"}]},
        "packageDependencies": [],
        "provenance": {"mode": "test-fixture", "fixtureAssembler": {"name": "test", "version": "1", "sha256": "0" * 64}, "profiler": {"name": "python-rc-profiler", "version": "1", "sha256": "0" * 64}},
    }
    documents = {
        "runtimeconditions.bindings.yaml": manifest,
        "runtimeconditions.binding-model.yaml": model,
        "runtimeconditions.extension.yaml": extension,
        "runtimeconditions.binding-release.yaml": release,
    }
    for name, document in documents.items():
        if name == "runtimeconditions.extension.yaml":
            data = yaml_bytes(document)
            release["dependencyLock"]["extensions"][0]["sourceSha256"] = hashlib.sha256(data).hexdigest()
    for name, document in documents.items():
        replace_resource(record, name, document)
    return record


def test_installed_binding_verifies_without_importing_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("from example_binding import service as declare\n")
    record = verified_distribution(tmp_path)
    set_distributions(monkeypatch, record)
    discovered = installed.InstalledBindingDiscovery().discover(workload)
    result = verify.InstalledBindingVerifier().verify(discovered)
    assert len(result.packages) == 1
    assert result.imported_packages[0].manifest["declarations"][0]["function"] == "service"
    assert "example_binding" not in sys.modules


def test_modified_generated_binding_source_fails_record_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("from example_binding import service\n")
    record = verified_distribution(tmp_path)
    set_distributions(monkeypatch, record)
    (record.root / "example_binding/bindings.py").write_text("altered after installation\n")
    discovered = installed.InstalledBindingDiscovery().discover(workload)
    with pytest.raises(RuntimeConditionsError, match="bindings.py fails installed RECORD hash"):
        verify.InstalledBindingVerifier().verify(discovered)


def test_installed_cli_verifies_binding_from_unrelated_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("import example_binding as binding\n")
    record = verified_distribution(tmp_path)
    set_distributions(monkeypatch, record)
    assert cli.main(["profile", "verify-bindings", "--project", str(workload), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["packages"][0]["distribution"] == "example-binding"
    assert output["packages"][0]["extensionId"] == "https://example.test/extension:1.0.0"
    assert "example_binding" not in sys.modules


@pytest.mark.parametrize(
    ("resource", "field", "value", "expected"),
    [
        ("runtimeconditions.bindings.yaml", "apiVersion", "wrong", "apiVersion"),
        ("runtimeconditions.binding-model.yaml", "kind", "wrong", "kind"),
        ("runtimeconditions.binding-release.yaml", "package", {"language": "python"}, "package"),
    ],
)
def test_malformed_binding_resource_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    resource: str, field: str, value: object, expected: str,
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("from example_binding import service\n")
    record = verified_distribution(tmp_path)
    path = record.root / "example_binding" / resource
    document = yaml.safe_load(path.read_text())
    document[field] = value
    replace_resource(record, resource, document)
    set_distributions(monkeypatch, record)
    discovered = installed.InstalledBindingDiscovery().discover(workload)
    with pytest.raises(RuntimeConditionsError, match=expected):
        verify.InstalledBindingVerifier().verify(discovered)


def test_altered_resource_fails_record_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("from example_binding import service\n")
    record = verified_distribution(tmp_path)
    (record.root / "example_binding/runtimeconditions.bindings.yaml").write_text("tampered: true\n")
    set_distributions(monkeypatch, record)
    discovered = installed.InstalledBindingDiscovery().discover(workload)
    with pytest.raises(RuntimeConditionsError, match="fails installed RECORD hash"):
        verify.InstalledBindingVerifier().verify(discovered)


def test_extension_yaml_presentation_can_change_when_source_lock_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("from example_binding import service\n")
    record = verified_distribution(tmp_path)
    path = record.root / "example_binding/runtimeconditions.extension.yaml"
    data = path.read_bytes().replace(b"  kinds:\n", b"  kinds: &declared_kinds\n")
    path.write_bytes(data)
    record.files = [item for item in record.files if str(item) != "example_binding/runtimeconditions.extension.yaml"]
    record.files.append(HashedRecord("example_binding/runtimeconditions.extension.yaml", data))
    release = yaml.safe_load((record.root / "example_binding/runtimeconditions.binding-release.yaml").read_text())
    release["dependencyLock"]["extensions"][0]["sourceSha256"] = hashlib.sha256(data).hexdigest()
    replace_resource(record, "runtimeconditions.binding-release.yaml", release)
    set_distributions(monkeypatch, record)
    discovered = installed.InstalledBindingDiscovery().discover(workload)
    assert len(verify.InstalledBindingVerifier().verify(discovered).packages) == 1


def test_unlisted_import_fails_before_profile_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("from example_binding import unknown\n")
    record = verified_distribution(tmp_path)
    set_distributions(monkeypatch, record)
    output = tmp_path / "profile.yaml"
    status = cli.main([
        "profile", "generate", "--project", str(workload), "--name", "sample",
        "--workload-uri", "example/sample", "--workload-version", "1", "--out", str(output),
    ])
    assert status == 1
    assert "unknown is not a declared symbol" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize(
    ("resource", "change", "expected"),
    [
        (
            "runtimeconditions.binding-model.yaml",
            lambda document: document["vocabulary"]["ownedDeclarations"][0].update(kind="other"),
            "binding model semantic digest mismatch",
        ),
        (
            "runtimeconditions.extension.yaml",
            lambda document: document["spec"]["kinds"][0].update(name="other"),
            "root extension identity or digest mismatch",
        ),
        (
            "runtimeconditions.binding-release.yaml",
            lambda document: document["package"].update(version="9.9.9"),
            "release package identity",
        ),
        (
            "runtimeconditions.binding-release.yaml",
            lambda document: document["dependencyLock"]["extensions"][0].update(sourceSha256="f" * 64),
            "dependency lock source digest mismatch",
        ),
    ],
)
def test_cross_resource_mismatches_fail_even_with_updated_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    resource: str, change: object, expected: str,
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("from example_binding import service\n")
    record = verified_distribution(tmp_path)
    document = yaml.safe_load((record.root / "example_binding" / resource).read_text())
    change(document)
    replace_resource(record, resource, document)
    set_distributions(monkeypatch, record)
    discovery = installed.InstalledBindingDiscovery().discover(workload)
    with pytest.raises(RuntimeConditionsError, match=expected):
        verify.InstalledBindingVerifier().verify(discovery)


def test_installed_dependency_metadata_must_match_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("from example_binding import service\n")
    record = verified_distribution(tmp_path)
    record.requires = ["undeclared-package>=1"]
    set_distributions(monkeypatch, record)
    discovery = installed.InstalledBindingDiscovery().discover(workload)
    with pytest.raises(RuntimeConditionsError, match="Requires-Dist differs"):
        verify.InstalledBindingVerifier().verify(discovery)


def test_direct_binding_dependency_is_resolved_from_installed_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("from example_binding import service\n")
    root_record = verified_distribution(tmp_path)
    dependency_record = verified_distribution(
        tmp_path, name="dependency-binding", import_package="dependency_binding",
        extension_id="https://example.test/dependency:1.0.0", kind="dependency",
    )

    def read(record: DistributionRecord, name: str) -> dict[str, object]:
        return yaml.safe_load((record.root / record.import_package / name).read_text())

    dependency_model = read(dependency_record, "runtimeconditions.binding-model.yaml")
    dependency_root = dependency_model["rootExtension"]
    dependency_extension = read(dependency_record, "runtimeconditions.extension.yaml")
    root_extension = read(root_record, "runtimeconditions.extension.yaml")
    root_extension["spec"]["dependencies"] = [reference_object(parse_identifier(dependency_root))]
    root_schema = verify._schemas()[verify.SCHEMAS["runtimeconditions.extension.yaml"]]
    root_digest = verify._canonical_sha256(verify._semantic_value(root_extension, root_schema, root_schema))
    replace_resource(root_record, "runtimeconditions.extension.yaml", root_extension)

    model = read(root_record, "runtimeconditions.binding-model.yaml")
    model["rootExtension"]["semanticSha256"] = root_digest
    model["extensions"][0]["semanticSha256"] = root_digest
    model["extensions"][0]["dependencies"] = [reference_object(parse_identifier(dependency_root))]
    model["extensions"].append(dependency_root)
    model["dependencyEdges"] = [{"from": reference_object(parse_identifier(model["rootExtension"])), "to": reference_object(parse_identifier(dependency_root))}]
    model["vocabulary"]["ownedDeclarations"][0]["provenance"]["extensionSha256"] = root_digest
    model["vocabulary"]["importedDeclarations"] = [{
        "coordinate": "kind:dependency", "owner": dependency_root["id"],
        "kind": "dependency", "provenance": {
            "owner": dependency_root["id"], "extensionSha256": dependency_root["semanticSha256"],
            "coordinate": "kind:dependency",
        },
    }]
    model["metadata"].pop("semanticSha256")
    model_digest = verify._canonical_sha256(model)
    model["metadata"]["semanticSha256"] = model_digest
    replace_resource(root_record, "runtimeconditions.binding-model.yaml", model)

    manifest = read(root_record, "runtimeconditions.bindings.yaml")
    manifest["model"]["semanticSha256"] = model_digest
    manifest["extension"]["semanticSha256"] = root_digest
    manifest["importedMarkerContracts"] = [{
        "modelRef": {"coordinate": "kind:dependency"},
        "owner": dependency_root["id"], "sourceName": "dependency",
        "markerInterface": "DependencyMarker", "markerMethod": "dependency_marker",
        "providerPackage": "dependency_binding",
    }]
    replace_resource(root_record, "runtimeconditions.bindings.yaml", manifest)

    release = read(root_record, "runtimeconditions.binding-release.yaml")
    release["model"]["semanticSha256"] = model_digest
    release["rootExtension"]["semanticSha256"] = root_digest
    release["dependencyLock"]["extensions"][0]["semanticSha256"] = root_digest
    release["dependencyLock"]["extensions"][0]["sourceSha256"] = hashlib.sha256(
        (root_record.root / "example_binding/runtimeconditions.extension.yaml").read_bytes()
    ).hexdigest()
    release["dependencyLock"]["extensions"][0]["dependencies"] = [reference_object(parse_identifier(dependency_root))]
    release["dependencyLock"]["extensions"].append({
        "id": dependency_root["id"], "version": dependency_root["version"],
        "semanticSha256": dependency_root["semanticSha256"],
        "sourceSha256": hashlib.sha256(
            (dependency_record.root / "dependency_binding/runtimeconditions.extension.yaml").read_bytes()
        ).hexdigest(),
        "sourceBackend": "package", "sourceLocator": "installed:dependency-binding",
    })
    release["packageDependencies"] = [{
        "extension": reference_object(parse_identifier(dependency_root)), "coordinate": "dependency-binding",
        "name": "dependency_binding", "testedVersion": "1.2.3",
        "compatibleVersionRange": {"minimumInclusive": "1.2.3", "nextBreakingExclusive": "2.0.0"},
        "artifact": {"kind": "python-wheel", "sha256": "0" * 64},
    }]
    replace_resource(root_record, "runtimeconditions.binding-release.yaml", release)
    root_record.requires = ["dependency-binding>=1.2.3,<2.0.0"]
    set_distributions(monkeypatch, root_record, dependency_record)

    discovered = installed.InstalledBindingDiscovery().discover(workload)
    verified = verify.InstalledBindingVerifier().verify(discovered)
    assert len(discovered.packages) == 1
    assert [item.installed.distribution for item in verified.packages] == [
        "dependency-binding", "example-binding",
    ]
    assert "dependency_binding" not in sys.modules


def test_digest_algorithm_matches_shared_conformance_models_when_available() -> None:
    conformance = (
        Path(__file__).resolve().parents[2]
        / "extensions/tooling/extension-bindings/model/conformance"
    )
    expected = sorted((conformance / "expected").glob("*/runtimeconditions.binding-model.yaml"))
    if not expected:
        pytest.skip("shared conformance sources are not installed with the profiler")
    for name, bundled in verify._schemas().items():
        assert bundled == yaml.safe_load((conformance.parent / name).read_text()), name
    schema = verify._schemas()[verify.SCHEMAS["runtimeconditions.extension.yaml"]]
    for model_path in expected:
        model = yaml.safe_load(model_path.read_text())
        declared = model["metadata"].pop("semanticSha256")
        assert verify._canonical_sha256(model) == declared, model_path
        source = conformance / "cases" / model_path.parent.name / "root.yaml"
        extension = yaml.safe_load(source.read_text())
        semantic = verify._semantic_value(extension, schema, schema)
        assert verify._canonical_sha256(semantic) == model["rootExtension"]["semanticSha256"], source


def semantic_package(
    extension_id: str, import_package: str, spec: dict[str, object],
    manifest: dict[str, object] | None = None,
) -> verify.VerifiedBindingPackage:
    root = {
        "id": extension_id, "version": "1.0.0",
        "semanticSha256": hashlib.sha256(extension_id.encode()).hexdigest(),
    }
    model = {
        "coreProfileSchema": {
            "id": CORE_ID, "version": CORE_VERSION, "semanticSha256": CORE_SEMANTIC_SHA256,
        },
        "rootExtension": root,
        "extensions": [root],
    }
    installed_package = SimpleNamespace(
        import_package=import_package, distribution=import_package, version="1.0.0",
    )
    return verify.VerifiedBindingPackage(
        installed_package, manifest or {"declarations": [], "rootBindings": [], "types": []},
        model, {"spec": spec}, {},
    )


def semantic_set(*packages: verify.VerifiedBindingPackage) -> verify.VerifiedBindingSet:
    by_id = {parse_identifier(item.model["rootExtension"]): item for item in packages}
    for package in packages:
        seen: set[str] = set()
        pending = [parse_identifier(package.model["rootExtension"])]
        while pending:
            current = pending.pop()
            if current in seen or current not in by_id:
                continue
            seen.add(current)
            pending.extend(parse_identifier(ref) for ref in by_id[current].extension["spec"].get("dependencies", []))
        package.model["extensions"] = [by_id[item].model["rootExtension"] for item in sorted(seen)]
    return verify.VerifiedBindingSet(tuple(packages), tuple(packages))


def base_semantic_package() -> verify.VerifiedBindingPackage:
    extension_id = "https://example.test/base:1.0.0"
    manifest = {
        "declarations": [{
            "modelRef": {"coordinate": "kind:service"}, "owner": extension_id,
            "sourceName": "service", "function": "service",
        }],
        "rootBindings": [{
            "role": "interface", "modelRef": {"coordinate": "schema:service"},
            "declarationCoordinate": "kind:service", "scope": {"kind": "service", "interfaceType": "http"},
            "sourceName": "http", "path": [{"name": "interface"}],
            "value": {"type": "Http"}, "fixedInterfaceType": "http",
        }],
        "types": [{
            "modelRef": {"coordinate": "schema:service"}, "nativeName": "Http",
            "construct": "object", "fields": [],
            "implements": [{"declarationCoordinate": "kind:service"}],
        }],
    }
    spec = {
        "kinds": [{"name": "service"}],
        "interfaceTypes": [{"name": "http", "targetKind": "service"}],
        "schemas": [{
            "id": "base", "appliesToKind": "service", "appliesToInterfaceType": "http",
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object", "required": ["kind", "interface"],
                "properties": {
                    "kind": {"const": "service"},
                    "interface": {"type": "object", "required": ["type"],
                                  "properties": {"type": {"const": "http"}}},
                },
                "additionalProperties": True,
            },
        }],
    }
    return semantic_package(extension_id, "example_binding", spec, manifest)


def test_transitive_marker_reexport_and_scoped_interface_are_static(
    tmp_path: Path,
) -> None:
    leaf_id, middle_id, root_id = (
        "https://example.test/leaf:1.0.0", "https://example.test/middle:1.0.0", "https://example.test/root:1.0.0",
    )
    model_ref = {"coordinate": "kind:worker"}
    declaration = {
        "modelRef": model_ref, "owner": leaf_id, "sourceName": "worker",
        "function": "worker", "markerInterface": "WorkerField", "markerMethod": "worker_marker",
    }
    contract = {key: declaration[key] for key in (
        "modelRef", "owner", "sourceName", "markerInterface", "markerMethod",
    )}
    leaf = semantic_package(leaf_id, "leaf_binding", {"kinds": [{"name": "worker"}]}, {
        "declarations": [declaration], "importedMarkerContracts": [], "rootBindings": [], "types": [],
    })
    middle = semantic_package(middle_id, "middle_binding", {
        "dependencies": [{"id": leaf_id, "version": "1.0.0"}], "interfaceTypes": [{"name": "process", "targetKind": "worker"}],
    }, {
        "declarations": [], "importedMarkerContracts": [{**contract, "providerPackage": "leaf_binding"}],
        "rootBindings": [], "types": [],
    })
    root = semantic_package(root_id, "root_binding", {
        "dependencies": [{"id": middle_id, "version": "1.0.0"}],
        "conditionFields": [{"name": "command", "appliesToKinds": ["worker"],
                             "appliesToInterfaceTypes": ["process"]}],
    }, {
        "declarations": [], "importedMarkerContracts": [{**contract, "providerPackage": "middle_binding"}],
        "rootBindings": [{
            "role": "condition-field", "modelRef": {"coordinate": "schema:command"},
            "declarationCoordinate": "kind:worker", "scope": {"kind": "worker", "interfaceType": "process"},
            "path": [{"name": "command"}], "value": {"type": "Command"},
        }],
        "types": [{
            "modelRef": {"coordinate": "schema:command"}, "nativeName": "Command",
            "construct": "wrapper", "fields": [{
                "modelRef": {"coordinate": "schema:command"}, "nativeName": "value",
                "sourceName": "command", "required": True, "value": {"builtin": "str"},
            }],
            "implements": [{"declarationCoordinate": "kind:worker"}],
        }],
    })
    bindings = semantic_set(leaf, middle, root)
    provider, resolved = verify.resolve_marker_declaration(
        root.manifest["importedMarkerContracts"][0], bindings.packages,
        {parse_identifier(item) for item in root.model["extensions"]},
    )
    assert provider is leaf and resolved is declaration
    (tmp_path / "app.py").write_text(
        "import root_binding as b\nb.worker(b.Command(value='sample'))\n",
    )
    extracted = GeneratedBindingExtractor(bindings).extract(tmp_path)
    assert extracted[0].condition == {
        "kind": "worker", "command": "sample", "interface": {"type": "process"},
    }
    profile = GeneratedProfileValidator(bindings).build(extracted, "sample", "example/sample", "1")
    assert profile["extensions"] == [{"id": item, "version": "1.0.0"} for item in sorted((leaf_id, middle_id, root_id))]

    middle.manifest["importedMarkerContracts"][0]["providerPackage"] = "root_binding"
    with pytest.raises(RuntimeConditionsError, match="provider cycle"):
        verify.resolve_marker_declaration(
            root.manifest["importedMarkerContracts"][0], bindings.packages,
            {parse_identifier(item) for item in root.model["extensions"]},
        )


def conformance_binding_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> tuple[verify.VerifiedBindingSet, tuple[ExtractedCondition, ...], dict[str, object]]:
    # In-memory model contract check; installable fixtures are maintained separately.
    tooling = Path(__file__).resolve().parents[2] / "extensions/tooling/extension-bindings"
    model_file = tooling / "model/conformance/expected" / case / "runtimeconditions.binding-model.yaml"
    if not model_file.is_file():
        pytest.skip("shared conformance model is unavailable")
    monkeypatch.syspath_prepend(str(tooling / "emitters/python/src"))
    from runtimeconditions_binding_emitter import build_plan, load_model, load_target, render_resources
    from runtimeconditions_binding_emitter.source import _conformance_source

    model = load_model(model_file)
    target = replace(
        load_target(tooling / "emitters/python/testdata/package-target.yaml"),
        root_extension=model["rootExtension"]["id"],
    )
    plan = build_plan(model, target)
    prefix = f"src/{target.import_package}/"
    manifest = yaml.safe_load(render_resources(plan, model)[prefix + "runtimeconditions.bindings.yaml"])
    source = _conformance_source(plan, model).replace(
        "from . import bindings as b", f"import {target.import_package} as b",
    )
    (tmp_path / "app.py").write_text(source)
    extension = yaml.safe_load((tooling / "model/conformance/cases" / case / "root.yaml").read_text())
    installed_package = SimpleNamespace(
        import_package=target.import_package, distribution=target.distribution_name, version=target.version,
    )
    package = verify.VerifiedBindingPackage(installed_package, manifest, model, extension, {})
    bindings = verify.VerifiedBindingSet((package,), (package,))
    extracted = GeneratedBindingExtractor(bindings).extract(tmp_path)
    return bindings, extracted, model


@pytest.mark.parametrize(
    ("case", "count"),
    [
        ("01-owned-kind-interface", 1),
        ("06-recursive-reference", 1),
        ("07-object-alternatives", 2),
        ("08-heterogeneous-union", 1),
        ("09-collections-and-maps", 1),
        ("10-scoped-domains-collisions", 2),
        ("11-source-name-preservation", 1),
    ],
)
def test_generated_conformance_conditions_pass_complete_semantic_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, count: int,
) -> None:
    bindings, extracted, model = conformance_binding_case(tmp_path, monkeypatch, case)
    profile = GeneratedProfileValidator(bindings).build(extracted, "sample", "example/sample", "1")
    assert len(profile["conditions"]) == count
    assert all(item["interface"]["type"] for item in profile["conditions"])
    if case == "01-owned-kind-interface":
        assert profile["conditions"] == [{
            "kind": "service", "interface": {"type": "http", "endpoint": "sample"},
            "region": "sample",
        }]
    assert profile["extensions"] == [reference_object(parse_identifier(model["rootExtension"]))]


@pytest.mark.parametrize(
    ("case", "field", "value", "message"),
    [
        ("07-object-alternatives", "configuration", {}, "extension schema"),
        ("08-heterogeneous-union", "target", {"id": 1}, "extension schema"),
        ("10-scoped-domains-collisions", "mode", "direct", "outside the owned domain"),
    ],
)
def test_generated_conformance_branch_constraints_reject_invalid_conditions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    case: str, field: str, value: object, message: str,
) -> None:
    bindings, extracted, _ = conformance_binding_case(tmp_path, monkeypatch, case)
    condition = {**extracted[0].condition, field: value}
    invalid = replace(extracted[0], condition=condition)
    with pytest.raises(RuntimeConditionsError) as failure:
        GeneratedProfileValidator(bindings).build((invalid,), "sample", "example/sample", "1")
    assert message in str(failure.value)
    assert "/app.py:" in str(failure.value)


def semantic_candidate(condition: dict[str, object], *extensions: str) -> ExtractedCondition:
    return ExtractedCondition(Path("/workload/app.py"), 3, 1, condition, tuple((item, "1.0.0") for item in extensions))


def test_semantic_validator_emits_direct_ids_and_uses_transitive_schema_closure() -> None:
    base = base_semantic_package()
    middle = semantic_package("https://example.test/middle:1.0.0", "middle_binding", {
        "dependencies": [{"id": "https://example.test/base:1.0.0", "version": "1.0.0"}],
        "schemas": [{"id": "middle", "appliesToKind": "service", "schema": {
            "type": "object", "properties": {"interface": {"type": "object"}},
        }}],
    })
    addon = semantic_package("https://example.test/addon:1.0.0", "addon_binding", {
        "dependencies": [{"id": "https://example.test/middle:1.0.0", "version": "1.0.0"}],
        "conditionFields": [{"name": "extra", "appliesToKinds": ["service"]}],
        "schemas": [{"id": "addon", "appliesToKind": "service", "schema": {
            "type": "object", "required": ["extra"],
            "properties": {"extra": {"const": "enabled"}},
        }}],
    })
    bindings = semantic_set(base, middle, addon)
    candidate = semantic_candidate(
        {"kind": "service", "interface": {"type": "http"}, "extra": "enabled"},
        "https://example.test/base:1.0.0", "https://example.test/addon:1.0.0",
    )
    profile = GeneratedProfileValidator(bindings).build((candidate,), "sample", "example/sample", "1")
    assert profile["extensions"] == [{"id": item, "version": "1.0.0"} for item in ["https://example.test/addon:1.0.0", "https://example.test/base:1.0.0"]]
    assert "https://example.test/middle:1.0.0" not in profile["extensions"]

    addon.extension["spec"]["schemas"][0]["schema"]["properties"]["extra"]["const"] = "disabled"
    with pytest.raises(RuntimeConditionsError, match=r"extension schema \('https://example.test/addon:1.0.0', '1.0.0'\)/addon"):
        GeneratedProfileValidator(bindings).build((candidate,), "sample", "example/sample", "1")


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ({"kind": "service"}, "core schema"),
        ({"kind": "service", "interface": {"type": "http"}, "rogue": 1}, "condition field 'rogue'"),
        ({"kind": "unknown", "interface": {"type": "http"}}, "exactly one vocabulary owner"),
    ],
)
def test_semantic_errors_are_source_located(condition: dict[str, object], message: str) -> None:
    bindings = semantic_set(base_semantic_package())
    candidate = semantic_candidate(condition, "https://example.test/base:1.0.0")
    with pytest.raises(RuntimeConditionsError) as failure:
        GeneratedProfileValidator(bindings).build((candidate,), "sample", "example/sample", "1")
    assert "/workload/app.py:3:1: conditions[0]" in str(failure.value)
    assert message in str(failure.value)


def test_semantic_validator_rejects_core_identity_and_missing_dependency() -> None:
    base = base_semantic_package()
    base.model["coreProfileSchema"]["semanticSha256"] = "0" * 64
    with pytest.raises(RuntimeConditionsError, match="core profile schema identity or digest mismatch"):
        GeneratedProfileValidator(semantic_set(base))
    base.model["coreProfileSchema"]["semanticSha256"] = CORE_SEMANTIC_SHA256
    base.extension["spec"]["dependencies"] = [{"id": "https://example.test/missing:1.0.0", "version": "1.0.0"}]
    candidate = semantic_candidate({"kind": "service", "interface": {"type": "http"}}, "https://example.test/base:1.0.0")
    with pytest.raises(RuntimeConditionsError, match="no verified installed binding package"):
        GeneratedProfileValidator(semantic_set(base)).build((candidate,), "sample", "example/sample", "1")


def test_semantic_validator_checks_complete_core_profile_and_duplicate_names() -> None:
    bindings = semantic_set(base_semantic_package())
    condition = {"kind": "service", "interface": {"type": "http"}, "name": "same"}
    candidate = semantic_candidate(condition, "https://example.test/base:1.0.0")
    with pytest.raises(RuntimeConditionsError, match="core schema"):
        GeneratedProfileValidator(bindings).build((candidate,), "", "example/sample", "1")
    with pytest.raises(RuntimeConditionsError, match="duplicate Condition name") as failure:
        GeneratedProfileValidator(bindings).build((candidate, candidate), "sample", "example/sample", "1")
    assert "/workload/app.py:3:1: conditions[1]/name" in str(failure.value)


def test_semantic_validator_checks_scoped_values_and_rejects_external_schema_refs() -> None:
    base = base_semantic_package()
    base.extension["spec"]["interfaceFields"] = [
        {"name": "mode", "targetKind": "service", "targetType": "http"},
    ]
    base.extension["spec"]["fieldValues"] = [
        {"field": "interface.mode", "targetKind": "service", "targetType": "http",
         "values": ["direct", "proxy"]},
    ]
    bindings = semantic_set(base)
    invalid = semantic_candidate(
        {"kind": "service", "interface": {"type": "http", "mode": "unknown"}},
        "https://example.test/base:1.0.0",
    )
    with pytest.raises(RuntimeConditionsError, match="outside the owned domain"):
        GeneratedProfileValidator(bindings).build((invalid,), "sample", "example/sample", "1")
    valid = semantic_candidate(
        {"kind": "service", "interface": {"type": "http", "mode": "direct"}},
        "https://example.test/base:1.0.0",
    )
    assert GeneratedProfileValidator(bindings).build((valid,), "sample", "example/sample", "1")["conditions"]
    base.extension["spec"]["schemas"][0]["schema"] = {"$ref": "https://example.invalid/missing"}
    with pytest.raises(RuntimeConditionsError, match="could not resolve"):
        GeneratedProfileValidator(bindings).build((valid,), "sample", "example/sample", "1")


def test_cli_writes_validated_profile_and_preserves_existing_output_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("import example_binding as b\nb.service(b.Http())\n")
    base = base_semantic_package()
    bindings = semantic_set(base)
    monkeypatch.setattr(cli, "verified_installed_bindings", lambda project: bindings)
    output = tmp_path / "profile.yaml"
    args = [
        "profile", "generate", "--project", str(workload), "--name", "sample",
        "--workload-uri", "example/sample", "--workload-version", "1", "--out", str(output),
    ]
    assert cli.main(args) == 0
    profile = yaml.safe_load(output.read_text())
    assert profile["extensions"] == [{"id": item, "version": "1.0.0"} for item in ["https://example.test/base:1.0.0"]]
    assert profile["conditions"] == [{"kind": "service", "interface": {"type": "http"}}]
    assert capsys.readouterr().out == ""

    base.extension["spec"]["schemas"][0]["schema"]["properties"]["interface"]["required"] = ["type", "endpoint"]
    assert cli.main(args) == 1
    assert "extension schema" in capsys.readouterr().err
    assert yaml.safe_load(output.read_text()) == profile
    assert cli.main(args[:-2]) == 1  # stdout mode also validates before emitting.
    assert capsys.readouterr().out == ""


def test_cli_preserves_existing_output_if_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workload = tmp_path / "workload"
    workload.mkdir()
    (workload / "app.py").write_text("import example_binding as b\nb.service(b.Http())\n")
    bindings = semantic_set(base_semantic_package())
    monkeypatch.setattr(cli, "verified_installed_bindings", lambda project: bindings)
    output = tmp_path / "profile.yaml"
    output.write_text("previous result\n")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(cli.os, "replace", fail_replace)
    status = cli.main([
        "profile", "generate", "--project", str(workload), "--name", "sample",
        "--workload-uri", "example/sample", "--workload-version", "1", "--out", str(output),
    ])
    assert status == 1
    assert "replace failed" in capsys.readouterr().err
    assert output.read_text() == "previous result\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["profile.yaml", "workload"]
