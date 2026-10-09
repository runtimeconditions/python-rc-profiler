"""Install both release artifacts in fresh environments and profile test bindings."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path

import yaml

if __package__:
    from .release_artifacts import sha256, verify_checksums
else:
    from release_artifacts import sha256, verify_checksums


RESOURCES = ("schemas.json", "runtimeconditions.profile.v0.4.0.schema.yaml")
ROOT_ID = "https://runtimeconditions.io/conformance/dependency-schema-only-root:1.0.0"
DEPENDENCY_ID = "https://runtimeconditions.io/conformance/dependency-schema-only-dependency:1.0.0"
PACKAGE_PREFIX = "runtimeconditions_conformance_dependency_schema_only_"


def clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith(("PIP_", "SETUPTOOLS_SCM_", "VCS_VERSIONING_")) or name in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}:
            environment.pop(name)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def write_fixture_wheel(output: Path, package: dict, files: dict[str, bytes], requirements: list[str]) -> Path:
    name = package["coordinate"].replace("-", "_")
    info = f"{name}-{package['version']}.dist-info"
    contents = {f"{package['name']}/{path}": data for path, data in files.items()}
    contents[f"{info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {package['coordinate']}\nVersion: {package['version']}\n"
        f"Requires-Python: >={package['minimumPythonVersion']}\n"
        + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements) + "\n"
    ).encode("utf-8")
    contents[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: rc-profiler-release-smoke\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    contents[f"{info}/top_level.txt"] = f"{package['name']}\n".encode()
    records = io.StringIO(newline="")
    writer = csv.writer(records, lineterminator="\n")
    for path, data in sorted(contents.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        writer.writerow([path, f"sha256={digest}", len(data)])
    writer.writerow([f"{info}/RECORD", "", ""])
    contents[f"{info}/RECORD"] = records.getvalue().encode("utf-8")
    wheel = output / f"{name}-{package['version']}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, data in sorted(contents.items()):
            entry = zipfile.ZipInfo(path, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
    return wheel


def binding_wheels(fixtures: Path, output: Path, profiler: Path, version: str) -> list[Path]:
    output.mkdir()
    packages = {}
    for role in ("dependency", "root"):
        directory = fixtures / f"{PACKAGE_PREFIX}{role}"
        files = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
        manifest = yaml.safe_load(files["runtimeconditions.bindings.yaml"])
        model = yaml.safe_load(files["runtimeconditions.binding-model.yaml"])
        packages[(model["rootExtension"]["id"], model["rootExtension"]["version"])] = {
            "role": role, "files": files, "model": model, "package": manifest["package"],
        }
    built = {}
    for identity, item in packages.items():
        model = item["model"]
        dependencies = []
        requirements = []
        root_entry = next(entry for entry in model["extensions"] if (entry["id"], entry["version"]) == identity)
        for dependency in root_entry.get("dependencies", []):
            dependency_key = (dependency["id"], dependency["version"])
            package = packages[dependency_key]["package"]
            dependencies.append({
                "extension": dependency, "coordinate": package["coordinate"], "name": package["name"],
                "testedVersion": "1.0.0",
                "compatibleVersionRange": {"minimumInclusive": "1.0.0", "nextBreakingExclusive": "2.0.0"},
                "artifact": {"kind": "python-wheel", "sha256": sha256(built[dependency_key])},
            })
            requirements.append(f"{package['coordinate']}>=1.0.0,<2.0.0")
        release = {
            "apiVersion": "runtimeconditions.io/binding-release/v1alpha1", "kind": "RuntimeConditionsBindingRelease",
            "package": {**item["package"], "packageKey": f"release-smoke-{item['role']}", "publicationMode": "github-tag"},
            "model": {"apiVersion": model["apiVersion"], "semanticSha256": model["metadata"]["semanticSha256"]},
            "rootExtension": model["rootExtension"],
            "dependencyLock": {"extensions": [
                {
                    **entry,
                    "sourceSha256": hashlib.sha256(packages[(entry["id"], entry["version"])]["files"]["runtimeconditions.extension.yaml"]).hexdigest(),
                    "sourceBackend": "package", "sourceLocator": f"release-smoke:{entry['id']}",
                }
                for entry in model["extensions"]
            ]},
            "packageDependencies": dependencies,
            "provenance": {
                "mode": "test-fixture",
                "fixtureAssembler": {"name": "rc-profiler-release-smoke", "version": "1.0.0", "sha256": sha256(Path(__file__))},
                "profiler": {"name": "python-rc-profiler", "version": version, "sha256": sha256(profiler)},
            },
        }
        files = {**item["files"], "runtimeconditions.binding-release.yaml": yaml.safe_dump(release, sort_keys=False).encode("utf-8")}
        built[identity] = write_fixture_wheel(output, item["package"], files, requirements)
    return list(built.values())


PROBE = '''
import hashlib, json, sys
from importlib import metadata, resources
from pathlib import Path
import runtimeconditions_profiler
from runtimeconditions_profiler.project.verify import _schemas
from runtimeconditions_profiler.profile.semantic import _core_schema
if not Path(runtimeconditions_profiler.__file__).is_relative_to(Path(sys.prefix)):
    raise RuntimeError("profiler did not load from the clean environment")
if len(_schemas()) != 4 or _core_schema()["x-runtimeconditions-version"] != "0.4.0":
    raise RuntimeError("installed schema resources are invalid")
print(json.dumps({
    "version": metadata.version("runtimeconditions-profiler"),
    "resources": {
        name: hashlib.sha256(resources.files("runtimeconditions_profiler").joinpath(name).read_bytes()).hexdigest()
        for name in ("schemas.json", "runtimeconditions.profile.v0.4.0.schema.yaml")
    },
}))
'''


def run(command: list, cwd: Path, environment: dict, log: Path, expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(list(map(str, command)), cwd=cwd, env=environment, text=True, capture_output=True, check=False)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"$ {' '.join(map(str, command))}\n{result.stdout}{result.stderr}\n")
    if result.returncode != expected:
        raise RuntimeError(f"command exited {result.returncode}: {command}\n{result.stdout}{result.stderr}")
    return result


def smoke(directory: Path, fixtures: Path, report: Path, expected_version: str | None = None) -> dict:
    artifacts = verify_checksums(directory)
    wheel = next(path for path in artifacts if path.suffix == ".whl")
    with zipfile.ZipFile(wheel) as archive:
        metadata_file = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        version = BytesParser().parsebytes(archive.read(metadata_file))["Version"]
        if expected_version is not None and version != expected_version:
            raise ValueError(f"wheel: expected version {expected_version}, got {version}")
        resources = {name: hashlib.sha256(archive.read(f"runtimeconditions_profiler/{name}")).hexdigest() for name in RESOURCES}
    report.parent.mkdir(parents=True, exist_ok=True)
    environment = clean_environment()
    runs = []
    with tempfile.TemporaryDirectory(prefix="rc-profiler-installed-") as temp:
        work = Path(temp).resolve()
        if work.is_relative_to(Path(__file__).resolve().parents[1]):
            raise ValueError("smoke working directory must be outside the source checkout")
        bindings = binding_wheels(fixtures, work / "bindings", wheel, version)
        for artifact in artifacts:
            kind = "wheel" if artifact.suffix == ".whl" else "sdist"
            log = report.parent / f"{report.stem}-{kind}.log"
            install_report = report.parent / f"{report.stem}-{kind}-install.json"
            env = work / f"{kind}-venv"
            venv.EnvBuilder(with_pip=True).create(env)
            binary = env / ("Scripts" if os.name == "nt" else "bin")
            python = binary / ("python.exe" if os.name == "nt" else "python")
            cli = binary / ("runtimeconditions-python-profiler.exe" if os.name == "nt" else "runtimeconditions-python-profiler")
            run([
                python, "-m", "pip", "--isolated", "install", "--no-input", "--no-cache-dir", "--disable-pip-version-check",
                "--report", install_report.resolve(), artifact.resolve(), *bindings,
            ], work, environment, log)
            run([python, "-m", "pip", "check"], work, environment, log)
            run([cli, "--help"], work, environment, log)
            probe = json.loads(run([python, "-I", "-c", PROBE], work, environment, log).stdout)
            if probe != {"version": version, "resources": resources}:
                raise ValueError(f"{kind}: installed metadata or resource bytes differ from the release wheel")
            project = work / f"{kind}-workload"
            project.mkdir()
            app = project / "app.py"
            app.write_text(
                f"from {PACKAGE_PREFIX}root import Command, Process, job\n"
                "job(Command(value='sample'), Process())\n"
                "raise RuntimeError('release smoke workload must not execute')\n", encoding="utf-8",
            )
            verified = json.loads(run([cli, "profile", "verify-bindings", "--project", project, "--json"], project, environment, log).stdout)
            if {(item["extensionId"], item["extensionVersion"]) for item in verified["packages"]} != {(ROOT_ID, "1.0.0"), (DEPENDENCY_ID, "1.0.0")}:
                raise ValueError(f"{kind}: incorrect binding identities, versions, or dependency closure")
            output = project / "profile.yaml"
            command = [cli, "profile", "generate", "--project", project, "--name", "release-smoke",
                       "--workload-uri", "https://example.test/workload", "--workload-version", "1.0.0", "--out", output]
            run(command, project, environment, log)
            expected = {
                "apiVersion": "runtimeconditions.io/v1alpha1", "kind": "RuntimeConditionsProfile", "metadata": {"name": "release-smoke"},
                "workload": {"uri": "https://example.test/workload", "version": "1.0.0"}, "extensions": [f"{ROOT_ID}:1.0.0"],
                "conditions": [{"kind": "job", "interface": {"type": "process"}, "command": "sample"}],
            }
            if yaml.safe_load(output.read_bytes()) != expected:
                raise ValueError(f"{kind}: generated profile differs from the expected document")
            original = output.read_bytes()
            app.write_text(app.read_text(encoding="utf-8").replace("'sample'", "'too-long'"), encoding="utf-8")
            rejected = run(command, project, environment, log, expected=1)
            if f"{(DEPENDENCY_ID, '1.0.0')}/command-limit" not in rejected.stderr or output.read_bytes() != original:
                raise ValueError(f"{kind}: dependency schema rejection or output preservation failed")
            runs.append({"kind": kind, "artifact": artifact.name, "sha256": sha256(artifact), "status": "passed",
                         "resources": resources, "verifiedClosure": sorted([ROOT_ID, DEPENDENCY_ID]),
                         "outsideSourceCheckout": True, "expectedProfile": expected, "negativeDiagnostic": rejected.stderr})
            print(f"{kind}: clean installation and binding workload checks passed", flush=True)
    verify_checksums(directory)
    result = {"status": "passed", "python": sys.version, "platform": sys.platform, "installedArtifacts": runs}
    report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--fixtures", type=Path, default=Path(__file__).resolve().parents[1] / "testdata/release-smoke")
    parser.add_argument("--report", type=Path, default=Path("release-evidence/smoke.json"))
    parser.add_argument("--expected-version", help="Require installed artifacts to match this tag-derived version")
    args = parser.parse_args()
    try:
        smoke(args.dist_dir.resolve(), args.fixtures.resolve(), args.report.resolve(), args.expected_version)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"installed release smoke check failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
