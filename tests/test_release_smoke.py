from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.release_artifacts import write_checksums
from scripts.smoke_install import (
    clean_environment,
    smoke,
)


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
        smoke(dist, tmp_path / "smoke.json")


def test_smoke_rejects_version_different_from_tag_before_installing(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "profiler.whl", "w") as wheel:
        wheel.writestr("profiler-1.0.0.dist-info/METADATA", "Version: 1.0.0\n")
    (dist / "profiler.tar.gz").write_bytes(b"sdist")
    write_checksums(dist)
    with pytest.raises(ValueError, match="expected version 0.0.1, got 1.0.0"):
        smoke(dist, tmp_path / "smoke.json", expected_version="0.0.1")
