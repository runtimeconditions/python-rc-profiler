from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..manifest.mapping import all_mappings, class_matches
from ..models import ManifestModel, SymbolMapping, ValidatedArtifact
from ..util import as_map


@dataclass(frozen=True)
class BindingArtifact:
    artifact: ValidatedArtifact

    @property
    def manifest(self) -> ManifestModel:
        assert self.artifact.manifest is not None
        return self.artifact.manifest

    @property
    def extension_id(self) -> str:
        assert self.artifact.extension_id is not None
        return self.artifact.extension_id

    def all_mappings(self) -> list[SymbolMapping]:
        return all_mappings(self.manifest)

    def find_declaration(self, class_name: str, member_name: str) -> Optional[SymbolMapping]:
        for mapping in self.manifest.declarations:
            if class_matches(class_name, mapping.class_name) and mapping.member_name == member_name:
                return mapping
        return None

    def find_root_option(self, class_name: str, member_name: str, condition: dict[str, Any]) -> Optional[SymbolMapping]:
        kind = str(condition.get("kind", ""))
        interface_type = str(as_map(condition.get("interface")).get("type", ""))
        for option in self.manifest.options:
            if not class_matches(class_name, option.class_name) or option.member_name != member_name:
                continue
            if option.applies_to_kinds and kind not in option.applies_to_kinds:
                continue
            if option.applies_to_interface_types and interface_type not in option.applies_to_interface_types:
                continue
            return option
        return None

