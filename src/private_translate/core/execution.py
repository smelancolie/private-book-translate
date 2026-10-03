from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from .glossary import load_glossary
from .language import load_language_package
from .model import Document, TranslationUnit, content_hash
from .state import read_json, write_json


SCHEMA_VERSION = 1
_HASH = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
_ID = {"type": "string", "minLength": 1}
_UUID = {"type": "string", "format": "uuid"}
_MARKER = re.compile(r"⟦/?[PK]\d+⟧|\{\{pn:[A-Za-z0-9_.:-]+\}\}")


SCHEMAS = {
    "TranslationTask": {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "required": ["schema_version", "task_id", "task_type", "created_at", "project_id", "chapter_id", "owner_id", "base_document_revision", "base_chapter_digest", "source_language", "target_language", "unit_ids", "required_unit_ids", "units", "context_capsule", "glossary_slice", "proper_noun_slice", "proper_noun_occurrences", "style_rules", "translation_quality_rules", "polysemy_watchlist", "output_contract"],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": 1}, "task_id": _UUID, "task_type": {"const": "translation"},
            "created_at": {"type": "string", "format": "date-time"}, "project_id": _ID, "chapter_id": _ID, "owner_id": _ID,
            "base_document_revision": _HASH, "base_chapter_digest": _HASH,
            "source_language": _ID, "target_language": _ID,
            "unit_ids": {"type": "array", "items": _ID, "uniqueItems": True},
            "required_unit_ids": {"type": "array", "items": _ID, "uniqueItems": True},
            "units": {"type": "array", "items": {"$ref": "#/$defs/TranslationUnit"}},
            "context_capsule": {"type": "object"}, "glossary_slice": {"type": "array", "items": {"type": "object", "required": ["source", "preferred_translation"], "properties": {"source": _ID, "preferred_translation": _ID, "category": {"type": "string"}, "forbidden_translations": {"type": "string"}}, "additionalProperties": False}},
            "proper_noun_slice": {"type": "array", "items": {"$ref": "#/$defs/ProperNounEntity"}},
            "proper_noun_occurrences": {"type": "array", "items": {"$ref": "#/$defs/ProperNounOccurrence"}},
            "style_rules": {"type": "array", "items": {"type": "string"}},
            "translation_quality_rules": {"type": "array", "items": {"type": "string"}},
            "polysemy_watchlist": {"type": "array", "items": {"type": "object"}},
            "output_contract": {"type": "object"},
        },
        "$defs": {
            "TranslationUnit": {"type": "object", "additionalProperties": False, "required": ["id", "chapter_id", "source_hash", "structure_hash", "source_text", "type", "translatable", "protected_tokens"], "properties": {
                "id": _ID, "chapter_id": _ID, "source_hash": _HASH, "structure_hash": _HASH,
                "source_text": {"type": "string"}, "type": _ID, "translatable": {"type": "boolean"},
                "protected_tokens": {"type": "array", "items": {"type": "string"}},
                "source_parent_id": _ID, "ordinal": {"type": "integer", "minimum": 0}, "metadata": {"type": "object"},
            }},
            "ProperNounEntity": {"type": "object", "additionalProperties": False, "required": ["entity_id", "source_form", "normalized_source", "category", "preferred_translation", "first_rendering", "subsequent_rendering", "display_policy", "status"], "properties": {
                "entity_id": _ID, "source_form": _ID, "normalized_source": _ID,
                "category": {"enum": ["person", "place", "organization", "work_title", "historical_title", "institution", "event", "other_named_entity"]},
                "preferred_translation": _ID, "first_rendering": _ID, "subsequent_rendering": _ID,
                "display_policy": {"enum": ["translated", "preserve_original", "first_bilingual_then_translated", "original_first_with_gloss", "first_bilingual_with_note_then_translated"]},
                "repeat_original_allowed_when": {"type": "string"},
                "status": {"const": "approved"}, "notes": {"type": "string"},
                "forbidden_renderings": {"type": "array", "items": {"type": "string"}},
            }},
            "ProperNounOccurrence": {"type": "object", "additionalProperties": False, "required": ["entity_id", "chapter_id", "unit_id", "occurrence_index", "is_body_first_occurrence", "is_title_occurrence", "rendering"], "properties": {
                "entity_id": _ID, "chapter_id": _ID, "unit_id": _ID,
                "occurrence_index": {"type": "integer", "minimum": 0}, "context": {"type": "string"},
                "is_body_first_occurrence": {"type": "boolean"}, "is_title_occurrence": {"type": "boolean"},
                "rendering": _ID, "placeholder": {"type": "string", "pattern": "^\\{\\{pn:[A-Za-z0-9_.:-]+\\}\\}$"},
            }},
        },
    },
    "TranslationPatch": {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "required": ["schema_version", "patch_id", "task_id", "task_type", "chapter_id", "owner_id", "base_document_revision", "base_chapter_digest", "translations"],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": 1}, "patch_id": _UUID, "task_id": _UUID, "task_type": {"const": "translation"},
            "chapter_id": _ID, "owner_id": _ID, "base_document_revision": _HASH, "base_chapter_digest": _HASH,
            "translations": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["unit_id", "source_hash", "target_text"], "properties": {"unit_id": _ID, "source_hash": _HASH, "target_text": {"type": "string", "minLength": 1}}}},
            "worker_metadata": {"type": "object"}, "model": {"type": "string"}, "reasoning_effort": {"type": "string"},
            "started_at": {"type": "string", "format": "date-time"}, "finished_at": {"type": "string", "format": "date-time"},
        },
    },
    "ReviewTask": {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "required": ["schema_version", "task_id", "task_type", "chapter_id", "reviewer_id", "translation_owner_id", "base_chapter_digest", "target_digest", "source_units", "target_units", "rubric", "context_capsule", "required_evidence_schema"],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": 1}, "task_id": _UUID, "task_type": {"const": "chapter_review"},
            "chapter_id": _ID, "reviewer_id": _ID, "translation_owner_id": _ID,
            "base_chapter_digest": _HASH, "target_digest": _HASH,
            "source_units": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["unit_id", "source_hash", "source_text"], "properties": {"unit_id": _ID, "source_hash": _HASH, "source_text": {"type": "string"}}}},
            "target_units": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["unit_id", "target_text"], "properties": {"unit_id": _ID, "target_text": {"type": "string", "minLength": 1}}}},
            "rubric": {"type": "string"}, "context_capsule": {"type": "object"}, "required_evidence_schema": {"type": "object"},
        },
    },
    "ReviewEvidence": {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "required": ["schema_version", "task_id", "chapter_id", "reviewer_id", "translation_owner_id", "base_chapter_digest", "target_digest", "issues", "summary"],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": 1}, "task_id": _UUID, "chapter_id": _ID,
            "reviewer_id": _ID, "translation_owner_id": _ID, "base_chapter_digest": _HASH, "target_digest": _HASH,
            "issues": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["unit_id", "category", "severity", "description", "suggested_fix"], "properties": {
                "unit_id": _ID, "category": {"enum": ["fidelity", "omission", "addition", "polysemy", "terminology", "proper_name", "register", "readability", "source_syntax", "over_translation", "under_translation", "notes_tables_captions", "title_consistency"]},
                "severity": {"enum": ["critical", "major", "minor", "suggestion"]}, "description": _ID, "suggested_fix": {"type": "string"},
            }}},
            "summary": {"type": "string"},
        },
    },
}

_QUALITY_RULES = [
    "fidelity to facts, claims, uncertainty, tone, narrative stance, emotional intensity, relationships, and argument structure",
    "completeness: no invented additions, silent omissions, summaries, or explanations",
    "natural Simplified Chinese with English syntax residue removed",
    "resolve word sense and polysemy from sentence, paragraph, neighbor, chapter, glossary, and speaker context",
    "consistent proper names, terminology, titles, notes, captions, tables, and paragraph boundaries",
    "natural dialogue, narrative voice, register, rhythm, imagery, and narrative distance within source evidence",
    "reconstruct Chinese syntax and information order without changing facts or logic",
    "use Chinese typography while preserving citations, numbers, dates, links, code, and protected markers",
    "for high-risk passages, privately compare candidate A/B/C and synthesize D; return only D in target_text",
    "resolve polysemy from context and record unresolved future-check items only in patch worker_metadata, never in reader-facing text",
    "preserve each locked proper-noun placeholder exactly; Python renders it using this task's occurrence contract",
]


class PatchConflict(ValueError):
    pass


def select_translation_backend(value: int) -> str:
    if type(value) is not int:
        raise ValueError("translation_backend must be 1 (Agent) or 2 (API)")
    if value == 1:
        return "agent"
    if value == 2:
        return "api"
    raise ValueError("translation_backend must be 1 (Agent) or 2 (API)")


def _hash_json(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _unit_revision(unit: TranslationUnit) -> dict:
    return {
        "id": unit.id, "source_hash": unit.source_hash, "structure_hash": unit.structure_hash,
        "translatable": unit.translatable, "target_text": unit.target_text,
        "translation_status": unit.translation_status, "qa_status": unit.qa_status,
        "last_qa_target_hash": unit.last_qa_target_hash,
    }


def document_revision(document: Document) -> str:
    return _hash_json([_unit_revision(unit) for unit in document.units])


def chapter_digest(document: Document, chapter_id: str) -> str:
    return _hash_json([_unit_revision(unit) for unit in document.units if unit.section_id == chapter_id])


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _is_id(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _protected_tokens(text: str) -> list[str]:
    return _MARKER.findall(text)


def _validate_proper_nouns(task: dict) -> None:
    categories = {"person", "place", "organization", "work_title", "historical_title", "institution", "event", "other_named_entity"}
    policies = {"translated", "preserve_original", "first_bilingual_then_translated", "original_first_with_gloss", "first_bilingual_with_note_then_translated"}
    entities = {}
    for entity in task["proper_noun_slice"]:
        _require(isinstance(entity, dict), "Invalid proper-noun entity")
        _require(set(entity) <= {"entity_id", "source_form", "normalized_source", "category", "preferred_translation", "first_rendering", "subsequent_rendering", "display_policy", "repeat_original_allowed_when", "status", "notes", "forbidden_renderings"}, "Proper-noun entity contains unknown fields")
        for key in ("entity_id", "source_form", "normalized_source", "category", "preferred_translation", "first_rendering", "subsequent_rendering", "display_policy"):
            _require(_is_id(entity.get(key)), f"Proper-noun entity is missing {key}")
        _require(entity.get("status") == "approved", "Only approved proper-noun entities may be locked")
        _require(entity["category"] in categories and entity["display_policy"] in policies, "Proper-noun category or display policy is invalid")
        _require(isinstance(entity.get("forbidden_renderings", []), list) and all(isinstance(value, str) for value in entity.get("forbidden_renderings", [])), "Proper-noun forbidden_renderings must be strings")
        _require(entity["entity_id"] not in entities, "Duplicate proper-noun entity_id")
        entities[entity["entity_id"]] = entity
    units = {unit["id"]: unit for unit in task["units"]}
    seen_occurrences = set()
    for occurrence in task["proper_noun_occurrences"]:
        _require(isinstance(occurrence, dict), "Invalid proper-noun occurrence")
        _require(set(occurrence) <= {"entity_id", "chapter_id", "unit_id", "occurrence_index", "context", "is_body_first_occurrence", "is_title_occurrence", "rendering", "placeholder"}, "Proper-noun occurrence contains unknown fields")
        _require(occurrence.get("entity_id") in entities, "Proper-noun occurrence refers to an unknown entity")
        _require(occurrence.get("chapter_id") == task["chapter_id"], "Proper-noun occurrence belongs to another chapter")
        unit = units.get(occurrence.get("unit_id"))
        _require(unit is not None and unit["translatable"], "Proper-noun occurrence refers to an unassigned unit")
        _require(type(occurrence.get("occurrence_index")) is int and occurrence["occurrence_index"] >= 0, "Invalid proper-noun occurrence index")
        _require(isinstance(occurrence.get("is_body_first_occurrence"), bool) and isinstance(occurrence.get("is_title_occurrence"), bool), "Proper-noun occurrence flags are invalid")
        _require(_is_id(occurrence.get("rendering")), "Proper-noun occurrence has no locked rendering")
        identity = (occurrence["entity_id"], occurrence["unit_id"], occurrence["occurrence_index"])
        _require(identity not in seen_occurrences, "Duplicate proper-noun occurrence")
        seen_occurrences.add(identity)
        entity = entities[occurrence["entity_id"]]
        expected_rendering = entity["first_rendering"] if occurrence["is_body_first_occurrence"] and not occurrence["is_title_occurrence"] else entity["subsequent_rendering"]
        _require(occurrence["rendering"] == expected_rendering, "Proper-noun occurrence violates its entity rendering policy")
        placeholder = occurrence.get("placeholder")
        if placeholder is not None:
            _require(placeholder == "{{pn:" + occurrence["entity_id"] + "}}", "Proper-noun placeholder does not match its entity")


def validate_translation_task(task: dict) -> None:
    _require(isinstance(task, dict), "TranslationTask must be an object")
    _require(set(task) == set(SCHEMAS["TranslationTask"]["required"]), "TranslationTask contains unknown or missing fields")
    _require(type(task.get("schema_version")) is int and task.get("schema_version") == SCHEMA_VERSION and task.get("task_type") == "translation", "Unsupported TranslationTask schema or task type")
    _require(_valid_uuid(task.get("task_id")) and _is_id(task.get("project_id")) and _is_id(task.get("chapter_id")) and _is_id(task.get("owner_id")), "TranslationTask identity fields are required")
    _require(_valid_hash(task.get("base_document_revision")) and _valid_hash(task.get("base_chapter_digest")), "TranslationTask revision digests are invalid")
    _require(_is_id(task.get("source_language")) and _is_id(task.get("target_language")) and isinstance(task.get("created_at"), str), "TranslationTask language/timestamp fields are invalid")
    try:
        datetime.fromisoformat(task["created_at"])
    except ValueError as error:
        raise ValueError("TranslationTask created_at must be ISO 8601") from error
    for key in ("unit_ids", "required_unit_ids", "units", "glossary_slice", "proper_noun_slice", "proper_noun_occurrences", "style_rules", "translation_quality_rules", "polysemy_watchlist"):
        _require(isinstance(task.get(key), list), f"TranslationTask {key} must be an array")
    _require(all(_is_id(unit_id) for unit_id in task["unit_ids"] + task["required_unit_ids"]), "TranslationTask unit ids are invalid")
    _require(isinstance(task.get("context_capsule"), dict) and isinstance(task.get("output_contract"), dict), "TranslationTask capsules must be objects")
    _require(all(isinstance(value, str) for key in ("style_rules", "translation_quality_rules") for value in task[key]), "TranslationTask language rules must be strings")
    _require(all(isinstance(item, dict) for key in ("glossary_slice", "polysemy_watchlist") for item in task[key]), "TranslationTask glossary and watchlist items must be objects")
    _require(all(_is_id(term.get("source")) and _is_id(term.get("preferred_translation")) for term in task["glossary_slice"]), "TranslationTask glossary slice is invalid")
    _require(task["output_contract"].get("schema") == "TranslationPatch" and task["output_contract"].get("schema_version") == SCHEMA_VERSION and task["output_contract"].get("required_unit_ids") == task["required_unit_ids"], "TranslationTask output contract does not match required units")
    ids = [unit.get("id") for unit in task["units"] if isinstance(unit, dict)]
    _require(all(_is_id(unit_id) for unit_id in ids) and len(ids) == len(task["units"]) and len(ids) == len(set(ids)), "TranslationTask units contain invalid or duplicate ids")
    _require(task["unit_ids"] == ids, "TranslationTask unit_ids do not match its units")
    by_id = {unit["id"]: unit for unit in task["units"]}
    required = task["required_unit_ids"]
    _require(len(required) == len(set(required)) and set(required) <= set(ids), "TranslationTask required_unit_ids are invalid")
    _require(required == [unit["id"] for unit in task["units"] if unit["translatable"]], "TranslationTask required_unit_ids do not match its translatable units")
    for unit in task["units"]:
        _require(set(unit) <= {"id", "chapter_id", "source_hash", "structure_hash", "source_text", "type", "translatable", "protected_tokens", "source_parent_id", "ordinal", "metadata"}, "TranslationTask unit contains unknown fields")
        _require(unit.get("chapter_id") == task["chapter_id"], "TranslationTask contains a unit from another chapter")
        _require(_is_id(unit.get("id")) and _is_id(unit.get("type")), "TranslationTask unit identity is invalid")
        _require(isinstance(unit.get("source_text"), str) and isinstance(unit.get("translatable"), bool), "TranslationTask unit content is invalid")
        _require(_valid_hash(unit.get("source_hash")) and unit["source_hash"] == content_hash(unit["source_text"]), "TranslationTask source hash is stale")
        _require(_valid_hash(unit.get("structure_hash")), "TranslationTask structure hash is invalid")
        tokens = unit.get("protected_tokens")
        _require(isinstance(tokens, list) and all(isinstance(token, str) for token in tokens), "TranslationTask protected_tokens must be strings")
        _require(tokens == _protected_tokens(unit["source_text"]), "TranslationTask protected-token declaration does not match source")
    _require(all(by_id[unit_id]["translatable"] for unit_id in required), "TranslationTask requires a non-translatable unit")
    _validate_proper_nouns(task)


def _project_document(project: Path) -> tuple[dict, Document]:
    project = project.resolve(strict=True)
    manifest = read_json(project / "project.json")
    document = Document.from_dict(read_json(project / "work" / "document.json"))
    return manifest, document


def _task_path(project: Path, task_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(task_id))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("Invalid translation task id") from error
    if normalized != task_id:
        raise ValueError("Invalid translation task id")
    return project / "work" / "tasks" / "translation" / f"{task_id}.json"


def create_translation_task(
    project: Path, chapter_id: str, owner_id: str, *,
    proper_noun_slice: list[dict] | None = None,
    proper_noun_occurrences: list[dict] | None = None,
    polysemy_watchlist: list[dict] | None = None,
    unit_ids: list[str] | None = None,
    batch_context: dict | None = None,
) -> Path:
    manifest, document = _project_document(project)
    _require(_is_id(owner_id), "owner_id is required")
    _require(batch_context is None or isinstance(batch_context, dict), "batch_context must be an object")
    section = next((item for item in document.sections if item.section_id == chapter_id), None)
    chapter_units = [unit for unit in document.units if unit.section_id == chapter_id]
    _require(section is not None and chapter_units, f"Unknown or empty chapter {chapter_id!r}")
    if unit_ids is None:
        units = chapter_units
    else:
        _require(isinstance(unit_ids, list) and len(unit_ids) == len(set(unit_ids)), "Translation task unit_ids must be a unique list")
        selected_ids = set(unit_ids)
        _require(selected_ids and selected_ids <= {unit.id for unit in chapter_units}, "Translation task unit_ids must belong to the chapter")
        units = [unit for unit in chapter_units if unit.id in selected_ids]
    language = load_language_package(manifest["source_language"], manifest["target_language"])
    ids = [unit.id for unit in units]
    selected_ids = set(ids)
    occurrences = [item for item in proper_noun_occurrences or [] if item["unit_id"] in selected_ids]
    entity_ids = {item["entity_id"] for item in occurrences}
    section_index = next(index for index, item in enumerate(document.sections) if item.section_id == chapter_id)
    previous_section = document.sections[section_index - 1] if section_index else None
    next_section = document.sections[section_index + 1] if section_index + 1 < len(document.sections) else None
    previous_unit = next((unit for unit in reversed(document.units) if previous_section and unit.section_id == previous_section.section_id), None)
    next_unit = next((unit for unit in document.units if next_section and unit.section_id == next_section.section_id), None)
    chapter_source = "\n".join(unit.source_text.casefold() for unit in units if unit.translatable)
    task = {
        "schema_version": SCHEMA_VERSION, "task_id": str(uuid.uuid4()), "task_type": "translation",
        "created_at": datetime.now(timezone.utc).isoformat(), "project_id": manifest["project_id"],
        "chapter_id": chapter_id, "owner_id": owner_id,
        "base_document_revision": document_revision(document), "base_chapter_digest": chapter_digest(document, chapter_id),
        "source_language": document.source_language, "target_language": document.target_language,
        "unit_ids": ids, "required_unit_ids": [unit.id for unit in units if unit.translatable],
        "units": [
            {"id": unit.id, "chapter_id": chapter_id, "source_hash": unit.source_hash,
             "structure_hash": unit.structure_hash, "source_text": unit.source_text, "type": unit.type,
             "translatable": unit.translatable, "protected_tokens": _protected_tokens(unit.source_text),
             "source_parent_id": unit.source_parent_id, "ordinal": index, "metadata": unit.metadata}
            for index, unit in enumerate(units)
        ],
        "context_capsule": {
            "section_title": section.title,
            "previous_chapter": {"title": previous_section.title, "source": previous_unit.source_text[-120:]} if previous_section and previous_unit else None,
            "next_chapter": {"title": next_section.title, "source": next_unit.source_text[:120]} if next_section and next_unit else None,
            "batch": batch_context,
        },
        "glossary_slice": [
            term for term in load_glossary(project / "glossary" / "terms.csv")
            if term["source"].casefold() in chapter_source
        ],
        "proper_noun_slice": [item for item in proper_noun_slice or [] if item["entity_id"] in entity_ids],
        "proper_noun_occurrences": occurrences,
        "style_rules": [language["language_rules"][key] for key in ("translation", "terminology", "proper_names", "style", "typography", "polysemy")],
        "translation_quality_rules": list(_QUALITY_RULES),
        "polysemy_watchlist": polysemy_watchlist or [],
        "output_contract": {
            "schema": "TranslationPatch", "schema_version": SCHEMA_VERSION,
            "required_unit_ids": [unit.id for unit in units if unit.translatable],
            "one_translation_per_required_unit": True,
            "translation_fields": ["unit_id", "source_hash", "target_text"],
            "translation_only": True,
        },
    }
    validate_translation_task(task)
    path = _task_path(project, task["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(task, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return path


def load_translation_task(project: Path, task_id: str) -> dict:
    path = _task_path(project, task_id)
    if not path.is_file():
        raise ValueError(f"UNKNOWN_TASK: {task_id}")
    task = read_json(path)
    validate_translation_task(task)
    _require(task["task_id"] == task_id, "Task file id does not match its contents")
    return task


def _validate_patch_shape(patch: dict) -> None:
    _require(isinstance(patch, dict), "TranslationPatch must be an object")
    _require(type(patch.get("schema_version")) is int and patch.get("schema_version") == SCHEMA_VERSION and patch.get("task_type") == "translation", "Unsupported TranslationPatch schema or task type")
    allowed = {"schema_version", "patch_id", "task_id", "task_type", "chapter_id", "owner_id", "base_document_revision", "base_chapter_digest", "translations", "worker_metadata", "model", "reasoning_effort", "started_at", "finished_at"}
    _require(set(patch) <= allowed, "TranslationPatch contains unknown fields")
    for key in ("patch_id", "task_id", "chapter_id", "owner_id"):
        _require(_is_id(patch.get(key)), f"TranslationPatch {key} is required")
    _require(_valid_uuid(patch["patch_id"]) and _valid_uuid(patch["task_id"]), "TranslationPatch ids must be UUIDs")
    _require(_valid_hash(patch.get("base_document_revision")) and _valid_hash(patch.get("base_chapter_digest")), "TranslationPatch revision digests are invalid")
    _require(isinstance(patch.get("translations"), list), "TranslationPatch translations must be an array")
    if "worker_metadata" in patch:
        _require(isinstance(patch["worker_metadata"], dict), "worker_metadata must be an object")
    for key in ("model", "reasoning_effort", "started_at", "finished_at"):
        if key in patch:
            _require(isinstance(patch[key], str), f"TranslationPatch {key} must be a string")
    for item in patch["translations"]:
        _require(isinstance(item, dict), "Translation entries must be objects")
        _require(set(item) == {"unit_id", "source_hash", "target_text"}, "Translation entry contains unknown or missing fields")
        _require(_is_id(item.get("unit_id")) and _valid_hash(item.get("source_hash")), "Translation entry unit_id/source_hash is invalid")
        _require(isinstance(item.get("target_text"), str), "Translation target_text must be a string")
        _require(bool(item["target_text"].strip()), "Empty required target")


def _validate_patch_against_document(project: Path, patch: dict, document: Document) -> dict:
    _validate_patch_shape(patch)
    task = load_translation_task(project, patch["task_id"])
    _require(patch["chapter_id"] == task["chapter_id"], "Patch belongs to another chapter")
    _require(patch["owner_id"] == task["owner_id"], "Patch owner_id does not match task")
    _require(patch["base_document_revision"] == task["base_document_revision"], "Patch base_document_revision does not match task")
    if patch["base_chapter_digest"] != task["base_chapter_digest"]:
        raise PatchConflict("PATCH_CONFLICT: patch chapter digest does not match its task")
    if chapter_digest(document, task["chapter_id"]) != task["base_chapter_digest"]:
        raise PatchConflict("PATCH_CONFLICT: chapter changed since task export")
    task_units = {unit["id"]: unit for unit in task["units"]}
    current_units = {unit.id: unit for unit in document.units if unit.section_id == task["chapter_id"]}
    occurrences_by_unit = {}
    for occurrence in task["proper_noun_occurrences"]:
        occurrences_by_unit.setdefault(occurrence["unit_id"], []).append(occurrence)
    ids = [item["unit_id"] for item in patch["translations"]]
    _require(len(ids) == len(set(ids)), "Duplicate translation unit")
    for unit_id in ids:
        if unit_id not in task_units:
            if any(unit.id == unit_id and unit.section_id != task["chapter_id"] for unit in document.units):
                raise ValueError("Translation unit belongs to another chapter")
            raise ValueError(f"Unknown translation unit {unit_id!r}")
    missing = set(task["required_unit_ids"]) - set(ids)
    _require(not missing, f"Missing required units: {', '.join(sorted(missing))}")
    by_id = {item["unit_id"]: item for item in patch["translations"]}
    for unit_id in ids:
        unit, current = task_units[unit_id], current_units.get(unit_id)
        _require(current is not None, f"Translation unit {unit_id!r} no longer exists in chapter")
        _require(unit["translatable"], f"Non-translatable unit modified: {unit_id}")
        entry = by_id[unit_id]
        _require(entry["source_hash"] == unit["source_hash"] == current.source_hash == content_hash(current.source_text), f"Stale source hash for unit {unit_id}")
        expected = Counter(unit["protected_tokens"])
        observed = Counter(_protected_tokens(entry["target_text"]))
        _require(observed == expected, f"Protected marker or proper-noun placeholder corruption in unit {unit_id}")
        for occurrence in occurrences_by_unit.get(unit_id, []):
            locked = occurrence["placeholder"] if occurrence.get("placeholder") else occurrence["rendering"]
            _require(locked in entry["target_text"], f"Locked proper-noun contract violation in unit {unit_id}: expected {locked}")
            entity = next(item for item in task["proper_noun_slice"] if item["entity_id"] == occurrence["entity_id"])
            _require(not any(value and value in entry["target_text"] for value in entity.get("forbidden_renderings", [])), f"Forbidden proper-noun rendering in unit {unit_id}")
    return task


def validate_translation_patch(project: Path, patch: dict) -> dict:
    project = project.resolve(strict=True)
    document = Document.from_dict(read_json(project / "work" / "document.json"))
    return _validate_patch_against_document(project, patch, document)


def apply_translation_patch(project: Path, patch: dict) -> Document:
    project = project.resolve(strict=True)
    document_path = project / "work" / "document.json"
    document = Document.from_dict(read_json(document_path))
    task = _validate_patch_against_document(project, patch, document)
    translations = {item["unit_id"]: item["target_text"] for item in patch["translations"]}
    for occurrence in sorted(task["proper_noun_occurrences"], key=lambda item: item["occurrence_index"]):
        placeholder = occurrence.get("placeholder")
        if placeholder:
            translations[occurrence["unit_id"]] = translations[occurrence["unit_id"]].replace(placeholder, occurrence["rendering"], 1)
    by_id = {unit.id: unit for unit in document.units if unit.section_id == task["chapter_id"]}
    updated = {
        unit_id for unit_id, text in translations.items()
        if by_id[unit_id].target_text != text or by_id[unit_id].translation_status != "translated"
    }
    target_changed = any(by_id[unit_id].target_text != translations[unit_id] for unit_id in updated)
    for unit_id in updated:
        unit = by_id[unit_id]
        unit.target_text = translations[unit_id]
        unit.translation_status = "translated"
        unit.qa_status = "pending"
        unit.last_qa_target_hash = None
        unit.last_error = None
    if updated:
        write_json(document_path, document.to_dict())
        control = project / "qa" / "chapter-controls" / f"{task['chapter_id']}.json"
        if target_changed and control.exists():
            record = read_json(control)
            record["latest_round_status"] = "STALE"
            write_json(control, record)
        consistency = project / "qa" / "book-consistency.json"
        if target_changed and consistency.exists():
            report = read_json(consistency)
            report["status"] = "STALE"
            report["stale_chapters"] = sorted(set(report.get("stale_chapters", [])) | {task["chapter_id"]})
            write_json(consistency, report)
    return document


def review_target_digest(target_units: list[dict]) -> str:
    return _hash_json(target_units)


def create_review_task(
    document: Document, chapter_id: str, reviewer_id: str, translation_owner_id: str,
    rubric: str, context_capsule: dict | None = None, unit_ids: list[str] | None = None,
) -> dict:
    chapter_units = [unit for unit in document.units if unit.section_id == chapter_id and unit.translatable]
    _require(bool(chapter_units), f"Unknown or empty chapter {chapter_id!r}")
    _require(reviewer_id != translation_owner_id, "reviewer_id must differ from translation_owner_id")
    if unit_ids is None:
        units = chapter_units
    else:
        _require(isinstance(unit_ids, list) and len(unit_ids) == len(set(unit_ids)), "ReviewTask unit_ids must be a unique list")
        selected = set(unit_ids)
        _require(selected and selected <= {unit.id for unit in chapter_units}, "ReviewTask unit_ids must belong to the chapter")
        units = [unit for unit in chapter_units if unit.id in selected]
    _require(all(unit.target_text and unit.translation_status == "translated" for unit in units), "ReviewTask requires a complete current translation")
    target_units = [{"unit_id": unit.id, "target_text": unit.target_text or ""} for unit in units]
    task = {
        "schema_version": SCHEMA_VERSION, "task_id": str(uuid.uuid4()), "task_type": "chapter_review",
        "chapter_id": chapter_id, "reviewer_id": reviewer_id, "translation_owner_id": translation_owner_id,
        "base_chapter_digest": chapter_digest(document, chapter_id), "target_digest": review_target_digest(target_units),
        "source_units": [{"unit_id": unit.id, "source_hash": unit.source_hash, "source_text": unit.source_text} for unit in units],
        "target_units": target_units, "rubric": rubric,
        "context_capsule": {
            **(context_capsule or {}),
            "chapter_target_digest": review_target_digest([
                {"unit_id": unit.id, "target_text": unit.target_text or ""} for unit in chapter_units
            ]),
        },
        "required_evidence_schema": SCHEMAS["ReviewEvidence"],
    }
    validate_review_task(task)
    return task


def validate_review_task(task: dict) -> None:
    _require(isinstance(task, dict) and type(task.get("schema_version")) is int and task.get("schema_version") == SCHEMA_VERSION and task.get("task_type") == "chapter_review", "Unsupported ReviewTask schema or task type")
    for key in ("task_id", "chapter_id", "reviewer_id", "translation_owner_id"):
        _require(_is_id(task.get(key)), f"ReviewTask {key} is required")
    _require(_valid_uuid(task["task_id"]), "ReviewTask task_id must be a UUID")
    _require(task["reviewer_id"] != task["translation_owner_id"], "reviewer_id must differ from translation_owner_id")
    _require(_valid_hash(task.get("base_chapter_digest")) and _valid_hash(task.get("target_digest")), "ReviewTask digests are invalid")
    _require(isinstance(task.get("source_units"), list) and isinstance(task.get("target_units"), list), "ReviewTask units must be arrays")
    _require(isinstance(task.get("rubric"), str) and isinstance(task.get("context_capsule"), dict) and isinstance(task.get("required_evidence_schema"), dict), "ReviewTask contract fields are invalid")
    _require(set(task) == {"schema_version", "task_id", "task_type", "chapter_id", "reviewer_id", "translation_owner_id", "base_chapter_digest", "target_digest", "source_units", "target_units", "rubric", "context_capsule", "required_evidence_schema"}, "ReviewTask contains unknown or missing fields")
    _require(all(isinstance(unit, dict) for unit in task["source_units"] + task["target_units"]), "ReviewTask unit entries must be objects")
    source_ids = [unit.get("unit_id") for unit in task["source_units"] if isinstance(unit, dict)]
    target_ids = [unit.get("unit_id") for unit in task["target_units"] if isinstance(unit, dict)]
    _require(all(_is_id(unit_id) for unit_id in source_ids + target_ids) and source_ids == target_ids and len(source_ids) == len(set(source_ids)), "ReviewTask source and target units do not align")
    _require(all(isinstance(unit.get("source_text"), str) and _valid_hash(unit.get("source_hash")) and unit["source_hash"] == content_hash(unit["source_text"]) for unit in task["source_units"]), "ReviewTask source unit is invalid")
    _require(all(isinstance(unit.get("target_text"), str) and bool(unit["target_text"].strip()) for unit in task["target_units"]), "ReviewTask target unit is invalid")
    _require(review_target_digest(task["target_units"]) == task["target_digest"], "ReviewTask target digest does not match target units")


def validate_review_evidence(evidence: dict, task: dict) -> None:
    validate_review_task(task)
    _require(isinstance(evidence, dict) and type(evidence.get("schema_version")) is int and evidence.get("schema_version") == SCHEMA_VERSION, "Unsupported ReviewEvidence schema")
    _require(set(evidence) == {"schema_version", "task_id", "chapter_id", "reviewer_id", "translation_owner_id", "base_chapter_digest", "target_digest", "issues", "summary"}, "ReviewEvidence contains unknown or missing fields")
    for key in ("task_id", "chapter_id", "reviewer_id", "translation_owner_id", "base_chapter_digest", "target_digest"):
        _require(evidence.get(key) == task[key], f"ReviewEvidence {key} does not match task")
    _require(evidence["reviewer_id"] != evidence["translation_owner_id"], "reviewer_id must differ from translation_owner_id")
    _require(isinstance(evidence.get("issues"), list) and isinstance(evidence.get("summary"), str), "ReviewEvidence issues/summary are invalid")
    known_ids = {unit["unit_id"] for unit in task["source_units"]}
    categories = set(SCHEMAS["ReviewEvidence"]["properties"]["issues"]["items"]["properties"]["category"]["enum"])
    severities = {"critical", "major", "minor", "suggestion"}
    for issue in evidence["issues"]:
        _require(isinstance(issue, dict) and set(issue) == {"unit_id", "category", "severity", "description", "suggested_fix"} and _is_id(issue.get("unit_id")) and issue["unit_id"] in known_ids, "ReviewEvidence issue refers to an unknown unit or has invalid fields")
        _require(issue.get("category") in categories and issue.get("severity") in severities, "ReviewEvidence issue category or severity is invalid")
        _require(_is_id(issue.get("description")) and isinstance(issue.get("suggested_fix"), str), "ReviewEvidence issue fields are invalid")


def validate_review_evidence_current(project: Path, evidence: dict, task: dict) -> None:
    project = project.resolve(strict=True)
    validate_review_evidence(evidence, task)
    state_path = project / "work" / "task-state" / "review" / f"{task['task_id']}.json"
    if state_path.exists():
        status = read_json(state_path)["status"]
        if status in {"superseded", "rejected"}:
            raise PatchConflict(f"REVIEW_TASK_{status.upper()}: evidence cannot be accepted")
    document = Document.from_dict(read_json(project / "work" / "document.json"))
    validate_review_task_current(document, task)
    if (task["context_capsule"].get("chapter_target_digest") is None
            and chapter_digest(document, task["chapter_id"]) != task["base_chapter_digest"]):
        targets = [{"unit_id": unit.id, "target_text": unit.target_text or ""}
                   for unit in document.units if unit.section_id == task["chapter_id"] and unit.translatable]
        if {unit["unit_id"] for unit in task["source_units"]} == {unit["unit_id"] for unit in targets}:
            previous_digest = task["target_digest"]
        else:
            control = project / "qa" / "chapter-controls" / f"{task['chapter_id']}.json"
            rounds = read_json(control).get("rounds", []) if control.exists() else []
            previous_digest = next((row.get("target_digest") for row in reversed(rounds)
                                    if task["task_id"] in row.get("evidence_task_ids", [])), None)
            if previous_digest is None:
                raise PatchConflict("REVIEW_TASK_STATE_UNKNOWN: legacy batch lacks chapter target provenance")
        if previous_digest != review_target_digest(targets):
            raise PatchConflict("STALE_REVIEW_TASK: legacy chapter target changed")
    evidence_path = project / "qa" / "review-evidence" / f"{task['task_id']}.json"
    if evidence_path.exists() and read_json(evidence_path) != evidence:
        raise PatchConflict("REVIEW_EVIDENCE_CONFLICT: task already has different evidence")


def validate_review_task_current(document: Document, task: dict) -> None:
    # QA bookkeeping is not a target revision; review freshness follows content.
    current = {unit.id: unit for unit in document.units if unit.section_id == task["chapter_id"]}
    current_targets = []
    for source, target in zip(task["source_units"], task["target_units"], strict=True):
        unit = current.get(source["unit_id"])
        if unit is None or unit.source_hash != source["source_hash"]:
            raise PatchConflict("STALE_REVIEW_TASK: source unit changed")
        if unit.target_text != target["target_text"]:
            raise PatchConflict("STALE_REVIEW_EVIDENCE: current target changed")
        current_targets.append({"unit_id": unit.id, "target_text": unit.target_text})
    if review_target_digest(current_targets) != task["target_digest"]:
        raise PatchConflict("STALE_REVIEW_EVIDENCE: current target digest changed")
    full_digest = task["context_capsule"].get("chapter_target_digest")
    if full_digest is not None and full_digest != review_target_digest([
        {"unit_id": unit.id, "target_text": unit.target_text or ""}
        for unit in document.units if unit.section_id == task["chapter_id"] and unit.translatable
    ]):
        raise PatchConflict("STALE_REVIEW_TASK: chapter target changed")


def set_review_task_status(project: Path, task: dict, status: str, **details) -> dict:
    _require(status in {"pending", "completed", "superseded", "rejected"}, "Invalid review task status")
    path = project / "work" / "task-state" / "review" / f"{task['task_id']}.json"
    state = read_json(path) if path.exists() else {"task_id": task["task_id"], "history": []}
    state["status"] = status
    state["history"].append({"status": status, "at": datetime.now(timezone.utc).isoformat(), **details})
    write_json(path, state)
    return state


def load_review_task_state(project: Path, task: dict) -> dict:
    path = project / "work" / "task-state" / "review" / f"{task['task_id']}.json"
    if path.exists():
        return read_json(path)
    # Recover legacy completion only from positive records, never file absence.
    control = project / "qa" / "chapter-controls" / f"{task['chapter_id']}.json"
    rounds = read_json(control).get("rounds", []) if control.exists() else []
    referenced = any(task["task_id"] in row.get("evidence_task_ids", []) for row in rounds)
    evidence = project / "qa" / "review-evidence" / f"{task['task_id']}.json"
    if referenced or evidence.exists():
        if evidence.exists():
            validate_review_evidence(read_json(evidence), task)
        return set_review_task_status(project, task, "completed", reason="legacy completion record")
    raise PatchConflict(f"REVIEW_TASK_STATE_UNKNOWN: {task['task_id']}; explicit lifecycle required")
