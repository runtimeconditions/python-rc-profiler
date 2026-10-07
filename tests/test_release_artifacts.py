from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import release_artifacts as release

COMMIT = "a" * 40


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    directory = tmp_path / "dist"
    directory.mkdir()
    (directory / "profiler-0.1.0-py3-none-any.whl").write_bytes(b"wheel bytes")
    (directory / "profiler-0.1.0.tar.gz").write_bytes(b"sdist bytes")
    release.write_checksums(directory)
    return directory


@pytest.mark.parametrize("version,prerelease", [
    ("0.0.1", "false"), ("0.1.0", "false"), ("9.9.9", "false"),
    ("0.2.0rc1", "true"), ("0.2.0.post1", "false"),
])
def test_tag_is_the_package_version_source(version: str, prerelease: str) -> None:
    assert release.check_tag(f"v{version}") == {"version": version, "prerelease": prerelease}


@pytest.mark.parametrize("version", ["01.0.0", "1.0.0+local"])
def test_tag_rejects_normalized_or_local_versions(version: str) -> None:
    with pytest.raises(ValueError, match="canonical PEP 440"):
        release.check_tag(f"v{version}")


@pytest.mark.parametrize("tag", ["0.0.1", "release-0.0.1", "v", "vbanana", "v1.0.0\nprerelease=false"])
def test_tag_rejects_missing_prefix_or_invalid_versions(tag: str) -> None:
    with pytest.raises(ValueError):
        release.check_tag(tag)


def test_tag_cli_emits_build_version_without_a_source_checkout(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    result = subprocess.run(
        [sys.executable, str(Path(release.__file__).resolve()), "check-tag", "v0.0.1", "--github-output", str(output)],
        cwd=tmp_path, text=True, capture_output=True, check=True,
    )
    assert json.loads(result.stdout) == {"version": "0.0.1", "prerelease": "false"}
    assert output.read_text() == "version=0.0.1\nprerelease=false\n"


def test_checksums_cover_both_exact_artifacts(dist: Path) -> None:
    assert len(release.verify_checksums(dist)) == 2
    next(dist.glob("*.whl")).write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum mismatch"):
        release.verify_checksums(dist)


@pytest.mark.parametrize("bad", ["missing", "duplicate", "traversal", "extra"])
def test_checksums_reject_invalid_manifests(dist: Path, bad: str) -> None:
    manifest = dist / "SHA256SUMS"
    lines = manifest.read_text().splitlines(keepends=True)
    if bad == "missing":
        manifest.write_text(lines[0])
    elif bad == "duplicate":
        manifest.write_text("".join(lines + [lines[0]]))
    elif bad == "traversal":
        manifest.write_text(lines[0].replace("  profiler", "  ../profiler"))
    else:
        (dist / "another.whl").write_bytes(b"extra")
    with pytest.raises(ValueError):
        release.verify_checksums(dist)


def github(monkeypatch: pytest.MonkeyPatch, dist: Path, draft=None, previous=None, commit=COMMIT, api_error=None):
    calls = []
    previous = previous or {}

    def fake(*args):
        calls.append(args)
        if args[0] == "api":
            if "/git/ref/" in args[1]:
                return subprocess.CompletedProcess(args, 0, json.dumps({"object": {"type": "commit", "sha": commit}}), "")
            if api_error:
                return subprocess.CompletedProcess(args, 1, "", api_error)
            if draft is None:
                return subprocess.CompletedProcess(args, 1, "", "release not found (HTTP 404)")
            return subprocess.CompletedProcess(args, 0, json.dumps({"draft": draft, "tag_name": "v0.1.0", "assets": [{"name": name} for name in previous]}), "")
        if args[:2] == ("release", "download"):
            name = args[args.index("--pattern") + 1]
            destination = Path(args[args.index("--dir") + 1])
            (destination / name).write_bytes(previous[name])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(release, "gh", fake)
    return calls


def test_publish_attaches_verified_assets_before_publishing(monkeypatch: pytest.MonkeyPatch, dist: Path) -> None:
    calls = github(monkeypatch, dist)
    release.publish(dist, "org/repo", "v0.1.0", COMMIT, False)
    operations = [call[:2] for call in calls]
    assert operations == [("api", "repos/org/repo/git/ref/tags/v0.1.0"), ("api", "repos/org/repo/releases/tags/v0.1.0"),
                          ("release", "create"), ("release", "upload"), ("release", "edit")]
    create = next(call for call in calls if call[:2] == ("release", "create"))
    assert "--draft" in create and "--verify-tag" in create
    upload = next(call for call in calls if call[:2] == ("release", "upload"))
    assert all(str(path.resolve()) in upload for path in dist.iterdir())
    assert "--clobber" not in upload
    assert "--draft=false" in calls[-1]


def test_publish_resumes_matching_draft(monkeypatch: pytest.MonkeyPatch, dist: Path) -> None:
    wheel = next(dist.glob("*.whl"))
    calls = github(monkeypatch, dist, draft=True, previous={wheel.name: wheel.read_bytes()})
    release.publish(dist, "org/repo", "v0.1.0", COMMIT, True)
    upload = next(call for call in calls if call[:2] == ("release", "upload"))
    assert str(wheel.resolve()) not in upload
    assert "--prerelease=true" in calls[-1]


def test_publish_matching_published_release_is_idempotent(monkeypatch: pytest.MonkeyPatch, dist: Path) -> None:
    calls = github(monkeypatch, dist, draft=False, previous={path.name: path.read_bytes() for path in dist.iterdir()})
    release.publish(dist, "org/repo", "v0.1.0", COMMIT, False)
    assert not any(call[:2] in {("release", "upload"), ("release", "edit"), ("release", "create")} for call in calls)


def test_publish_refuses_different_existing_asset(monkeypatch: pytest.MonkeyPatch, dist: Path) -> None:
    wheel = next(dist.glob("*.whl"))
    calls = github(monkeypatch, dist, draft=True, previous={wheel.name: b"different bytes"})
    with pytest.raises(ValueError, match="never overwritten"):
        release.publish(dist, "org/repo", "v0.1.0", COMMIT, False)
    assert not any(call[:2] == ("release", "upload") for call in calls)


def test_publish_refuses_moved_tag(monkeypatch: pytest.MonkeyPatch, dist: Path) -> None:
    calls = github(monkeypatch, dist, commit="b" * 40)
    with pytest.raises(ValueError, match="verified commit"):
        release.publish(dist, "org/repo", "v0.1.0", COMMIT, False)
    assert len(calls) == 1


def test_publish_preserves_authentication_errors(monkeypatch: pytest.MonkeyPatch, dist: Path) -> None:
    calls = github(monkeypatch, dist, api_error="forbidden (HTTP 403)")
    with pytest.raises(RuntimeError, match="403"):
        release.publish(dist, "org/repo", "v0.1.0", COMMIT, False)
    assert not any(call[:2] == ("release", "create") for call in calls)


@pytest.fixture
def pypi_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\ndynamic = ["version"]\n')
    return source


def test_prepare_pypi_preserves_release_bytes_and_excludes_checksums(
    monkeypatch: pytest.MonkeyPatch, dist: Path, tmp_path: Path, pypi_source: Path,
) -> None:
    from scripts import check_distributions

    previous = {path.name: path.read_bytes() for path in dist.iterdir()}
    calls = github(monkeypatch, dist, draft=False, previous=previous)
    download = tmp_path / "download"
    packages = tmp_path / "packages"
    validations = []

    def validate(wheel, sdist, source, expected_version):
        validations.append((wheel, sdist, source, expected_version))
        return {"status": "passed"}

    monkeypatch.setattr(check_distributions, "check_distributions", validate)
    assert release.prepare_pypi(download, packages, "org/repo", "v0.1.0", COMMIT, pypi_source) == {"status": "passed"}
    assert {path.name for path in packages.iterdir()} == set(previous) - {"SHA256SUMS"}
    assert all(path.read_bytes() == previous[path.name] for path in packages.iterdir())
    assert len(validations) == 1
    assert validations[0][2] == pypi_source
    assert validations[0][3] == "0.1.0"
    assert len([call for call in calls if call[:2] == ("release", "download")]) == 3
    assert not any(call[:2] in {("release", "upload"), ("release", "create")} for call in calls)


def test_prepare_pypi_requires_published_release(
    monkeypatch: pytest.MonkeyPatch, dist: Path, tmp_path: Path, pypi_source: Path,
) -> None:
    calls = github(monkeypatch, dist, draft=True)
    with pytest.raises(ValueError, match="published release"):
        release.prepare_pypi(tmp_path / "download", tmp_path / "packages", "org/repo", "v0.1.0", COMMIT, pypi_source)
    assert not any(call[:2] == ("release", "download") for call in calls)


@pytest.mark.parametrize("missing", [".whl", ".tar.gz", "SHA256SUMS"])
def test_prepare_pypi_rejects_missing_release_asset(
    monkeypatch: pytest.MonkeyPatch, dist: Path, tmp_path: Path, pypi_source: Path, missing: str,
) -> None:
    previous = {path.name: path.read_bytes() for path in dist.iterdir() if not path.name.endswith(missing)}
    calls = github(monkeypatch, dist, draft=False, previous=previous)
    with pytest.raises(ValueError, match="GitHub Release must contain"):
        release.prepare_pypi(tmp_path / "download", tmp_path / "packages", "org/repo", "v0.1.0", COMMIT, pypi_source)
    assert not any(call[:2] == ("release", "download") for call in calls)


def test_prepare_pypi_rejects_unsafe_asset_name(
    monkeypatch: pytest.MonkeyPatch, dist: Path, tmp_path: Path, pypi_source: Path,
) -> None:
    previous = {path.name: path.read_bytes() for path in dist.iterdir()}
    wheel = next(name for name in previous if name.endswith(".whl"))
    previous[f"../{wheel}"] = previous.pop(wheel)
    calls = github(monkeypatch, dist, draft=False, previous=previous)
    with pytest.raises(ValueError, match="asset name"):
        release.prepare_pypi(tmp_path / "download", tmp_path / "packages", "org/repo", "v0.1.0", COMMIT, pypi_source)
    assert not any(call[:2] == ("release", "download") for call in calls)


def test_prepare_pypi_rejects_changed_release_bytes(
    monkeypatch: pytest.MonkeyPatch, dist: Path, tmp_path: Path, pypi_source: Path,
) -> None:
    previous = {path.name: path.read_bytes() for path in dist.iterdir()}
    previous[next(name for name in previous if name.endswith(".whl"))] = b"changed bytes"
    github(monkeypatch, dist, draft=False, previous=previous)
    packages = tmp_path / "packages"
    with pytest.raises(ValueError, match="checksum mismatch"):
        release.prepare_pypi(tmp_path / "download", packages, "org/repo", "v0.1.0", COMMIT, pypi_source)
    assert not list(packages.iterdir())


def test_prepare_pypi_rejects_contents_before_staging_uploads(
    monkeypatch: pytest.MonkeyPatch, dist: Path, tmp_path: Path, pypi_source: Path,
) -> None:
    from scripts import check_distributions

    github(monkeypatch, dist, draft=False, previous={path.name: path.read_bytes() for path in dist.iterdir()})

    def invalid(*args, **kwargs):
        raise ValueError("distribution contents do not match")

    monkeypatch.setattr(check_distributions, "check_distributions", invalid)
    packages = tmp_path / "packages"
    with pytest.raises(ValueError, match="contents do not match"):
        release.prepare_pypi(tmp_path / "download", packages, "org/repo", "v0.1.0", COMMIT, pypi_source)
    assert not list(packages.iterdir())


def test_prepare_pypi_rejects_stale_upload_directory(
    monkeypatch: pytest.MonkeyPatch, dist: Path, tmp_path: Path, pypi_source: Path,
) -> None:
    calls = github(monkeypatch, dist, draft=False, previous={path.name: path.read_bytes() for path in dist.iterdir()})
    packages = tmp_path / "packages"
    packages.mkdir()
    (packages / "stale.whl").write_bytes(b"old release")
    with pytest.raises(ValueError, match="must be empty"):
        release.prepare_pypi(tmp_path / "download", packages, "org/repo", "v0.1.0", COMMIT, pypi_source)
    assert not any(call[:2] == ("release", "download") for call in calls)


def test_prepare_pypi_rejects_moved_tag(
    monkeypatch: pytest.MonkeyPatch, dist: Path, tmp_path: Path, pypi_source: Path,
) -> None:
    calls = github(monkeypatch, dist, commit="b" * 40)
    with pytest.raises(ValueError, match="verified commit"):
        release.prepare_pypi(tmp_path / "download", tmp_path / "packages", "org/repo", "v0.1.0", COMMIT, pypi_source)
    assert len(calls) == 1
