from __future__ import annotations

from pathlib import Path

import yaml

from runtimeconditions_profiler.main import DiscoveryOptions, ProjectDiscovery


TESTDATA = Path(__file__).resolve().parents[1] / "testdata"


def test_authoring_fixtures() -> None:
    fixtures_root = TESTDATA / "authoring"
    for fixture in sorted(path for path in fixtures_root.iterdir() if path.is_dir()):
        config = yaml.safe_load((fixture / "fixture.yaml").read_text())
        result = ProjectDiscovery().discover(fixture, DiscoveryOptions())
        if config["valid"]:
            assert not result.has_errors, f"{fixture.name} should be valid: {messages(result.diagnostics)}"
        else:
            assert result.has_errors, f"{fixture.name} should fail"
            expected = config.get("wantErrorContains")
            if expected:
                assert expected in messages(result.diagnostics)


def messages(diagnostics) -> str:
    return "; ".join(diagnostic.message for diagnostic in diagnostics)
