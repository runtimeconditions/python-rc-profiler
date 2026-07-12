from __future__ import annotations

from typing import Any

from ..yamlio import dump_yaml


class ProfileYamlWriter:
    @staticmethod
    def write(profile: dict[str, Any]) -> str:
        return dump_yaml(profile)

