from __future__ import annotations

import io
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.check_distributions import (
    check_distributions,
    check_imports,
    source_payload,
)

SOURCE = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((SOURCE / "pyproject.toml").read_text())["project"]
RESOURCE = "runtimeconditions_profiler/runtimeconditions.profile.v0.2.0.schema.yaml"


def archives(tmp_path: Path, mutate=None) -> tuple[Path, Path]:
    metadata_dir = "runtimeconditions_profiler-0.1.0.dist-info"
    metadata = (
        f"Metadata-Version: 2.1\nName: {PROJECT['name']}\nVersion: {PROJECT['version']}\n"
        f"Requires-Python: {PROJECT['requires-python']}\nDescription-Content-Type: text/markdown\n"
        + "".join(f"Requires-Dist: {item}\n" for item in PROJECT["dependencies"])
        + "\nDescription\n"
    ).encode()
    wheel_files = {
        **source_payload(SOURCE),
        f"{metadata_dir}/METADATA": metadata,
        f"{metadata_dir}/entry_points.txt": (
            b"[console_scripts]\n"
            b"runtimeconditions-python-profiler = runtimeconditions_profiler.cli:main\n"
        ),
    }
    source_files = {**source_payload(SOURCE), "PKG-INFO": metadata}
    extra_files = [SOURCE / name for name in ("README.md", "pyproject.toml", "MANIFEST.in")]
    extra_files.extend((SOURCE / "scripts").glob("*.py"))
    extra_files.extend((SOURCE / "requirements").glob("*.txt"))
    extra_files.extend(path for path in (SOURCE / "testdata/release-smoke").rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    for path in extra_files:
        source_files[path.relative_to(SOURCE).as_posix()] = path.read_bytes()
    if mutate:
        mutate(wheel_files, source_files)
    wheel = tmp_path / "profiler.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in wheel_files.items():
            archive.writestr(name, data)
    sdist = tmp_path / "profiler.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name, data in source_files.items():
            item = tarfile.TarInfo(f"runtimeconditions_profiler-0.1.0/{name}")
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return wheel, sdist


def test_distribution_contents_accept_complete_archives(tmp_path: Path) -> None:
    wheel, sdist = archives(tmp_path)
    result = check_distributions(wheel, sdist, SOURCE)
    assert result["status"] == "passed"
    assert set(result["resources"]) == {"schemas.json", "runtimeconditions.profile.v0.2.0.schema.yaml"}
    assert "referencing>=0.28.4" in result["runtimeDependencies"]


@pytest.mark.parametrize("archive", ["wheel", "sdist"])
@pytest.mark.parametrize("change", ["missing", "altered", "extra"])
def test_distribution_contents_reject_incorrect_runtime_payload(
    tmp_path: Path, archive: str, change: str,
) -> None:
    def mutate(wheel, sdist):
        files = wheel if archive == "wheel" else sdist
        if change == "missing":
            files.pop(RESOURCE)
        elif change == "altered":
            files[RESOURCE] = b"invalid schema"
        else:
            files["runtimeconditions_profiler/stale.py"] = b""

    wheel, sdist = archives(tmp_path, mutate)
    with pytest.raises(ValueError, match=archive):
        check_distributions(wheel, sdist, SOURCE)


def test_distribution_contents_reject_missing_dependency(tmp_path: Path) -> None:
    def mutate(wheel, sdist):
        path = next(name for name in wheel if name.endswith("/METADATA"))
        wheel[path] = wheel[path].replace(b"Requires-Dist: referencing>=0.28.4\n", b"")

    wheel, sdist = archives(tmp_path, mutate)
    with pytest.raises(ValueError, match="runtime dependencies"):
        check_distributions(wheel, sdist, SOURCE)


def test_distribution_contents_reject_incorrect_cli(tmp_path: Path) -> None:
    def mutate(wheel, sdist):
        path = next(name for name in wheel if name.endswith("/entry_points.txt"))
        wheel[path] = wheel[path].replace(b"cli:main", b"missing:main")

    wheel, sdist = archives(tmp_path, mutate)
    with pytest.raises(ValueError, match="console scripts"):
        check_distributions(wheel, sdist, SOURCE)


def test_distribution_contents_reject_build_output(tmp_path: Path) -> None:
    def mutate(wheel, sdist):
        sdist["build/lib/stale.py"] = b""

    wheel, sdist = archives(tmp_path, mutate)
    with pytest.raises(ValueError, match="build output"):
        check_distributions(wheel, sdist, SOURCE)


def test_runtime_import_requires_direct_dependency() -> None:
    project = {"dependencies": [item for item in PROJECT["dependencies"] if not item.startswith("referencing")]}
    with pytest.raises(ValueError, match="undeclared runtime dependency: referencing"):
        check_imports(source_payload(SOURCE), project)
