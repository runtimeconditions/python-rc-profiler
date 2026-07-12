from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PROFILER_ROOT = ROOT / "python-rc-profiler"
TESTDATA = PROFILER_ROOT / "testdata"

sys.path.insert(0, str(PROFILER_ROOT))

from runtimeconditions_profiler.main import (  # noqa: E402
    ArtifactDiscovery,
    ArtifactValidator,
    DiscoveryOptions,
    ProfileExtractor,
    ProfileOptions,
    ProfileValidator,
    ProfileYamlWriter,
    ProjectDiscovery,
    RuntimeConditionsError,
)


COMMON = ROOT / "extensions" / "common-integrations" / "python"
ENV = ROOT / "extensions" / "env-configuration" / "python"


def test_authoring_fixtures() -> None:
    fixtures_root = TESTDATA / "authoring"
    for fixture in sorted(path for path in fixtures_root.iterdir() if path.is_dir()):
        config = yaml.safe_load((fixture / "fixture.yaml").read_text())
        result = ProjectDiscovery().discover(fixture, DiscoveryOptions())
        if config["valid"]:
            assert not result.has_errors, f"{fixture.name} should be valid: {messages(result.diagnostics)}"
        else:
            assert result.has_errors, f"{fixture.name} should fail"
            expected = config.get("wantErrorContains")
            if expected:
                assert expected in messages(result.diagnostics)


def test_repository_python_bindings_validate() -> None:
    artifacts = []
    discovery = ArtifactDiscovery()
    artifacts.extend(discovery.discover_path_artifact(COMMON))
    artifacts.extend(discovery.discover_path_artifact(ENV))
    validated = ArtifactValidator().validate(artifacts)
    diagnostics = [diagnostic for artifact in validated for diagnostic in artifact.diagnostics]
    assert diagnostics == []
    assert len(validated) == 2
    common = next(item for item in validated if item.manifest and item.manifest.package == "runtimeconditions_common_integrations")
    env = next(item for item in validated if item.manifest and item.manifest.package == "runtimeconditions_env_configuration")
    assert len(common.manifest.constants) == 10
    assert len(common.manifest.declarations) == 3
    assert len(env.manifest.options) == 2


def test_pyproject_package_path_resolution() -> None:
    result = ProjectDiscovery().discover(
        TESTDATA / "profile-generation" / "declarative-app",
        DiscoveryOptions(resolve_package_paths=True),
    )
    assert not result.has_errors, messages(result.diagnostics)
    assert COMMON.resolve() in result.package_paths
    assert ENV.resolve() in result.package_paths
    assert len(result.validated_artifacts) == 2


@pytest.mark.parametrize(
    ("fixture", "name", "workload_uri", "package_paths", "golden"),
    [
        (
            "declarative-app",
            "python-declarative-app",
            "example/python-declarative-app",
            [COMMON, ENV],
            "declarative-app.golden.yaml",
        ),
        (
            "wildcard-import",
            "python-wildcard-cache",
            "example/python-wildcard-cache",
            [COMMON],
            "wildcard-cache.golden.yaml",
        ),
        (
            "semantic-resolution",
            "python-semantic-resolution",
            "example/python-semantic-resolution",
            [COMMON, ENV],
            "semantic-resolution.golden.yaml",
        ),
        (
            "unused-extension",
            "python-unused-extension",
            "example/python-unused-extension",
            [COMMON, ENV],
            "unused-extension.golden.yaml",
        ),
    ],
)
def test_profile_generation_golden(
    fixture: str,
    name: str,
    workload_uri: str,
    package_paths: list[Path],
    golden: str,
) -> None:
    project = TESTDATA / "profile-generation" / fixture
    profile = extract_profile(project, name, workload_uri, "test", package_paths)
    assert normalize(ProfileYamlWriter.write(profile)) == normalize((TESTDATA / "golden" / golden).read_text())


def test_request_logger_demo_profile_generation() -> None:
    project = ROOT / "rc-demos" / "apps" / "request-logger-http-python"
    profile = extract_profile(
        project,
        "request-logger-http",
        "github.com/runtimeconditions/rc-demos/apps/request-logger-http-python",
        "dev",
        [COMMON, ENV],
    )
    assert normalize(ProfileYamlWriter.write(profile)) == normalize(
        (TESTDATA / "golden" / "request-logger-http-python.golden.yaml").read_text()
    )


def test_generated_profile_validation_rejects_bad_values() -> None:
    project = TESTDATA / "profile-generation" / "declarative-app"
    options = DiscoveryOptions(package_paths=[COMMON, ENV])
    discovery = ProjectDiscovery().discover(project, options)
    profile = ProfileExtractor().extract(
        project,
        ProfileOptions(
            name="python-declarative-app",
            workload_uri="example/python-declarative-app",
            workload_version="test",
            discovery_options=options,
        ),
    )

    unknown_kind = copy.deepcopy(profile)
    unknown_kind["conditions"][0]["kind"] = "worker"
    assert_profile_invalid(unknown_kind, discovery, "conditions[0].kind worker")

    unknown_interface = copy.deepcopy(profile)
    unknown_interface["conditions"][0]["interface"]["type"] = "grpc"
    assert_profile_invalid(unknown_interface, discovery, "conditions[0].interface.type api/grpc")

    invalid_method = copy.deepcopy(profile)
    invalid_method["conditions"][0]["interface"]["operations"][0]["method"] = "FETCH"
    assert_profile_invalid(invalid_method, discovery, "conditions[0].interface.operations[0].method FETCH")

    invalid_env = copy.deepcopy(profile)
    invalid_env["conditions"][0]["configuration"]["env"][0]["property"] = "apiKey"
    assert_profile_invalid(invalid_env, discovery, "conditions[0].configuration.env[0].property apiKey")

    missing_dependency = copy.deepcopy(profile)
    missing_dependency["extensions"] = [
        "https://runtimeconditions.io/extensions/env-configuration/v1alpha1/runtimeconditions.extension.yaml"
    ]
    assert_profile_invalid(
        missing_dependency,
        discovery,
        "extensions missing dependency https://runtimeconditions.io/extensions/common-integrations/v1alpha1/runtimeconditions.extension.yaml",
    )


def test_generate_fails_before_extraction_when_extension_validation_fails() -> None:
    with pytest.raises(RuntimeConditionsError) as exc:
        extract_profile(
            TESTDATA / "profile-generation" / "declarative-app",
            "broken",
            "example/broken",
            "test",
            [TESTDATA / "authoring" / "binding-vocabulary-invalid" / "extension"],
        )
    assert "artifact validation failed" in str(exc.value)


def extract_profile(
    project: Path,
    name: str,
    workload_uri: str,
    workload_version: str,
    package_paths: list[Path],
) -> dict:
    return ProfileExtractor().extract(
        project,
        ProfileOptions(
            name=name,
            workload_uri=workload_uri,
            workload_version=workload_version,
            discovery_options=DiscoveryOptions(package_paths=package_paths),
        ),
    )


def assert_profile_invalid(profile: dict, discovery, expected: str) -> None:
    diagnostics = ProfileValidator().validate(profile, discovery)
    assert expected in messages(diagnostics)


def messages(diagnostics) -> str:
    return "; ".join(diagnostic.message for diagnostic in diagnostics)


def normalize(value: str) -> str:
    return value.replace("\r\n", "\n").strip() + "\n"
