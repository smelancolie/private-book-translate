from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any


LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(locator: dict[str, Any]) -> str:
    encoded = json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


@dataclass
class TranslationUnit:
    id: str
    section_id: str
    type: str
    source_text: str
    target_text: str | None
    translatable: bool
    source_language: str
    target_language: str
    source_hash: str
    translation_status: str
    qa_status: str
    locator: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    last_qa_target_hash: str | None = None
    last_error: str | None = None

    @classmethod
    def create(
        cls,
        *,
        section_id: str,
        type: str,
        source_text: str,
        translatable: bool,
        source_language: str,
        target_language: str,
        locator: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> TranslationUnit:
        if not LANGUAGE_TAG.fullmatch(source_language) or not LANGUAGE_TAG.fullmatch(target_language):
            raise ValueError("Language identifiers must be BCP-47 language tags")
        return cls(
            id=stable_id(locator),
            section_id=section_id,
            type=type,
            source_text=source_text,
            target_text=None,
            translatable=translatable,
            source_language=source_language,
            target_language=target_language,
            source_hash=content_hash(source_text),
            translation_status="pending",
            qa_status="pending",
            locator=locator,
            metadata=metadata or {},
        )

    @property
    def status(self) -> str:
        if self.translation_status == "stale":
            return "stale"
        if self.translation_status != "translated":
            return "pending"
        return f"qa_{self.qa_status}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TranslationUnit:
        return cls(**value)


@dataclass
class Section:
    section_id: str
    title: str
    locator: dict[str, Any]


@dataclass
class AssetReference:
    path: str
    media_type: str
    source_hash: str
    locator: dict[str, Any] = field(default_factory=dict)


@dataclass
class Document:
    format: str
    source_language: str
    target_language: str
    metadata: dict[str, Any]
    sections: list[Section]
    units: list[TranslationUnit]
    assets: list[AssetReference] = field(default_factory=list)
    structure: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "metadata": self.metadata,
            "sections": [asdict(section) for section in self.sections],
            "units": [unit.to_dict() for unit in self.units],
            "assets": [asdict(asset) for asset in self.assets],
            "structure": self.structure,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Document:
        return cls(
            format=value["format"],
            source_language=value["source_language"],
            target_language=value["target_language"],
            metadata=value["metadata"],
            sections=[Section(**section) for section in value["sections"]],
            units=[TranslationUnit.from_dict(unit) for unit in value["units"]],
            assets=[AssetReference(**asset) for asset in value.get("assets", [])],
            structure=value.get("structure", {}),
        )
