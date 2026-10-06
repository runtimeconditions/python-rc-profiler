"""Validate release tags, checksum distributions, and attach verified assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import quote


def distributions(directory: Path) -> list[Path]:
    wheels = sorted(directory.glob("*.whl"))
    sources = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("release requires exactly one wheel and one source distribution")
    return sorted([*wheels, *sources])


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_tag(tag: str, source: Path) -> dict[str, str]:
    from packaging.version import Version

    version = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    parsed = Version(version)
    if str(parsed) != version or parsed.local is not None:
        raise ValueError("release version must be canonical PEP 440 without a local suffix")
    if tag != f"v{version}":
        raise ValueError(f"release tag {tag!r} must equal v{version}")
    return {"version": version, "prerelease": str(parsed.is_prerelease).lower()}


def write_checksums(directory: Path) -> Path:
    manifest = directory / "SHA256SUMS"
    manifest.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in distributions(directory)),
        encoding="ascii",
    )
    return manifest


def verify_checksums(directory: Path) -> list[Path]:
    paths = distributions(directory)
    expected = {}
    for line in (directory / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if match is None or match[2] in expected:
            raise ValueError("invalid or duplicate SHA256SUMS entry")
        expected[match[2]] = match[1]
    if set(expected) != {path.name for path in paths}:
        raise ValueError("SHA256SUMS must cover exactly the two release distributions")
    for path in paths:
        if sha256(path) != expected[path.name]:
            raise ValueError(f"checksum mismatch: {path.name}")
    return paths


def gh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], text=True, capture_output=True, check=False)


def checked_gh(*args: str) -> str:
    result = gh(*args)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def verify_remote_tag(repository: str, tag: str, commit: str) -> None:
    reference = json.loads(checked_gh("api", f"repos/{repository}/git/ref/tags/{quote(tag, safe='')}"))["object"]
    for _ in range(10):
        if reference["type"] == "commit":
            if reference["sha"] != commit:
                raise ValueError("remote release tag no longer points to the verified commit")
            return
        if reference["type"] != "tag":
            break
        reference = json.loads(checked_gh("api", f"repos/{repository}/git/tags/{reference['sha']}"))["object"]
    raise ValueError("release tag does not resolve to a commit")


def publish(directory: Path, repository: str, tag: str, commit: str, prerelease: bool) -> None:
    assets = [*verify_checksums(directory), directory / "SHA256SUMS"]
    verify_remote_tag(repository, tag, commit)
    result = gh("api", f"repos/{repository}/releases/tags/{quote(tag, safe='')}")
    if result.returncode:
        if "HTTP 404" not in result.stderr:
            raise RuntimeError(result.stderr.strip())
        args = ["release", "create", tag, "--repo", repository, "--verify-tag", "--draft", "--title", tag, "--generate-notes"]
        if prerelease:
            args.append("--prerelease")
        checked_gh(*args)
        release = {"draft": True, "assets": []}
    else:
        release = json.loads(result.stdout)
    existing = {asset["name"] for asset in release["assets"]}
    missing = [path for path in assets if path.name not in existing]
    if not release["draft"] and missing:
        raise ValueError("published release is missing verified assets; use a new release tag")
    # A retry can finish a draft or verify an existing release, but never replace bytes.
    with tempfile.TemporaryDirectory(prefix="rc-release-assets-") as temp:
        for path in assets:
            if path.name not in existing:
                continue
            checked_gh("release", "download", tag, "--repo", repository, "--pattern", path.name, "--dir", temp)
            if sha256(Path(temp) / path.name) != sha256(path):
                raise ValueError(f"release already has different bytes for {path.name}; assets are never overwritten")
    if missing:
        checked_gh("release", "upload", tag, *[str(path.resolve()) for path in missing], "--repo", repository)
    if release["draft"]:
        checked_gh("release", "edit", tag, "--repo", repository, "--draft=false", f"--prerelease={str(prerelease).lower()}")


def prepare_pypi(
    directory: Path, packages: Path, repository: str, tag: str, commit: str, source: Path,
) -> dict:
    """Copy checked GitHub Release distributions into the PyPI upload directory."""
    try:
        from .check_distributions import check_distributions
    except ImportError:  # Direct script invocation from the tagged source checkout.
        from check_distributions import check_distributions

    check_tag(tag, source)
    verify_remote_tag(repository, tag, commit)
    release = json.loads(checked_gh("api", f"repos/{repository}/releases/tags/{quote(tag, safe='')}"))
    if release["draft"] or release["tag_name"] != tag:
        raise ValueError("PyPI publication requires a published release for the exact tag")
    names = [asset["name"] for asset in release["assets"]]
    wheels = [name for name in names if name.endswith(".whl")]
    sources = [name for name in names if name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sources) != 1 or names.count("SHA256SUMS") != 1:
        raise ValueError("GitHub Release must contain one wheel, one source distribution, and SHA256SUMS")
    assets = [*wheels, *sources, "SHA256SUMS"]
    if any(Path(name).name != name or "/" in name or "\\" in name for name in assets):
        raise ValueError("invalid GitHub Release asset name")
    for destination in (directory, packages):
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise ValueError("release download and PyPI upload directories must be empty")
    for name in assets:
        checked_gh("release", "download", tag, "--repo", repository, "--pattern", name, "--dir", str(directory))
    checked = verify_checksums(directory)
    report = check_distributions(directory / wheels[0], directory / sources[0], source)
    for path in checked:
        shutil.copyfile(path, packages / path.name)
        if sha256(packages / path.name) != sha256(path):
            raise ValueError(f"PyPI upload copy differs from the release asset: {path.name}")
    # SHA256SUMS stays outside packages: only installable distributions are uploaded.
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    tag_parser = commands.add_parser("check-tag")
    tag_parser.add_argument("tag")
    tag_parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    tag_parser.add_argument("--github-output", type=Path)
    for name in ("checksums", "verify-checksums", "publish", "prepare-pypi"):
        command = commands.add_parser(name)
        command.add_argument("--dist-dir", type=Path, default=Path("dist"))
        if name in {"publish", "prepare-pypi"}:
            command.add_argument("--repository", required=True)
            command.add_argument("--tag", required=True)
            command.add_argument("--commit", required=True)
        if name == "publish":
            command.add_argument("--prerelease", action="store_true")
        if name == "prepare-pypi":
            command.add_argument("--packages-dir", type=Path, default=Path("pypi-dist"))
            command.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        if args.command == "check-tag":
            result = check_tag(args.tag, args.source_root)
            if args.github_output:
                with args.github_output.open("a", encoding="utf-8") as output:
                    output.writelines(f"{key}={value}\n" for key, value in result.items())
            print(json.dumps(result))
        elif args.command == "checksums":
            print(write_checksums(args.dist_dir))
        elif args.command == "verify-checksums":
            verify_checksums(args.dist_dir)
            print("release checksums verified")
        elif args.command == "publish":
            publish(args.dist_dir, args.repository, args.tag, args.commit, args.prerelease)
        else:
            report = prepare_pypi(args.dist_dir, args.packages_dir, args.repository, args.tag, args.commit, args.source_root)
            print(json.dumps(report, indent=2, sort_keys=True))
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        parser.exit(1, f"release check failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
