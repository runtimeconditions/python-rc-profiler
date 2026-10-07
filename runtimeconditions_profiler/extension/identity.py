"""Helpers for exact extension release references and metadata."""

from __future__ import annotations

from ..errors import RuntimeConditionsError


def parse_identifier(reference: object) -> tuple[str, str]:
    """Read an exact (id, version) pair without imposing URI syntax."""
    if not isinstance(reference, dict):
        raise RuntimeConditionsError("extension reference requires id and version")
    identifier, version = reference.get("id"), reference.get("version")
    if not isinstance(identifier, str) or not identifier or not isinstance(version, str) or not version or "uri" in reference:
        raise RuntimeConditionsError("extension reference requires id and version; metadata.uri is unsupported")
    return identifier, version


def reference_object(reference: tuple[str, str]) -> dict[str, str]:
    return {"id": reference[0], "version": reference[1]}


def definition_identifier(metadata: dict[str, object]) -> str:
    """Return the ID declared by valid extension metadata."""
    return parse_identifier(metadata)[0]
