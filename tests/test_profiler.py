from __future__ import annotations

import copy
import hashlib
import shutil
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

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
from runtimeconditions_profiler.models import SDKMappingArtifact  # noqa: E402
from runtimeconditions_profiler.sdk.python import SDKPythonExtractor  # noqa: E402


COMMON = ROOT / "extensions" / "common-integrations" / "python"
ENV = ROOT / "extensions" / "env-configuration" / "python"
KUBERNETES_EXTENSION = ROOT / "extensions" / "kubernetes-api" / "releases" / "0.1.0" / "runtimeconditions.extension.yaml"
KUBERNETES_MAPPING = ROOT / "sdk" / "authorship" / "kubernetes-python" / "mappings" / "runtimeconditions.sdk-mapping.yaml"
KUBERNETES_CONFIGMAP_APP = ROOT / "sdk" / "kubernetes" / "python" / "configmap-reader"
KUBERNETES_WATCH_APP = ROOT / "sdk" / "kubernetes" / "python" / "pod-watcher"
KUBERNETES_DYNAMIC_CONFIGMAP_APP = ROOT / "sdk" / "kubernetes" / "python" / "dynamic-configmap-lifecycle"
KUBERNETES_DYNAMIC_CRD_APP = ROOT / "sdk" / "kubernetes" / "python" / "dynamic-crd-reader"
KUBERNETES_DYNAMIC_WATCH_APP = ROOT / "sdk" / "kubernetes" / "python" / "dynamic-pod-watcher"
AWS_EXTENSION = ROOT / "extensions" / "aws-s3" / "releases" / "0.1.0" / "runtimeconditions.extension.yaml"
AWS_MAPPINGS = {
    "boto3": ROOT / "extensions" / "aws-s3" / "mappings" / "boto3" / "runtimeconditions.sdk-mapping.yaml",
    "botocore": ROOT / "extensions" / "aws-s3" / "mappings" / "botocore" / "runtimeconditions.sdk-mapping.yaml",
    "s3transfer": ROOT / "extensions" / "aws-s3" / "mappings" / "s3transfer" / "runtimeconditions.sdk-mapping.yaml",
}
AWS_APPS = ROOT / "sdk" / "s3" / "python"
AWS_PROFILE_RESULTS = ROOT / "sdk" / "authorship" / "aws-python" / "results" / "profiles"
NATS_EXTENSION = ROOT / "extensions" / "nats-service" / "releases" / "0.1.0" / "runtimeconditions.extension.yaml"
NATS_MAPPING = ROOT / "sdk" / "authorship" / "nats-python" / "mappings" / "runtimeconditions.sdk-mapping.yaml"
NATS_APPS = ROOT / "sdk" / "nats" / "python"
NATS_PROFILE_RESULTS = ROOT / "sdk" / "authorship" / "nats-python" / "results" / "profiles"


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


def test_installed_sdk_mapping_resolves_direct_kubernetes_call_without_bindings() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package_paths = stage_kubernetes_sdk_catalog(root)
        installed = metadata.Distribution.at(root / "site/kubernetes-36.0.3.dist-info")
        before = {name for name in sys.modules if name == "kubernetes" or name.startswith("kubernetes.")}
        with patch("runtimeconditions_profiler.sdk.discovery.metadata.distributions", return_value=[installed]):
            profile = extract_profile(KUBERNETES_CONFIGMAP_APP, "kubernetes-configmap-reader", "example/kubernetes-configmap-reader", "test", [package_paths[1]])
        after = {name for name in sys.modules if name == "kubernetes" or name.startswith("kubernetes.")}

    assert before == after
    assert profile["extensions"] == ["https://runtimeconditions.io/extensions/kubernetes-api/0.1.0/runtimeconditions.extension.yaml"]
    assert profile["conditions"] == [{"kind": "kubernetes", "interface": {"type": "api", "operations": [{"verb": "get", "apiGroup": "", "apiVersion": "v1", "resource": "configmaps", "scope": "namespaced"}]}}]


def test_aws_direct_client_resolves_factory_and_botocore_operation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package_paths = stage_aws_sdk_catalog(root)
        installed = [metadata.Distribution.at(root / f"site/{distribution}-{yaml.safe_load(source.read_text(encoding='utf-8'))['metadata']['distributionVersion']}.dist-info") for distribution, source in AWS_MAPPINGS.items()]
        before = {name for name in sys.modules if name in AWS_MAPPINGS or any(name.startswith(f"{distribution}.") for distribution in AWS_MAPPINGS)}
        with patch("runtimeconditions_profiler.sdk.discovery.metadata.distributions", return_value=installed):
            profile = extract_aws_profile("direct-client", [package_paths[1]])
        after = {name for name in sys.modules if name in AWS_MAPPINGS or any(name.startswith(f"{distribution}.") for distribution in AWS_MAPPINGS)}

    assert before == after
    assert profile["extensions"] == ["https://runtimeconditions.io/extensions/aws-s3/0.1.0/runtimeconditions.extension.yaml"]
    assert profile["conditions"] == [{"kind": "aws.s3", "interface": {"type": "bucket", "operations": [{"name": "PutObject"}]}}]
    assert_aws_profile_golden("direct-client", profile)


@pytest.mark.parametrize("fixture", ["session-client", "factory-wrapper", "dependency-injection", "resource-api"])
def test_aws_put_object_client_flows_resolve_without_application_annotations(fixture: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        package_paths = stage_aws_sdk_catalog(Path(directory))
        profile = extract_aws_profile(fixture, package_paths)

    assert profile["conditions"] == [{"kind": "aws.s3", "interface": {"type": "bucket", "operations": [{"name": "PutObject"}]}}]
    assert_aws_profile_golden(fixture, profile)


def test_aws_dynamic_service_does_not_infer_s3_from_an_unresolved_factory_selector() -> None:
    with tempfile.TemporaryDirectory() as directory:
        package_paths = stage_aws_sdk_catalog(Path(directory))
        profile = extract_aws_profile("dynamic-service", package_paths)

    assert profile["extensions"] == []
    assert profile["conditions"] == []
    assert_aws_profile_golden("dynamic-service", profile)


@pytest.mark.parametrize("fixture", ["core-messaging", "jetstream-publisher", "jetstream-consumer", "key-value", "object-store", "complete-service"])
def test_nats_python_typed_state_flows_match_reviewed_profiles(fixture: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        package_paths = stage_nats_sdk_catalog(Path(directory))
        profile = extract_profile(NATS_APPS / fixture, f"nats-python-{fixture}", f"https://github.com/runtimeconditions/sdk-authorship-discovery/tree/main/nats/python/{fixture}", "0.1.0", package_paths)

    assert normalize(ProfileYamlWriter.write(profile)) == normalize((NATS_PROFILE_RESULTS / f"{fixture}.yaml").read_text(encoding="utf-8"))


def test_nats_python_does_not_emit_an_operation_with_an_unresolved_required_binding() -> None:
    source = """import nats

async def run(subject: str) -> None:
    client = await nats.connect()
    await client.publish(subject, b\"payload\")
"""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        (project / "app.py").write_text(source, encoding="utf-8")
        profile = extract_profile(project, "nats-dynamic-subject", "example/nats-dynamic-subject", "test", stage_nats_sdk_catalog(root / "catalog"))

    assert profile["conditions"] == [{"kind": "nats", "interface": {"type": "service", "operations": [{"resource": "connection", "action": "connect"}]}}]


def test_generic_typed_value_bindings_fall_through_and_omit_explicit_none() -> None:
    source = """import nats
from nats.js.api import ConsumerConfig, StreamConfig

async def run() -> None:
    client = await nats.connect()
    jetstream = client.jetstream()
    await jetstream.add_stream(StreamConfig(name="ORDERS", subjects=None))
    await jetstream.add_consumer("ORDERS", ConsumerConfig(name=None, durable_name="ORDERS_WORKER"))
"""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        (project / "app.py").write_text(source, encoding="utf-8")
        profile = extract_profile(project, "nats-explicit-none", "example/nats-explicit-none", "test", stage_nats_sdk_catalog(root / "catalog"))

    assert profile["conditions"] == [{"kind": "nats", "interface": {"type": "service", "operations": [
        {"resource": "connection", "action": "connect"},
        {"resource": "stream", "action": "create", "name": "ORDERS"},
        {"resource": "consumer", "action": "create", "stream": "ORDERS", "name": "ORDERS_WORKER"},
    ]}}]


def test_generic_typed_state_call_can_resolve_multiple_canonical_operations() -> None:
    source = """import nats

async def run() -> None:
    client = await nats.connect()
    await client.combined(\"events.created\")
"""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package_paths = stage_nats_sdk_catalog(root / "catalog")
        project = root / "project"
        project.mkdir()
        source_path = project / "app.py"
        source_path.write_text(source, encoding="utf-8")
        discovery = ProjectDiscovery().discover(project, DiscoveryOptions(package_paths=package_paths, discover_installed_sdk_mappings=False))
        original = discovery.sdk_mappings[0]
        document = copy.deepcopy(original.mapping)
        document["python"]["calls"].append(
            {
                "id": "combined",
                "symbols": [{"module": "example", "class": "CombinedClient", "method": "combined"}],
                "receiverState": "nats.connection",
                "operations": [
                    {"operationRef": "subject.publish", "operationBindings": {"subject": {"argument": {"position": 0, "keyword": "subject"}}}},
                    {"operationRef": "subject.subscribe", "operationBindings": {"subject": {"argument": {"position": 0, "keyword": "subject"}}}},
                ],
            }
        )
        mapping = SDKMappingArtifact(original.distribution, original.distribution_version, original.name, original.index_path, original.mapping_path, original.mapping_sha256, document)
        conditions, extensions = SDKPythonExtractor([mapping], discovery.sdk_extensions).extract([source_path], project)

    assert extensions == ["https://runtimeconditions.io/extensions/nats-service/0.1.0/runtimeconditions.extension.yaml"]
    assert conditions == [{"kind": "nats", "interface": {"type": "service", "operations": [{"resource": "connection", "action": "connect"}, {"resource": "subject", "action": "publish", "subject": "events.created"}, {"resource": "subject", "action": "subscribe", "subject": "events.created"}]}}]


def test_aws_managed_transfer_composes_all_mapped_runtime_paths() -> None:
    with tempfile.TemporaryDirectory() as directory:
        package_paths = stage_aws_sdk_catalog(Path(directory))
        profile = extract_aws_profile("managed-transfer", package_paths)

    assert profile["conditions"] == [{"kind": "aws.s3", "interface": {"type": "bucket", "operations": [
        {"name": "PutObject"},
        {"name": "CreateMultipartUpload"},
        {"name": "UploadPart"},
        {"name": "CompleteMultipartUpload"},
        {"name": "AbortMultipartUpload"},
    ]}}]
    assert_aws_profile_golden("managed-transfer", profile)


def test_aws_factory_fails_closed_when_an_owner_mapping_dependency_is_absent() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package_paths = stage_aws_sdk_catalog(root)
        (root / "site/s3transfer/runtimeconditions/index.yaml").unlink()
        with pytest.raises(RuntimeConditionsError) as exc:
            extract_aws_profile("direct-client", package_paths)

    assert "required SDK mapping s3transfer/s3transfer.aws.s3 was not discovered" in str(exc.value)


def test_sdk_condition_delegation_resolves_watch_without_a_generic_wrapper_condition() -> None:
    with tempfile.TemporaryDirectory() as directory:
        package_paths = stage_kubernetes_sdk_catalog(Path(directory))
        profile = extract_profile(KUBERNETES_WATCH_APP, "kubernetes-pod-watcher", "example/kubernetes-pod-watcher", "test", package_paths)

    assert profile["conditions"] == [{"kind": "kubernetes", "interface": {"type": "api", "operations": [{"verb": "watch", "apiGroup": "", "apiVersion": "v1", "resource": "pods", "scope": "namespaced"}]}}]


def test_dynamic_client_resolves_distinct_built_in_resource_operations() -> None:
    with tempfile.TemporaryDirectory() as directory:
        package_paths = stage_kubernetes_sdk_catalog(Path(directory))
        profile = extract_profile(KUBERNETES_DYNAMIC_CONFIGMAP_APP, "kubernetes-dynamic-configmaps", "example/kubernetes-dynamic-configmaps", "test", package_paths)

    assert profile["conditions"] == [{"kind": "kubernetes", "interface": {"type": "api", "operations": [
        {"verb": "create", "apiGroup": "", "apiVersion": "v1", "resource": "configmaps", "scope": "namespaced"},
        {"verb": "get", "apiGroup": "", "apiVersion": "v1", "resource": "configmaps", "scope": "namespaced"},
        {"verb": "list", "apiGroup": "", "apiVersion": "v1", "resource": "configmaps", "scope": "namespaced"},
        {"verb": "patch", "apiGroup": "", "apiVersion": "v1", "resource": "configmaps", "scope": "namespaced"},
        {"verb": "delete", "apiGroup": "", "apiVersion": "v1", "resource": "configmaps", "scope": "namespaced"},
    ]}}]


def test_dynamic_client_watch_is_one_state_bound_operation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        package_paths = stage_kubernetes_sdk_catalog(Path(directory))
        profile = extract_profile(KUBERNETES_DYNAMIC_WATCH_APP, "kubernetes-dynamic-pod-watcher", "example/kubernetes-dynamic-pod-watcher", "test", package_paths)

    assert profile["conditions"] == [{"kind": "kubernetes", "interface": {"type": "api", "operations": [{"verb": "watch", "apiGroup": "", "apiVersion": "v1", "resource": "pods", "scope": "namespaced"}]}}]


def test_dynamic_client_unmodeled_crd_emits_no_invented_resource_condition() -> None:
    with tempfile.TemporaryDirectory() as directory:
        package_paths = stage_kubernetes_sdk_catalog(Path(directory))
        profile = extract_profile(KUBERNETES_DYNAMIC_CRD_APP, "kubernetes-dynamic-crd", "example/kubernetes-dynamic-crd", "test", package_paths)

    assert profile["extensions"] == []
    assert profile["conditions"] == []


def test_dynamic_client_unresolved_selector_emits_nothing() -> None:
    profile = extract_kubernetes_source_profile("from kubernetes import client, dynamic\n\ndef resources(api_version, kind):\n    resource = dynamic.DynamicClient(client.ApiClient()).resources.get(api_version=api_version, kind=kind)\n    return resource.get(namespace='default')\n")

    assert profile["extensions"] == []
    assert profile["conditions"] == []


def test_dynamic_client_derives_cluster_and_all_namespace_scopes_from_resource_state() -> None:
    profile = extract_kubernetes_source_profile("from kubernetes import client, dynamic\n\ndef resources():\n    dynamic_client = dynamic.DynamicClient(client.ApiClient())\n    nodes = dynamic_client.resources.get(api_version='v1', kind='Node')\n    configmaps = dynamic_client.resources.get(api_version='v1', kind='ConfigMap')\n    nodes.get(name='worker-1')\n    return configmaps.get()\n")

    assert profile["conditions"][0]["interface"]["operations"] == [
        {"verb": "get", "apiGroup": "", "apiVersion": "v1", "resource": "nodes", "scope": "cluster"},
        {"verb": "list", "apiGroup": "", "apiVersion": "v1", "resource": "configmaps", "scope": "all_namespaces"},
    ]


def test_dynamic_client_branches_on_meaningful_arguments_without_inventing_invalid_delete() -> None:
    profile = extract_kubernetes_source_profile("from kubernetes import client, dynamic\n\ndef resources():\n    resource = dynamic.DynamicClient(client.ApiClient()).resources.get(api_version='v1', kind='ConfigMap')\n    resource.get(name=None, namespace='default')\n    resource.delete(label_selector='app=example', namespace='default')\n    resource.delete(namespace='default')\n")

    assert profile["conditions"][0]["interface"]["operations"] == [
        {"verb": "list", "apiGroup": "", "apiVersion": "v1", "resource": "configmaps", "scope": "namespaced"},
        {"verb": "deletecollection", "apiGroup": "", "apiVersion": "v1", "resource": "configmaps", "scope": "namespaced"},
    ]


def test_sdk_mapping_extraction_is_additive_to_existing_declarative_bindings() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        shutil.copytree(TESTDATA / "profile-generation" / "declarative-app", project)
        app = project / "src/app.py"
        app.write_text(app.read_text(encoding="utf-8") + "\n\nfrom kubernetes import client\n\ndef read_config_map():\n    return client.CoreV1Api().read_namespaced_config_map(name='settings', namespace='default')\n", encoding="utf-8")
        sdk_paths = stage_kubernetes_sdk_catalog(root / "catalog")
        profile = extract_profile(project, "mixed", "example/mixed", "test", [COMMON, ENV, *sdk_paths])

    assert len(profile["conditions"]) == 3
    assert {condition["kind"] for condition in profile["conditions"]} == {"api", "cache", "kubernetes"}
    assert profile["extensions"] == [
        "https://runtimeconditions.io/extensions/common-integrations/v1alpha1/runtimeconditions.extension.yaml",
        "https://runtimeconditions.io/extensions/env-configuration/v1alpha1/runtimeconditions.extension.yaml",
        "https://runtimeconditions.io/extensions/kubernetes-api/0.1.0/runtimeconditions.extension.yaml",
    ]


def test_direct_list_call_remains_list_without_the_wrapper_transformation() -> None:
    profile = extract_kubernetes_source_profile("from kubernetes import client\n\ndef pods():\n    api = client.CoreV1Api()\n    return api.list_namespaced_pod(namespace='payments')\n")

    assert profile["conditions"][0]["interface"]["operations"] == [{"verb": "list", "apiGroup": "", "apiVersion": "v1", "resource": "pods", "scope": "namespaced"}]


def test_watch_stream_preserves_a_delegated_operation_without_a_matching_conditional() -> None:
    profile = extract_kubernetes_source_profile("from kubernetes import client, watch\n\ndef logs():\n    api = client.CoreV1Api()\n    return watch.Watch().stream(api.read_namespaced_pod_log, name='worker', namespace='payments')\n")

    assert profile["conditions"][0]["interface"]["operations"] == [{"verb": "get", "apiGroup": "", "apiVersion": "v1", "resource": "pods", "scope": "namespaced", "subresource": "log"}]


def test_unresolved_delegated_callable_does_not_emit_a_broad_sdk_condition() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package_paths = stage_kubernetes_sdk_catalog(root / "catalog")
        project = root / "project"
        (project / "src").mkdir(parents=True)
        (project / "src/app.py").write_text("from kubernetes import watch\n\ndef events(func):\n    return watch.Watch().stream(func, namespace='default')\n", encoding="utf-8")
        profile = extract_profile(project, "unresolved-watch", "example/unresolved-watch", "test", package_paths)

    assert profile["extensions"] == []
    assert profile["conditions"] == []


def test_tampered_sdk_mapping_is_rejected_before_source_extraction() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package_paths = stage_kubernetes_sdk_catalog(root)
        mapping_path = root / "site/kubernetes/runtimeconditions/mappings/kubernetes-api.yaml"
        mapping_path.write_text(mapping_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with pytest.raises(RuntimeConditionsError) as exc:
            extract_profile(KUBERNETES_CONFIGMAP_APP, "tampered", "example/tampered", "test", package_paths)

    assert "mapping file digest" in str(exc.value)


def test_tampered_sdk_extension_is_rejected_before_source_extraction() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package_paths = stage_kubernetes_sdk_catalog(root)
        extension_path = root / "extensions/kubernetes-api/0.1.0/runtimeconditions.extension.yaml"
        extension = yaml.safe_load(extension_path.read_text(encoding="utf-8"))
        extension["spec"]["kinds"][0]["name"] = "tampered"
        extension_path.write_text(yaml.safe_dump(extension, sort_keys=False), encoding="utf-8")
        with pytest.raises(RuntimeConditionsError) as exc:
            extract_profile(KUBERNETES_CONFIGMAP_APP, "tampered", "example/tampered", "test", package_paths)

    assert "extension semantic digest" in str(exc.value)


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


def extract_aws_profile(fixture: str, package_paths: list[Path]) -> dict:
    return extract_profile(
        AWS_APPS / fixture,
        f"aws-s3-{fixture}",
        f"https://github.com/runtimeconditions/sdk-authorship-discovery/tree/main/s3/python/{fixture}",
        "0.1.0",
        package_paths,
    )


def assert_aws_profile_golden(fixture: str, profile: dict) -> None:
    assert normalize(ProfileYamlWriter.write(profile)) == normalize((AWS_PROFILE_RESULTS / f"{fixture}.yaml").read_text(encoding="utf-8"))


def stage_kubernetes_sdk_catalog(root: Path) -> list[Path]:
    mapping_path = root / "site/kubernetes/runtimeconditions/mappings/kubernetes-api.yaml"
    mapping_path.parent.mkdir(parents=True)
    shutil.copyfile(KUBERNETES_MAPPING, mapping_path)
    mapping_digest = hashlib.sha256(mapping_path.read_bytes()).hexdigest()
    index = {
        "apiVersion": "runtimeconditions.io/sdk-mapping/v1alpha1",
        "kind": "RuntimeConditionsSDKMappingIndex",
        "metadata": {"distribution": "kubernetes", "distributionVersion": "36.0.3", "language": "python"},
        "mappings": [{"name": "kubernetes.api", "service": "kubernetes-api", "path": "kubernetes/runtimeconditions/mappings/kubernetes-api.yaml", "sha256": mapping_digest}],
    }
    index_path = root / "site/kubernetes/runtimeconditions/index.yaml"
    index_path.write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    extension_path = root / "extensions/kubernetes-api/0.1.0/runtimeconditions.extension.yaml"
    extension_path.parent.mkdir(parents=True)
    shutil.copyfile(KUBERNETES_EXTENSION, extension_path)
    dist_info = root / "site/kubernetes-36.0.3.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: kubernetes\nVersion: 36.0.3\n", encoding="utf-8")
    (dist_info / "RECORD").write_text("kubernetes/runtimeconditions/index.yaml,,\nkubernetes/runtimeconditions/mappings/kubernetes-api.yaml,,\n", encoding="utf-8")
    return [root / "site", root / "extensions"]


def stage_aws_sdk_catalog(root: Path) -> list[Path]:
    site = root / "site"
    for distribution, source in AWS_MAPPINGS.items():
        mapping = yaml.safe_load(source.read_text(encoding="utf-8"))
        version = str(mapping["metadata"]["distributionVersion"])
        mapping_path = site / distribution / "runtimeconditions/mappings/aws-s3.yaml"
        mapping_path.parent.mkdir(parents=True)
        shutil.copyfile(source, mapping_path)
        index = {
            "apiVersion": "runtimeconditions.io/sdk-mapping/v1alpha1",
            "kind": "RuntimeConditionsSDKMappingIndex",
            "metadata": {"distribution": distribution, "distributionVersion": version, "language": "python"},
            "mappings": [{"name": mapping["metadata"]["name"], "service": "s3", "path": f"{distribution}/runtimeconditions/mappings/aws-s3.yaml", "sha256": hashlib.sha256(mapping_path.read_bytes()).hexdigest()}],
        }
        index_path = site / distribution / "runtimeconditions/index.yaml"
        index_path.write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
        dist_info = site / f"{distribution}-{version}.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n", encoding="utf-8")
        (dist_info / "RECORD").write_text(f"{distribution}/runtimeconditions/index.yaml,,\n{distribution}/runtimeconditions/mappings/aws-s3.yaml,,\n", encoding="utf-8")
    extension_path = root / "extensions/aws-s3/0.1.0/runtimeconditions.extension.yaml"
    extension_path.parent.mkdir(parents=True)
    shutil.copyfile(AWS_EXTENSION, extension_path)
    return [site, root / "extensions"]


def stage_nats_sdk_catalog(root: Path) -> list[Path]:
    site = root / "site"
    mapping = yaml.safe_load(NATS_MAPPING.read_text(encoding="utf-8"))
    version = str(mapping["metadata"]["distributionVersion"])
    mapping_path = site / "nats/runtimeconditions/mappings/nats-service.yaml"
    mapping_path.parent.mkdir(parents=True)
    shutil.copyfile(NATS_MAPPING, mapping_path)
    index = {
        "apiVersion": "runtimeconditions.io/sdk-mapping/v1alpha1",
        "kind": "RuntimeConditionsSDKMappingIndex",
        "metadata": {"distribution": "nats-py", "distributionVersion": version, "language": "python"},
        "mappings": [{"name": mapping["metadata"]["name"], "service": "nats", "path": "nats/runtimeconditions/mappings/nats-service.yaml", "sha256": hashlib.sha256(mapping_path.read_bytes()).hexdigest()}],
    }
    index_path = site / "nats/runtimeconditions/index.yaml"
    index_path.write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    extension_path = root / "extensions/nats-service/0.1.0/runtimeconditions.extension.yaml"
    extension_path.parent.mkdir(parents=True)
    shutil.copyfile(NATS_EXTENSION, extension_path)
    return [site, root / "extensions"]


def extract_kubernetes_source_profile(source: str) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package_paths = stage_kubernetes_sdk_catalog(root / "catalog")
        project = root / "project"
        (project / "src").mkdir(parents=True)
        (project / "src/app.py").write_text(source, encoding="utf-8")
        return extract_profile(project, "kubernetes-source", "example/kubernetes-source", "test", package_paths)


def assert_profile_invalid(profile: dict, discovery, expected: str) -> None:
    diagnostics = ProfileValidator().validate(profile, discovery)
    assert expected in messages(diagnostics)


def messages(diagnostics) -> str:
    return "; ".join(diagnostic.message for diagnostic in diagnostics)


def normalize(value: str) -> str:
    return value.replace("\r\n", "\n").strip() + "\n"
