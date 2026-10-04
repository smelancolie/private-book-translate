from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .batching import provider_budgets, task_input_tokens, translation_batches
from .model import Document, TranslationUnit
from .providers import ProviderCapabilities
from .state import read_json, write_json


FIELDS = ("source", "preferred_translation", "category", "status", "first_occurrence", "occurrence_count", "notes", "forbidden_translations")
_MARKERS = re.compile(r"⟦/?[PK]\d+⟧")
_NAMES = re.compile(r"\b(?!(?i:a|an|but|chapter|follow|he|how|i|it|next|she|the|this|that|then|they|we|when|where|who|you)\b)[A-Z][a-z]+(?:[-'][A-Za-z]+)?(?:\s+(?:(?:of|the|and|for|in|to|by)\s+)?[A-Z][a-z]+(?:[-'][A-Za-z]+)?){0,2}\b")
_SPECIALIST = re.compile(r"\b(?:[A-Z]{2,}(?:-[A-Z]{2,})*|[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9-]+)\b")
_WORDS = re.compile(r"[A-Za-z][A-Za-z'-]*")
_STOP = {"a", "an", "and", "as", "at", "but", "by", "chapter", "figure", "for", "he", "here", "i", "if", "in", "introduction", "it", "next", "of", "on", "or", "part", "preface", "section", "she", "table", "that", "the", "then", "there", "this", "to", "volume", "we", "when", "where", "which", "who", "why", "you"}
_TERM_ENDINGS = ("ance", "ence", "ics", "ing", "ism", "ity", "ization", "ment", "ology", "sion", "tion")


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return [{key: (value or "").strip() for key, value in row.items() if key} for row in csv.DictReader(stream)]


def _write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows({field: row.get(field, "") for field in FIELDS} for row in rows)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def load_glossary(path: Path) -> list[dict[str, str]]:
    result = []
    for row in _read_rows(path):
        status = row.get("status", "").casefold()
        legacy_approved = row.get("approved", "").casefold() in {"true", "yes", "1"}
        if status and status not in {"proposed", "approved", "rejected"}:
            raise ValueError(f"Unknown glossary status {status!r} for {row.get('source', '')!r}")
        if not (status == "approved" or legacy_approved) or not row.get("source") or not row.get("preferred_translation"):
            continue
        result.append({key: row[key] for key in ("source", "preferred_translation", "category", "forbidden_translations") if row.get(key)})
    return result


def discover_candidates(document: Document) -> list[dict]:
    found: dict[str, dict] = {}

    def record(source: str, category: str, unit: TranslationUnit, text: str, start: int, count: int = 1) -> None:
        source = " ".join(source.split())
        key = source.casefold()
        row = found.setdefault(key, {
            "source": source, "category": category, "occurrence_count": 0,
            "unit_ids": set(), "section_ids": set(), "source_occurrences": [],
            "source_occurrences_by_unit": {}, "notes": "",
        })
        if category == "specialist_term" or (category == "book_concept" and row["category"] == "proper_name"):
            row["category"] = category
        row["occurrence_count"] += count
        row["unit_ids"].add(unit.id)
        row["section_ids"].add(unit.section_id)
        existing = row["source_occurrences_by_unit"].get(unit.id)
        if existing:
            existing["count"] += count
        else:
            existing = {"unit_id": unit.id, "section_id": unit.section_id, "count": count}
            row["source_occurrences"].append(existing)
            row["source_occurrences_by_unit"][unit.id] = existing
        if not row["notes"]:
            left, right = max(0, start - 40), min(len(text), start + len(source) + 40)
            row["notes"] = " ".join(text[left:right].split())

    for unit in document.units:
        if not unit.translatable:
            continue
        text = _MARKERS.sub("", unit.source_text)
        for match in _NAMES.finditer(text):
            source = match.group(0)
            words = source.split()
            if words[0].casefold() in _STOP:
                continue
            if len(words) == 1:
                prefix = text[:match.start()].rstrip()
                if not prefix or prefix[-1] in ".!?。！？\n":
                    continue
            connectors = {"of", "the", "and", "for", "in", "to", "by"}
            category = "book_concept" if any(word.casefold() in connectors for word in words) else "proper_name"
            record(source, category, unit, text, match.start())
        for match in _SPECIALIST.finditer(text):
            record(match.group(0), "specialist_term", unit, text, match.start())
        words = list(_WORDS.finditer(text))
        for size in (2, 3):
            for index in range(len(words) - size + 1):
                group = words[index:index + size]
                source = " ".join(word.group(0) for word in group)
                if not any(group_word.group(0).casefold().endswith(_TERM_ENDINGS) for group_word in group):
                    continue
                if sum(group_word.group(0).casefold() not in _STOP for group_word in group) < 2:
                    continue
                record(source, "specialist_term", unit, text, group[0].start())

    candidates = []
    for row in found.values():
        words = row["source"].split()
        units = len(row["unit_ids"])
        if row["category"] == "proper_name":
            eligible = (len(words) > 1 and (units >= 2 or row["occurrence_count"] >= 3)) or (len(words) == 1 and units >= 2 and row["occurrence_count"] >= 3)
        elif row["category"] == "book_concept":
            eligible = units >= 2 or row["occurrence_count"] >= 3
        else:
            eligible = units >= 2 and row["occurrence_count"] >= 2
        if not eligible:
            continue
        occurrences = sorted(row["source_occurrences"], key=lambda item: (item["section_id"], item["unit_id"]))
        candidates.append({
            "source": row["source"], "category": row["category"],
            "first_occurrence": f"{occurrences[0]['section_id']}:{occurrences[0]['unit_id']}",
            "occurrence_count": str(row["occurrence_count"]), "notes": row["notes"],
            "unit_ids": sorted(row["unit_ids"]), "section_ids": sorted(row["section_ids"]),
            "source_occurrences": occurrences,
        })
    return sorted(candidates, key=lambda row: (row["category"], row["source"].casefold()))


_TERMINOLOGY_OWNER_ID = "agent:terminology-owner"
_TERMINOLOGY_RESULT_SCHEMA = {
    "type": "object",
    "required": ["schema_version", "task_id", "owner_id", "proposals"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": 1},
        "task_id": {"type": "string", "format": "uuid"},
        "owner_id": {"const": _TERMINOLOGY_OWNER_ID},
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source", "preferred_translation"],
                "additionalProperties": False,
                "properties": {
                    "source": {"type": "string", "minLength": 1},
                    "preferred_translation": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}
_MAX_CONTEXT_OCCURRENCES = 3


def _candidate_rows_by_source(path: Path) -> dict[str, dict[str, str]]:
    result = {}
    for row in _read_rows(path):
        source = row.get("source", "")
        status = row.get("status", "").casefold()
        if not source or status not in {"proposed", "approved", "rejected"}:
            raise ValueError(f"Invalid terminology candidate row for {source!r}")
        key = source.casefold()
        if key in result:
            raise ValueError(f"Duplicate terminology candidate {source!r}")
        result[key] = {field: row.get(field, "") for field in FIELDS}
    return result


def _terminology_task_path(project: Path, task_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(task_id))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("Invalid terminology task id") from error
    if normalized != task_id:
        raise ValueError("Invalid terminology task id")
    return project / "work" / "tasks" / "terminology" / f"{task_id}.json"


def _terminology_candidate_snapshot(
    candidate: dict | None, row: dict[str, str],
) -> dict:
    discovery = (
        {
            "category": candidate["category"],
            "first_occurrence": candidate["first_occurrence"],
            "occurrence_count": candidate["occurrence_count"],
            "notes": candidate["notes"],
            "was_discovered": True,
            "context_occurrences": candidate["source_occurrences"][:_MAX_CONTEXT_OCCURRENCES],
        }
        if candidate is not None else {
            "category": row["category"], "first_occurrence": row["first_occurrence"],
            "occurrence_count": row["occurrence_count"], "notes": row["notes"],
            "was_discovered": False, "context_occurrences": [],
        }
    )
    return {
        "source": candidate["source"] if candidate is not None else row["source"],
        "candidate_row": {field: row.get(field, "") for field in FIELDS},
        "discovery": discovery,
    }


def _terminology_task(
    project: Path, document: Document, language: dict, source_keys: list[str],
    snapshots: list[dict], batch_context: str, batch_index: int, batch_count: int,
) -> dict:
    context = json.loads(batch_context)
    manifest = read_json(project / "project.json")
    return {
        "schema_version": 1,
        "task_id": str(uuid.uuid4()),
        "task_type": "terminology",
        "owner_id": _TERMINOLOGY_OWNER_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_id": manifest["project_id"],
        "source_language": document.source_language,
        "target_language": document.target_language,
        "source_keys": source_keys,
        "candidate_snapshots": snapshots,
        "context_capsule": {
            "translation_batch": context,
            "candidates": [
                {
                    "source": snapshot["source"],
                    "category": snapshot["discovery"]["category"],
                    "first_occurrence": snapshot["discovery"]["first_occurrence"],
                    "occurrence_count": snapshot["discovery"]["occurrence_count"],
                    "notes": snapshot["candidate_row"]["notes"],
                    "source_occurrences": snapshot["discovery"]["context_occurrences"],
                }
                for snapshot in snapshots
            ],
            "batch": {"index": batch_index, "count": batch_count},
        },
        "rubric": (
            "Propose one consistent, concise Simplified Chinese glossary translation for every supplied English term. "
            "Use the local occurrence context, preserve proper names where appropriate, and return the exact source key. "
            "These are unapproved proposals for human review; do not imply approval.\n\n"
            + language["language_rules"]["terminology"]
        ),
        "required_result_schema": _TERMINOLOGY_RESULT_SCHEMA,
    }


def _validate_terminology_task_shape(task: object) -> dict:
    if not isinstance(task, dict):
        raise ValueError("Invalid terminology task")
    task_id = task.get("task_id")
    _terminology_task_path(Path("."), task_id)
    if (
        type(task.get("schema_version")) is not int
        or task["schema_version"] != 1
        or task.get("task_type") != "terminology"
        or task.get("owner_id") != _TERMINOLOGY_OWNER_ID
        or task.get("required_result_schema") != _TERMINOLOGY_RESULT_SCHEMA
    ):
        raise ValueError("Invalid terminology task")
    source_keys = task.get("source_keys")
    snapshots = task.get("candidate_snapshots")
    if (
        not isinstance(source_keys, list)
        or not source_keys
        or not all(isinstance(source, str) and source.strip() for source in source_keys)
        or len({source.casefold() for source in source_keys}) != len(source_keys)
        or not isinstance(snapshots, list)
        or not all(isinstance(snapshot, dict) for snapshot in snapshots)
        or [snapshot.get("source") for snapshot in snapshots if isinstance(snapshot, dict)] != source_keys
    ):
        raise ValueError("Invalid terminology task candidates")
    return task


def validate_terminology_task_current(project: Path, task: dict) -> None:
    task = _validate_terminology_task_shape(task)
    project = project.resolve(strict=True)
    task_path = _terminology_task_path(project, task["task_id"])
    if not task_path.is_file():
        raise ValueError(f"UNKNOWN_TERMINOLOGY_TASK: {task['task_id']}")
    if read_json(task_path) != task:
        raise ValueError("TERMINOLOGY_TASK_CHANGED: saved task differs from submitted task")

    manifest = read_json(project / "project.json")
    document = Document.from_dict(read_json(project / "work" / "document.json"))
    if (
        task.get("project_id") != manifest["project_id"]
        or task.get("source_language") != document.source_language
        or task.get("target_language") != document.target_language
    ):
        raise ValueError("STALE_TERMINOLOGY_TASK: project or language changed")

    candidate_rows = _candidate_rows_by_source(project / "glossary" / "candidates.csv")
    discovered = {row["source"].casefold(): row for row in discover_candidates(document)}
    for snapshot in task["candidate_snapshots"]:
        source = snapshot["source"]
        key = source.casefold()
        current_row = candidate_rows.get(key)
        current_discovery = discovered.get(key)
        if current_row != snapshot.get("candidate_row"):
            raise ValueError(f"STALE_TERMINOLOGY_TASK: candidate row changed for {source!r}")
        if current_discovery is not None and current_discovery["source"] != source:
            raise ValueError(f"STALE_TERMINOLOGY_TASK: candidate source changed for {source!r}")
        current_snapshot = _terminology_candidate_snapshot(current_discovery, current_row)
        if current_snapshot != snapshot:
            raise ValueError(f"STALE_TERMINOLOGY_TASK: source context changed for {source!r}")
        if current_row["status"] != "proposed" or current_row["preferred_translation"]:
            raise ValueError(f"STALE_TERMINOLOGY_TASK: candidate is no longer pending for {source!r}")


def validate_terminology_result(project: Path, result: dict, task: dict) -> list[dict[str, str]]:
    task = _validate_terminology_task_shape(task)
    validate_terminology_task_current(project, task)
    if (
        not isinstance(result, dict)
        or set(result) != {"schema_version", "task_id", "owner_id", "proposals"}
        or type(result.get("schema_version")) is not int
        or result["schema_version"] != 1
        or result.get("task_id") != task["task_id"]
        or result.get("owner_id") != task["owner_id"]
        or not isinstance(result.get("proposals"), list)
    ):
        raise ValueError("Invalid terminology result identity or shape")

    expected = {source.casefold(): source for source in task["source_keys"]}
    proposals = {}
    for proposal in result["proposals"]:
        if not isinstance(proposal, dict) or set(proposal) != {"source", "preferred_translation"}:
            raise ValueError("Invalid terminology proposal")
        source = proposal.get("source")
        translation = proposal.get("preferred_translation")
        if not isinstance(source, str) or not source.strip() or not isinstance(translation, str) or not translation.strip():
            raise ValueError("Terminology proposals require non-empty source and preferred_translation")
        key = source.casefold()
        if key not in expected:
            raise ValueError(f"Unknown terminology source {source!r}")
        if key in proposals:
            raise ValueError(f"Duplicate terminology proposal for {source!r}")
        proposals[key] = translation.strip()
    if set(proposals) != set(expected):
        missing = [source for key, source in expected.items() if key not in proposals]
        raise ValueError(f"Missing terminology proposals: {', '.join(missing)}")
    return [
        {"source": source, "preferred_translation": proposals[key]}
        for key, source in expected.items()
    ]


def _pending_terminology_task(project: Path, pending_keys: set[str]) -> dict | None:
    task_dir = project / "work" / "tasks" / "terminology"
    if not task_dir.exists():
        return None
    for path in sorted(task_dir.glob("*.json")):
        task = _validate_terminology_task_shape(read_json(path))
        if any(source.casefold() in pending_keys for source in task["source_keys"]):
            validate_terminology_task_current(project, task)
            return task
    return None


def _fit_terminology_batches(
    units: list[TranslationUnit], capabilities: ProviderCapabilities, language: dict,
    context: str, document: Document, project: Path,
    candidates_by_source: dict[str, dict], rows_by_source: dict[str, dict[str, str]],
) -> list[tuple[list[TranslationUnit], str, list[dict[str, str]]]]:
    budget = provider_budgets(capabilities, language)["translation_batch_target_tokens"]

    def fit(group: list[TranslationUnit]) -> list[tuple[list[TranslationUnit], str, list[dict[str, str]]]]:
        packed = translation_batches(group, capabilities, language, context, [])
        fitted = []
        for batch, batch_context, batch_glossary in packed:
            source_keys = [unit.source_text for unit in batch]
            snapshots = [
                _terminology_candidate_snapshot(
                    candidates_by_source.get(unit.source_text.casefold()),
                    rows_by_source[unit.source_text.casefold()],
                )
                for unit in batch
            ]
            draft = _terminology_task(
                project, document, language, source_keys, snapshots, batch_context,
                len(units) - 1, len(units),
            )
            if task_input_tokens(draft) <= budget:
                fitted.append((batch, batch_context, batch_glossary))
            elif len(batch) > 1:
                middle = len(batch) // 2
                fitted.extend(fit(batch[:middle]))
                fitted.extend(fit(batch[middle:]))
            else:
                raise ValueError(
                    f"Terminology task for {batch[0].source_text!r} exceeds the configured input budget"
                )
        return fitted

    return fit(units)


def prepare_terminology(
    project: Path, document: Document, provider, language: dict, *,
    executor=None, model: str | None = None, capabilities: ProviderCapabilities | None = None,
) -> None:
    if executor is not None and (model is None or capabilities is None):
        raise ValueError("Host terminology execution requires model and capabilities")
    if executor is None and provider is None:
        raise ValueError("API terminology execution requires a provider")
    active_capabilities = capabilities if capabilities is not None else provider.capabilities
    glossary_dir = project / "glossary"
    candidate_path = glossary_dir / "candidates.csv"
    terms_path = glossary_dir / "terms.csv"
    discovered = discover_candidates(document)
    write_json(glossary_dir / "occurrences.json", {"terms": discovered})

    by_source = _candidate_rows_by_source(candidate_path)

    for candidate in discovered:
        key = candidate["source"].casefold()
        row = by_source.get(key)
        if row is None:
            row = {
                **{field: "" for field in FIELDS}, "source": candidate["source"],
                "category": candidate["category"], "status": "proposed",
            }
            by_source[key] = row
        row["category"] = row["category"] or candidate["category"]
        row["first_occurrence"] = candidate["first_occurrence"]
        row["occurrence_count"] = candidate["occurrence_count"]
        row["notes"] = row["notes"] or candidate["notes"]

    rows = sorted(by_source.values(), key=lambda row: row["source"].casefold())
    _write_rows(candidate_path, rows)
    pending = [row for row in rows if row["status"] == "proposed" and not row["preferred_translation"]]
    units = [TranslationUnit.create(
        section_id="terminology", type="terminology_candidate", source_text=row["source"],
        translatable=True, source_language=document.source_language, target_language=document.target_language,
        locator={"candidate": row["source"]}, metadata={"category": row["category"]},
    ) for row in pending]
    context = "Propose a consistent, concise Simplified Chinese glossary translation for each supplied English term. Preserve proper names where appropriate. These are unapproved proposals for human review."
    candidate_by_source = {candidate["source"].casefold(): candidate for candidate in discovered}
    row_by_source = {row["source"].casefold(): row for row in rows}
    resume_task = None
    if executor is None:
        batches = translation_batches(units, active_capabilities, language, context, [])
    else:
        resume_task = _pending_terminology_task(project, {unit.source_text.casefold() for unit in units})
        resume_keys = {source.casefold() for source in resume_task["source_keys"]} if resume_task else set()
        remaining = [unit for unit in units if unit.source_text.casefold() not in resume_keys]
        batches = _fit_terminology_batches(
            remaining, active_capabilities, language, context, document, project,
            candidate_by_source, row_by_source,
        )
        if resume_task is not None:
            by_source_unit = {unit.source_text.casefold(): unit for unit in units}
            batches.insert(0, (
                [by_source_unit[source.casefold()] for source in resume_task["source_keys"]],
                json.dumps(resume_task["context_capsule"]["translation_batch"], ensure_ascii=False), [],
            ))
    for batch_index, (batch, batch_context, batch_glossary) in enumerate(batches):
        source_keys = [unit.source_text for unit in batch]
        if executor is None:
            translations = provider.translate(batch, batch_context, batch_glossary, language)
            if not isinstance(translations, dict) or set(translations) != {unit.id for unit in batch}:
                raise ValueError("Provider response must contain exactly one proposal per terminology candidate")
            proposals = []
            for unit in batch:
                translation = translations[unit.id]
                if not isinstance(translation, str) or not translation.strip():
                    raise ValueError(f"Provider returned an empty proposal for {unit.source_text!r}")
                proposals.append({"source": unit.source_text, "preferred_translation": translation.strip()})
        else:
            snapshots = [
                _terminology_candidate_snapshot(
                    candidate_by_source.get(unit.source_text.casefold()),
                    row_by_source[unit.source_text.casefold()],
                )
                for unit in batch
            ]
            existing = resume_task if batch_index == 0 else None
            task = existing or _terminology_task(
                project, document, language, source_keys, snapshots, batch_context,
                batch_index, len(batches),
            )
            if existing is None:
                task_path = _terminology_task_path(project, task["task_id"])
                task_path.parent.mkdir(parents=True, exist_ok=True)
                with task_path.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(task, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
            result = executor.execute(task, model=model)
            proposals = validate_terminology_result(project, result, task)

        current_rows = _candidate_rows_by_source(candidate_path)
        for proposal in proposals:
            key = proposal["source"].casefold()
            row = current_rows[key]
            if row["status"] != "proposed" or row["preferred_translation"]:
                raise ValueError(f"Terminology candidate is no longer pending: {proposal['source']!r}")
            row["preferred_translation"] = proposal["preferred_translation"]
        _write_rows(candidate_path, sorted(current_rows.values(), key=lambda row: row["source"].casefold()))

    terms = _read_rows(terms_path)
    term_by_source = {}
    for row in terms:
        if not row.get("source"):
            continue
        normalized = {field: row.get(field, "") for field in FIELDS}
        normalized["status"] = normalized["status"] or (
            "approved" if row.get("approved", "").casefold() in {"true", "yes", "1"} else "proposed"
        )
        term_by_source[row["source"].casefold()] = normalized
    for row in rows:
        if row["status"] != "approved":
            continue
        if not row["preferred_translation"]:
            raise ValueError(f"Approved terminology candidate {row['source']!r} needs a preferred translation")
        existing = term_by_source.get(row["source"].casefold(), {})
        term_by_source[row["source"].casefold()] = {
            field: row.get(field) or existing.get(field, "") for field in FIELDS
        }
    _write_rows(terms_path, sorted(term_by_source.values(), key=lambda row: row.get("source", "").casefold()))


def consistency_issues(units: list[TranslationUnit], glossary: list[dict[str, str]]) -> list[dict[str, str]]:
    issues = []
    for unit in units:
        if not unit.translatable or unit.target_text is None:
            continue
        for term in glossary:
            source = term["source"]
            target = term["preferred_translation"]
            if not re.search(re.escape(source), unit.source_text, flags=re.IGNORECASE):
                continue
            forbidden = [value.strip() for value in term.get("forbidden_translations", "").split("|") if value.strip()]
            if target not in unit.target_text or any(value in unit.target_text for value in forbidden):
                issues.append({
                    "unit_id": unit.id,
                    "severity": "P1",
                    "description": f"Glossary consistency: translate {source!r} as {target!r}.",
                })
    return issues
