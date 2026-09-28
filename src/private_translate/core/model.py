from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any
import uuid


LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_hash(value: Any) -> str:
    return content_hash(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def structure_hash(unit_type: str, metadata: dict[str, Any]) -> str:
    def without_unit_references(value):
        if isinstance(value, dict):
            return {
                key: without_unit_references(item) for key, item in value.items()
                if key not in {"caption_unit_ids", "surrounding_unit_ids"}
            }
        if isinstance(value, list):
            return [without_unit_references(item) for item in value]
        return value

    stable_metadata = without_unit_references(metadata)
    return _json_hash({"type": unit_type, "metadata": stable_metadata})


@dataclass
class TranslationUnit:
    id: str
    source_parent_id: str
    section_id: str
    type: str
    source_text: str
    target_text: str | None
    translatable: bool
    source_language: str
    target_language: str
    source_hash: str
    structure_hash: str
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
        source_parent_id: str | None = None,
        structural_data: Any = None,
    ) -> TranslationUnit:
        if not LANGUAGE_TAG.fullmatch(source_language) or not LANGUAGE_TAG.fullmatch(target_language):
            raise ValueError("Language identifiers must be BCP-47 language tags")
        unit_id = str(uuid.uuid4())
        unit_metadata = metadata or {}
        return cls(
            id=unit_id,
            source_parent_id=source_parent_id or unit_id,
            section_id=section_id,
            type=type,
            source_text=source_text,
            target_text=None,
            translatable=translatable,
            source_language=source_language,
            target_language=target_language,
            source_hash=content_hash(source_text),
            structure_hash=structure_hash(type, {"metadata": unit_metadata, "structure": structural_data}),
            translation_status="pending",
            qa_status="pending",
            locator=locator,
            metadata=unit_metadata,
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
        fields = dict(value)
        fields.setdefault("source_parent_id", fields["id"])
        fields.setdefault("structure_hash", structure_hash(fields["type"], fields.get("metadata", {})))
        return cls(**fields)


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
    source_file_sha256: str | None = None
    source_fingerprint: str | None = None
    structure_fingerprint: str | None = None

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
            "source_file_sha256": self.source_file_sha256,
            "source_fingerprint": self.source_fingerprint,
            "structure_fingerprint": self.structure_fingerprint,
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
            source_file_sha256=value.get("source_file_sha256"),
            source_fingerprint=value.get("source_fingerprint"),
            structure_fingerprint=value.get("structure_fingerprint"),
        )


def update_document_fingerprints(document: Document, source_path) -> None:
    document.source_file_sha256 = content_hash_bytes(source_path.read_bytes())
    document.structure_fingerprint = _json_hash({
        "sections": [
            {"section_id": section.section_id, "title": section.title, "locator": section.locator}
            for section in document.sections
        ],
        "units": [
            {"section_id": unit.section_id, "type": unit.type, "structure_hash": unit.structure_hash, "locator": unit.locator}
            for unit in document.units
        ],
        "assets": [
            {"path": asset.path, "media_type": asset.media_type, "source_hash": asset.source_hash, "locator": asset.locator}
            for asset in document.assets
        ],
    })
    document.source_fingerprint = _json_hash({
        "source_file_sha256": document.source_file_sha256,
        "structure_fingerprint": document.structure_fingerprint,
    })
