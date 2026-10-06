"""Helpers for canonical extension URI and version identifiers."""

from __future__ import annotations

from urllib.parse import urlsplit

from ..errors import RuntimeConditionsError


def parse_identifier(identifier: str) -> tuple[str, str]:
    """Split and validate an extension identifier of the form ``<uri>:<version>``."""
    uri, separator, version = identifier.rpartition(":")
    if not separator or not uri or not version:
        raise RuntimeConditionsError("extension identifier must be <uri>:<version>")

    try:
        parsed = urlsplit(uri)
        parsed.port  # Force validation of a supplied port.
    except ValueError as exc:
        raise RuntimeConditionsError("extension identifier contains a malformed URI") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in uri
        or "#" in uri
        or not parsed.path
        or parsed.path == "/"
        or any(ord(char) < 32 or char.isspace() for char in identifier)
        or any(char in ":/\\?#" for char in version)
    ):
        raise RuntimeConditionsError(
            "extension identifier must contain an absolute HTTP(S) URI and version"
        )
    return uri, version


def definition_identifier(metadata: dict[str, object]) -> str:
    """Return the canonical identifier declared by extension metadata."""
    uri, version = metadata.get("uri"), metadata.get("version")
    if not isinstance(uri, str) or not isinstance(version, str) or "id" in metadata:
        raise RuntimeConditionsError("extension metadata requires uri and version; metadata.id is unsupported")
    identifier = f"{uri}:{version}"
    parse_identifier(identifier)
    return identifier
