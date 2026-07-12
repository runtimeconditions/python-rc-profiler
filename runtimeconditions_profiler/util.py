from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .errors import RuntimeConditionsError
from .models import Diagnostic, RuntimeConditionsArtifact


def as_map(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def scalar(value: Any) -> Optional[str]:
    if isinstance(value, (str, int, bool, float)):
        return str(value)
    return None


def string_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def parse_plain_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, bool))]


def ignored_path(path: Path) -> bool:
    ignored = {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "build",
        "dist",
        "*.egg-info",
    }
    parts = set(path.parts)
    if parts & (ignored - {"*.egg-info"}):
        return True
    return any(part.endswith(".egg-info") for part in path.parts)


def uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise RuntimeConditionsError(f"unsupported artifact URI scheme: {parsed.scheme}")
    return Path(parsed.path)


def dedupe_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def dedupe_artifacts(artifacts: list[RuntimeConditionsArtifact]) -> list[RuntimeConditionsArtifact]:
    result: list[RuntimeConditionsArtifact] = []
    seen: set[str] = set()
    for artifact in artifacts:
        key = artifact.manifest_uri
        if key in seen:
            continue
        seen.add(key)
        result.append(artifact)
    return result


def diagnostics_text(diagnostics: list[Diagnostic]) -> str:
    return "; ".join(diagnostic.message for diagnostic in diagnostics)


def deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [deep_copy(item) for item in value]
    return value


def add_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def add_extension_closure(extension: str, dependencies: dict[str, list[str]], resolved: set[str]) -> None:
    for dependency in dependencies.get(extension, []):
        add_extension_closure(dependency, dependencies, resolved)
    resolved.add(extension)


def condition_scopes_overlap(
    left_kinds: tuple[str, ...],
    left_types: tuple[str, ...],
    right_kinds: tuple[str, ...],
    right_types: tuple[str, ...],
) -> bool:
    if not set(left_kinds).intersection(right_kinds):
        return False
    return not left_types or not right_types or bool(set(left_types).intersection(right_types))


def increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1

