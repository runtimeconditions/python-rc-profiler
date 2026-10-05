from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import RuntimeConditionsError

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover - dependency error path
    yaml = None
    YAML_IMPORT_ERROR = exc
else:
    YAML_IMPORT_ERROR = None


class Yaml:
    @staticmethod
    def load_text(text: str) -> dict[str, Any]:
        if yaml is None:
            raise RuntimeConditionsError(f"PyYAML is required: {YAML_IMPORT_ERROR}")
        data = yaml.safe_load(text)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise RuntimeConditionsError("YAML document must be a mapping")
        return data

    @staticmethod
    def load(path: Path) -> dict[str, Any]:
        return Yaml.load_text(path.read_text(encoding="utf-8"))


def dump_yaml(data: dict[str, Any]) -> str:
    if yaml is None:
        raise RuntimeConditionsError(f"PyYAML is required: {YAML_IMPORT_ERROR}")
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)

