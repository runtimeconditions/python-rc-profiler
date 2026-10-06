import pytest

from runtimeconditions_profiler.errors import RuntimeConditionsError
from runtimeconditions_profiler.extension.identity import definition_identifier, parse_identifier


def test_definition_identity_joins_resolved_uri_and_version() -> None:
    metadata = {
        "uri": "https://extensions.example.test/acme/jobs",
        "version": "1.2.3",
    }

    identifier = definition_identifier(metadata)

    assert identifier == "https://extensions.example.test/acme/jobs:1.2.3"
    assert parse_identifier(identifier) == (
        "https://extensions.example.test/acme/jobs",
        "1.2.3",
    )


@pytest.mark.parametrize(
    "identifier",
    [
        "jobs:1.2.3",
        "https://extensions.example.test/acme/jobs:",
        "https://extensions.example.test/acme/jobs:1/2",
        "https://user@extensions.example.test/acme/jobs:1.2.3",
        "https://extensions.example.test/acme/jobs:1.2.3?channel=latest",
    ],
)
def test_invalid_extension_identity_is_rejected(identifier: str) -> None:
    with pytest.raises(RuntimeConditionsError):
        parse_identifier(identifier)
