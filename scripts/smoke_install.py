"""Install and check both release artifacts in fresh environments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path


if __package__:
    from .release_artifacts import sha256, verify_checksums
else:
    from release_artifacts import sha256, verify_checksums


RESOURCES = ("schemas.json", "runtimeconditions.profile.v0.4.0.schema.yaml")


def clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith(("PIP_", "SETUPTOOLS_SCM_", "VCS_VERSIONING_")) or name in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}:
            environment.pop(name)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


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


def smoke(directory: Path, report: Path, expected_version: str | None = None) -> dict:
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
                "--report", install_report.resolve(), artifact.resolve(),
            ], work, environment, log)
            run([python, "-m", "pip", "check"], work, environment, log)
            run([cli, "--help"], work, environment, log)
            probe = json.loads(run([python, "-I", "-c", PROBE], work, environment, log).stdout)
            if probe != {"version": version, "resources": resources}:
                raise ValueError(f"{kind}: installed metadata or resource bytes differ from the release wheel")
            runs.append({"kind": kind, "artifact": artifact.name, "sha256": sha256(artifact), "status": "passed",
                         "resources": resources, "outsideSourceCheckout": True})
            print(f"{kind}: clean installation checks passed", flush=True)
    verify_checksums(directory)
    result = {"status": "passed", "python": sys.version, "platform": sys.platform, "installedArtifacts": runs}
    report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--report", type=Path, default=Path("release-evidence/smoke.json"))
    parser.add_argument("--expected-version", help="Require installed artifacts to match this tag-derived version")
    args = parser.parse_args()
    try:
        smoke(args.dist_dir.resolve(), args.report.resolve(), args.expected_version)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"installed release smoke check failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
