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
        "",
        {"uri": "https://extensions.example.test/acme/jobs", "version": "1.2.3"},
        "https://extensions.example.test/acme/jobs:",
    ],
)
def test_invalid_extension_identity_is_rejected(identifier: object) -> None:
    with pytest.raises(RuntimeConditionsError):
        parse_identifier(identifier)


FULL = "https://runtimeconditions.io/extensions/rc/common-integrations/v1alpha1/runtimeconditions.extension.yaml"
SHORT = "https://runtimeconditions.io/extensions/rc/common-integrations"


def test_profile_reference_uses_the_short_url_and_version() -> None:
    from runtimeconditions_profiler.extension.identity import profile_reference

    assert profile_reference((FULL, "v1alpha1")) == f"{SHORT}:v1alpha1"


def test_profile_reference_keeps_ids_that_do_not_end_with_the_version_tail() -> None:
    from runtimeconditions_profiler.extension.identity import profile_reference

    assert profile_reference(("acme/jobs", "1.0.0")) == "acme/jobs:1.0.0"
    assert profile_reference(("urn:acme:jobs", "")) == "urn:acme:jobs"
    assert profile_reference((FULL, "v2")) == f"{FULL}:v2"


def test_expand_identifier_rebuilds_the_full_id() -> None:
    from runtimeconditions_profiler.extension.identity import expand_identifier, parse_identifier

    assert expand_identifier(*parse_identifier(f"{SHORT}:v1alpha1")) == FULL
    assert expand_identifier(FULL, "v1alpha1") == FULL
    assert expand_identifier("acme/jobs", "1.0.0") == "acme/jobs"


def test_validator_resolves_a_short_extension_reference() -> None:
    from types import SimpleNamespace

    from runtimeconditions_profiler.profile.validator import ProfileValidator

    from runtimeconditions_profiler.extension.definition import parse_extension_definition

    definition = parse_extension_definition({"metadata": {"id": FULL, "version": "v1alpha1"}, "spec": {}}, FULL, FULL)
    artifact = SimpleNamespace(id=FULL, version="v1alpha1", definition=definition)
    discovery = SimpleNamespace(validated_artifacts=[], sdk_extensions=[artifact])
    profile = {"apiVersion": "runtimeconditions.io/v1alpha1", "kind": "RuntimeConditionsProfile", "extensions": [f"{SHORT}:v1alpha1"], "conditions": []}
    messages = [item.message for item in ProfileValidator().validate(profile, discovery)]
    assert not [m for m in messages if "missing extension definition" in m]
