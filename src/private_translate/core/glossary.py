from __future__ import annotations

import csv
from collections import defaultdict
import os
from pathlib import Path
import re
import tempfile

from .batching import translation_batches
from .model import Document, TranslationUnit
from .state import write_json


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


def prepare_terminology(project: Path, document: Document, provider, language: dict) -> None:
    glossary_dir = project / "glossary"
    candidate_path = glossary_dir / "candidates.csv"
    terms_path = glossary_dir / "terms.csv"
    discovered = discover_candidates(document)
    write_json(glossary_dir / "occurrences.json", {"terms": discovered})

    rows = _read_rows(candidate_path)
    by_source = {}
    for row in rows:
        source = row.get("source", "")
        status = row.get("status", "").casefold()
        if not source or status not in {"proposed", "approved", "rejected"}:
            raise ValueError(f"Invalid terminology candidate row for {source!r}")
        key = source.casefold()
        if key in by_source:
            raise ValueError(f"Duplicate terminology candidate {source!r}")
        by_source[key] = {field: row.get(field, "") for field in FIELDS}

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
    row_by_id = {unit.id: row for unit, row in zip(units, pending)}
    context = "Propose a consistent, concise Simplified Chinese glossary translation for each supplied English term. Preserve proper names where appropriate. These are unapproved proposals for human review."
    for batch, batch_context, batch_glossary in translation_batches(units, provider, language, context, []):
        translations = provider.translate(batch, batch_context, batch_glossary, language)
        if set(translations) != {unit.id for unit in batch}:
            raise ValueError("Provider response must contain exactly one proposal per terminology candidate")
        for unit in batch:
            row_by_id[unit.id]["preferred_translation"] = translations[unit.id].strip()
            if not row_by_id[unit.id]["preferred_translation"]:
                raise ValueError(f"Provider returned an empty proposal for {unit.source_text!r}")
        _write_rows(candidate_path, rows)

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
