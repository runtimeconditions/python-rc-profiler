from __future__ import annotations

import base64
import csv
import hashlib
import io
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts.release_artifacts import sha256, write_checksums
from scripts.smoke_install import (
    DEPENDENCY_ID,
    ROOT_ID,
    binding_wheels,
    clean_environment,
    smoke,
)

SOURCE = Path(__file__).resolve().parents[1]


def test_smoke_fixtures_have_recorded_resources_and_dependency_provenance(tmp_path: Path) -> None:
    profiler = tmp_path / "profiler.whl"
    profiler.write_bytes(b"profiler artifact")
    wheels = binding_wheels(SOURCE / "testdata/release-smoke", tmp_path / "bindings", profiler, "8.0.0")
    assert len(wheels) == 2
    releases = {}
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            record = next(name for name in archive.namelist() if name.endswith(".dist-info/RECORD"))
            rows = list(csv.reader(io.StringIO(archive.read(record).decode())))
            assert {row[0] for row in rows} == set(archive.namelist())
            for name, digest, size in rows:
                if name == record:
                    assert digest == size == ""
                    continue
                data = archive.read(name)
                expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                assert digest == f"sha256={expected}" and size == str(len(data))
            resource = next(name for name in archive.namelist() if name.endswith("/runtimeconditions.binding-release.yaml"))
            release = yaml.safe_load(archive.read(resource))
            identity = release["rootExtension"]["id"]
            releases[identity] = (release, wheel)
            assert release["provenance"]["profiler"] == {"name": "python-rc-profiler", "version": "8.0.0", "sha256": sha256(profiler)}
            assert release["provenance"]["mode"] == "test-fixture"
            init = resource.rsplit("/", 1)[0] + "/__init__.py"
            assert b"raise RuntimeError" in archive.read(init)
    root, _ = releases[ROOT_ID]
    dependency, dependency_wheel = releases[DEPENDENCY_ID]
    assert root["packageDependencies"][0]["artifact"]["sha256"] == sha256(dependency_wheel)
    assert dependency["packageDependencies"] == []
    assert {entry["id"] for entry in root["dependencyLock"]["extensions"]} == {ROOT_ID, DEPENDENCY_ID}


def test_smoke_removes_ambient_python_and_pip_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    names = (
        "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_INDEX_URL",
        "SETUPTOOLS_SCM_PRETEND_VERSION",
        "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_RUNTIMECONDITIONS_PROFILER",
        "VCS_VERSIONING_PRETEND_VERSION_FOR_RUNTIMECONDITIONS_PROFILER",
    )
    for name in names:
        monkeypatch.setenv(name, "checkout-or-private-index")
    environment = clean_environment()
    assert all(name not in environment for name in names)
    assert environment["PYTHONNOUSERSITE"] == "1"


def test_smoke_rejects_tampered_artifact_before_installing(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "profiler.whl"
    wheel.write_bytes(b"original")
    (dist / "profiler.tar.gz").write_bytes(b"original sdist")
    write_checksums(dist)
    wheel.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        smoke(dist, tmp_path / "missing-fixtures", tmp_path / "smoke.json")


def test_smoke_rejects_version_different_from_tag_before_installing(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "profiler.whl", "w") as wheel:
        wheel.writestr("profiler-1.0.0.dist-info/METADATA", "Version: 1.0.0\n")
    (dist / "profiler.tar.gz").write_bytes(b"sdist")
    write_checksums(dist)
    with pytest.raises(ValueError, match="expected version 0.0.1, got 1.0.0"):
        smoke(dist, tmp_path / "missing-fixtures", tmp_path / "smoke.json", expected_version="0.0.1")
