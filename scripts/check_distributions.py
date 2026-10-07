"""Check release archives against their source without importing the profiler."""

from __future__ import annotations

import argparse
import ast
import configparser
import hashlib
import json
import sys
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

PACKAGE = "runtimeconditions_profiler"
RESOURCES = ("schemas.json", "runtimeconditions.profile.v0.2.0.schema.yaml")
IMPORT_DISTRIBUTIONS = {
    "jsonschema": "jsonschema",
    "packaging": "packaging",
    "referencing": "referencing",
    "rfc8785": "rfc8785",
    "yaml": "PyYAML",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def source_payload(source: Path) -> dict[str, bytes]:
    paths = sorted((source / PACKAGE).rglob("*.py"))
    paths.extend(source / PACKAGE / resource for resource in RESOURCES)
    return {path.relative_to(source).as_posix(): path.read_bytes() for path in paths}


def requirement_key(value: str) -> tuple:
    item = Requirement(value)
    return (
        canonicalize_name(item.name), tuple(sorted(item.extras)), str(item.specifier),
        str(item.marker) if item.marker else None, item.url,
    )


def check_metadata(data: bytes, project: dict, version: str, label: str) -> None:
    metadata = BytesParser().parsebytes(data)
    for field, expected in (
        ("Name", project["name"]), ("Version", version),
        ("Requires-Python", project["requires-python"]),
        ("Description-Content-Type", "text/markdown"),
    ):
        require(metadata[field] == expected, f"{label}: incorrect {field}")
    requirements = metadata.get_all("Requires-Dist", [])
    runtime = [item for item in requirements if "extra" not in str(Requirement(item).marker)]
    require(
        {requirement_key(item) for item in runtime}
        == {requirement_key(item) for item in project["dependencies"]},
        f"{label}: runtime dependencies differ from pyproject.toml",
    )


def check_imports(payload: dict[str, bytes], project: dict) -> list[str]:
    declared = {canonicalize_name(Requirement(item).name) for item in project["dependencies"]}
    imports = set()
    for name, data in payload.items():
        if not name.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(data, filename=name)):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
    external = imports - sys.stdlib_module_names - {PACKAGE}
    for module in sorted(external):
        require(module in IMPORT_DISTRIBUTIONS, f"unmapped third-party import: {module}")
        require(
            canonicalize_name(IMPORT_DISTRIBUTIONS[module]) in declared,
            f"undeclared runtime dependency: {IMPORT_DISTRIBUTIONS[module]}",
        )
    return sorted(external)


def check_payload(files: dict[str, bytes], expected: dict[str, bytes], label: str) -> None:
    actual = {name: data for name, data in files.items() if name.startswith(f"{PACKAGE}/")}
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    require(not missing and not extra, f"{label}: missing={missing}; unexpected={extra}")
    changed = sorted(name for name in expected if actual[name] != expected[name])
    require(not changed, f"{label}: files differ from source: {changed}")


def wheel_files(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        names = [item.filename for item in archive.infolist() if not item.is_dir()]
        require(len(names) == len(set(names)), f"{path}: duplicate archive entries")
        return {name: archive.read(name) for name in names}


def sdist_files(path: Path) -> dict[str, bytes]:
    files = {}
    with tarfile.open(path, "r:gz") as archive:
        roots = {PurePosixPath(item.name).parts[0] for item in archive.getmembers()}
        require(len(roots) == 1, f"{path}: source archive must have one root directory")
        for item in archive.getmembers():
            if item.isdir():
                continue
            require(item.isfile(), f"{path}: unsupported archive entry: {item.name}")
            parts = PurePosixPath(item.name).parts
            require(".." not in parts and len(parts) > 1, f"{path}: invalid path: {item.name}")
            name = PurePosixPath(*parts[1:]).as_posix()
            require(name not in files, f"{path}: duplicate archive entry: {name}")
            stream = archive.extractfile(item)
            require(stream is not None, f"{path}: unreadable archive entry: {name}")
            with stream:
                files[name] = stream.read()
    return files


def check_distributions(wheel: Path, sdist: Path, source: Path, expected_version: str | None = None) -> dict:
    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    expected = source_payload(source)
    imports = check_imports(expected, project)
    wheel_contents = wheel_files(wheel)
    sdist_contents = sdist_files(sdist)
    check_payload(wheel_contents, expected, "wheel")
    check_payload(sdist_contents, expected, "sdist")
    metadata_dirs = {
        name.split("/")[0] for name in wheel_contents
        if name.split("/")[0].endswith(".dist-info")
    }
    require(len(metadata_dirs) == 1, "wheel: expected one .dist-info directory")
    metadata_dir = metadata_dirs.pop()
    require(
        all(name.split("/")[0] in {PACKAGE, metadata_dir} for name in wheel_contents),
        "wheel: unexpected files outside the runtime package and distribution metadata",
    )
    wheel_metadata = wheel_contents[f"{metadata_dir}/METADATA"]
    version = expected_version if expected_version is not None else BytesParser().parsebytes(wheel_metadata)["Version"]
    require(isinstance(version, str) and bool(version), "wheel: missing Version")
    require(str(Version(version)) == version, "distribution version must be canonical PEP 440")
    check_metadata(wheel_metadata, project, version, "wheel")
    check_metadata(sdist_contents["PKG-INFO"], project, version, "sdist")
    entrypoints = configparser.ConfigParser()
    entrypoints.optionxform = str
    entrypoints.read_string(wheel_contents[f"{metadata_dir}/entry_points.txt"].decode("utf-8"))
    require(
        dict(entrypoints["console_scripts"]) == project["scripts"],
        "wheel: console scripts differ from pyproject.toml",
    )
    source_files = [source / name for name in ("README.md", "pyproject.toml", "MANIFEST.in")]
    source_files.extend((source / "scripts").glob("*.py"))
    source_files.extend((source / "requirements").glob("*.txt"))
    source_files.extend(path for path in (source / "testdata/release-smoke").rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    for path in source_files:
        name = path.relative_to(source).as_posix()
        require(sdist_contents.get(name) == (source / name).read_bytes(), f"sdist: missing or altered {name}")
    forbidden = {"build", "dist", "archive", ".git", "__pycache__", ".venv"}
    require(
        all(not forbidden.intersection(PurePosixPath(name).parts) for name in sdist_contents),
        "sdist: contains build output, caches, or archived research",
    )
    return {
        "status": "passed", "name": project["name"], "version": version,
        "pythonModules": sum(name.endswith(".py") for name in expected),
        "resources": {name: hashlib.sha256(expected[f"{PACKAGE}/{name}"]).hexdigest() for name in RESOURCES},
        "consoleScripts": project["scripts"], "runtimeDependencies": project["dependencies"],
        "thirdPartyImports": imports,
        "artifacts": {
            str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (wheel, sdist)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("sdist", type=Path)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expected-version", help="Require both artifacts to match this tag-derived version")
    args = parser.parse_args()
    try:
        report = check_distributions(args.wheel, args.sdist, args.source_root, args.expected_version)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"distribution check failed: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
