"""Exact extension identity and HTTPS catalog lookup convention."""

from urllib.parse import quote, urlsplit, urlunsplit

from ..errors import RuntimeConditionsError


def parse_identifier(identifier: str) -> tuple[str, str, str, str]:
    uri, separator, version = identifier.rpartition(":")
    if not separator or "://" not in uri:
        raise RuntimeConditionsError("extension identifier must be <https-uri>:<version>")
    parsed = urlsplit(uri)
    parts = parsed.path.removeprefix("/").split("/")
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.query or "?" in uri or parsed.fragment or "#" in uri
        or len(parts) not in (1, 2) or (len(parts) == 2 and parts[0] == "extensions")
    ):
        raise RuntimeConditionsError("extension URI must be HTTPS with /<service> or /<provider>/<service>")
    for part in (*parts, version):
        if not part or part in (".", "..") or any(c in "/\\:%?#" or c.isspace() or ord(c) < 32 for c in part):
            raise RuntimeConditionsError("extension provider, service, and version must be safe path segments")
    return uri, version, parts[0] if len(parts) == 2 else "rc", parts[-1]


def definition_identifier(metadata: dict) -> str:
    uri, version = metadata.get("uri"), metadata.get("version")
    if not isinstance(uri, str) or not isinstance(version, str) or "id" in metadata:
        raise RuntimeConditionsError("extension metadata requires uri and version; metadata.id is unsupported")
    identifier = uri + ":" + version
    parse_identifier(identifier)
    return identifier


def definition_url(identifier: str) -> str:
    uri, version, provider, service = parse_identifier(identifier)
    parsed = urlsplit(uri)
    path = "/extensions/" + "/".join(quote(p, safe="+.-_~") for p in (provider, service, version)) + "/runtimeconditions.extension.yaml"
    return urlunsplit(("https", parsed.netloc, path, "", ""))
