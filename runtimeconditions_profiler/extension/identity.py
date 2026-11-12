"""Helpers for exact extension release references and metadata."""

from __future__ import annotations

from collections.abc import Iterable

from ..errors import RuntimeConditionsError


def parse_identifier(reference: object) -> tuple[str, str]:
    """Read extension metadata or a string reference as (id, optional version)."""
    if isinstance(reference, str) and reference:
        marker = reference.rfind(":")
        scheme_end = reference.find("://")
        if marker > 0 and marker > scheme_end + 2 and marker > reference.rfind("/"):
            if marker == len(reference) - 1:
                raise RuntimeConditionsError("extension reference version suffix must be non-empty")
            return reference[:marker], reference[marker + 1 :]
        return reference, ""
    if not isinstance(reference, dict):
        raise RuntimeConditionsError("extension reference requires a non-empty string")
    identifier, version = reference.get("id"), reference.get("version", "")
    if not isinstance(identifier, str) or not identifier or ("version" in reference and (not isinstance(version, str) or not version)) or "uri" in reference:
        raise RuntimeConditionsError("extension metadata requires id and an optional string version; metadata.uri is unsupported")
    return identifier, version or ""


def reference_object(reference: tuple[str, str]) -> dict[str, str]:
    return {"id": reference[0], "version": reference[1]}


EXTENSION_FILENAME = "runtimeconditions.extension.yaml"


def short_identifier(identifier: str, version: str) -> str:
    """Drop the /<version>/runtimeconditions.extension.yaml tail of an https extension id.

    Ids that are not https URLs, or that do not end that way, stay unchanged.
    """
    tail = f"/{version}/{EXTENSION_FILENAME}"
    if version and identifier.startswith("https://") and identifier.endswith(tail):
        return identifier[: -len(tail)]
    return identifier


def expand_identifier(identifier: str, version: str) -> str:
    """Rebuild the full extension id from a short https reference; anything else is returned as is."""
    if version and identifier.startswith("https://") and not identifier.endswith("/" + EXTENSION_FILENAME):
        return f"{identifier}/{version}/{EXTENSION_FILENAME}"
    return identifier


def profile_reference(reference: tuple[str, str]) -> str:
    """Serialize an extension reference for a Runtime Conditions Profile as <short-url>:<version>."""
    identifier, version = reference
    short = short_identifier(identifier, version)
    return f"{short}:{version}" if version else short


def definition_identifier(metadata: dict[str, object]) -> str:
    """Return the ID declared by valid extension metadata."""
    return parse_identifier(metadata)[0]


def resolve_reference(reference: tuple[str, str], known: Iterable[tuple[str, str]]) -> tuple[str, str]:
    """Pair a dependency written as a bare id with its version.

    Extension dependencies name an extension by its full id string, which carries no
    version suffix. When the id matches exactly one known (id, version), return that;
    otherwise return the reference unchanged.
    """
    keys = list(known)
    if reference in keys or reference[1]:
        return reference
    matches = [key for key in keys if key[0] == reference[0]]
    return matches[0] if len(matches) == 1 else reference
