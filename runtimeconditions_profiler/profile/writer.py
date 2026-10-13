from __future__ import annotations

from typing import Any

from ..yamlio import dump_yaml

PROFILE_FIELD_ORDER = (
    "apiVersion",
    "kind",
    "metadata",
    "workload",
    "extensions",
    "conditions",
)


class ProfileYamlWriter:
    @staticmethod
    def write(profile: dict[str, Any]) -> str:
        ordered = {key: profile[key] for key in PROFILE_FIELD_ORDER if key in profile}
        ordered.update((key, profile[key]) for key in sorted(profile) if key not in ordered)
        return dump_yaml(ordered)
