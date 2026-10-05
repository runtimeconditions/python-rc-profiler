from __future__ import annotations

import tempfile
from pathlib import Path

from runtimeconditions_profiler.main import DiscoveryOptions, ProfileExtractor, ProfileOptions


ROOT = Path(__file__).resolve().parents[2]
MAPPING = ROOT / "sdk/registry/mappings/pypi/boto3/aws-s3/runtimeconditions.sdk-mapping.yaml"
EXTENSION = ROOT / "extensions/catalog/aws/s3/releases/0.2.0/runtimeconditions.extension.yaml"
S3_APPS = ROOT / "sdk/s3/python"


def extract(project: Path) -> dict:
    return ProfileExtractor().extract(
        project,
        ProfileOptions(
            name=project.name,
            workload_uri="example/test",
            workload_version="test",
            discovery_options=DiscoveryOptions(
                mapping_paths=[MAPPING],
                extension_paths=[EXTENSION],
            ),
        ),
    )


def test_direct_put_object_omits_unknown_optional_bucket_name() -> None:
    profile = extract(S3_APPS / "direct-client")
    assert profile["extensions"] == [
        "https://runtimeconditions.io/aws/aws-s3:0.2.0"
    ]
    assert profile["conditions"] == [
        {
            "kind": "aws.s3",
            "interface": {"type": "bucket", "operations": [{"name": "PutObject"}]},
        }
    ]


def test_managed_upload_emits_the_union_of_possible_canonical_operations() -> None:
    profile = extract(S3_APPS / "managed-transfer")
    assert profile["conditions"][0]["interface"]["operations"] == [
        {"name": "PutObject"},
        {"name": "CreateMultipartUpload"},
        {"name": "UploadPart"},
        {"name": "CompleteMultipartUpload"},
        {"name": "AbortMultipartUpload"},
    ]


def test_resource_bucket_identity_flows_to_put_object() -> None:
    profile = extract(S3_APPS / "resource-api")
    assert profile["conditions"] == [
        {
            "kind": "aws.s3",
            "interface": {"type": "bucket", "operations": [{"name": "PutObject"}]},
        }
    ]


def test_dynamic_service_selector_remains_silent() -> None:
    profile = extract(S3_APPS / "dynamic-service")
    assert profile["extensions"] == []
    assert profile["conditions"] == []


def test_session_factory_symbol_is_matched_without_executing_boto3() -> None:
    source = """import boto3

def upload(bucket: str) -> None:
    session = boto3.Session()
    client = session.client(service_name=\"s3\")
    client.put_object(Bucket=bucket, Key=\"one\", Body=b\"1\")
"""
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        (project / "app.py").write_text(source, encoding="utf-8")
        profile = extract(project)
    assert profile["conditions"][0]["interface"]["operations"] == [{"name": "PutObject"}]


def test_two_static_buckets_emit_two_named_conditions() -> None:
    source = """import boto3

def upload() -> None:
    client = boto3.client(\"s3\")
    client.put_object(Bucket=\"invoices\", Key=\"one\", Body=b\"1\")
    client.put_object(Bucket=\"receipts\", Key=\"two\", Body=b\"2\")
"""
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        (project / "app.py").write_text(source, encoding="utf-8")
        profile = extract(project)
    assert profile["conditions"] == [
        {
            "kind": "aws.s3",
            "interface": {
                "type": "bucket",
                "bucketName": "invoices",
                "operations": [{"name": "PutObject"}],
            },
        },
        {
            "kind": "aws.s3",
            "interface": {
                "type": "bucket",
                "bucketName": "receipts",
                "operations": [{"name": "PutObject"}],
            },
        },
    ]


def test_profile_generation_without_mappings_is_valid_and_silent() -> None:
    profile = ProfileExtractor().extract(
        S3_APPS / "direct-client",
        ProfileOptions(
            name="no-mapping",
            workload_uri="example/no-mapping",
            workload_version="test",
            discovery_options=DiscoveryOptions(),
        ),
    )
    assert profile["extensions"] == []
    assert profile["conditions"] == []
