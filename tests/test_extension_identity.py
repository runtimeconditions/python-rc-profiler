import pytest

from runtimeconditions_profiler.errors import RuntimeConditionsError
from runtimeconditions_profiler.extension.identity import definition_identifier, parse_identifier


def test_definition_identity_reads_id_and_version() -> None:
    metadata = {
        "id": "acme/jobs",
        "version": "release:custom",
    }

    identifier = definition_identifier(metadata)

    assert identifier == "acme/jobs"
    assert parse_identifier(metadata) == ("acme/jobs", "release:custom")


@pytest.mark.parametrize(
    "identifier",
    [
        {"id": "jobs", "version": ""},
        {"id": "", "version": "1.2.3"},
        {"id": "jobs"},
        {"uri": "https://extensions.example.test/acme/jobs", "version": "1.2.3"},
        "https://extensions.example.test/acme/jobs:1.2.3?channel=latest",
    ],
)
def test_invalid_extension_identity_is_rejected(identifier: object) -> None:
    with pytest.raises(RuntimeConditionsError):
        parse_identifier(identifier)
