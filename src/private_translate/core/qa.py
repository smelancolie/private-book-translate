from __future__ import annotations

import json
from pathlib import Path
import re

from .batching import review_batches, translation_batches
from .glossary import consistency_issues
from .model import Document, TranslationUnit, content_hash
from .providers import TranslationProvider
from .state import read_json, write_json


REVIEWS_PER_INVOCATION = 3


def _target_hash(units: list[TranslationUnit]) -> str:
    return content_hash(json.dumps([(unit.id, unit.target_text) for unit in units], ensure_ascii=False, separators=(",", ":")))


def _save_document(project: Path, document: Document) -> None:
    write_json(project / "work" / "document.json", document.to_dict())


def _review_text(units: list[TranslationUnit], field: str) -> str:
    return "\n\n".join(f"[{unit.id}] {getattr(unit, field) or ''}" for unit in units if unit.translatable)


def _review_issues(units: list[TranslationUnit], provider: TranslationProvider, language: dict, context: str, rubric: str) -> tuple[list[dict], int]:
    issues = []
    batches = review_batches(units, provider, language, context, rubric)
    for batch, batch_context in batches:
        targets = {unit.id: unit.target_text for unit in batch}
        issues.extend(provider.review(
            _review_text(batch, "source_text"), json.dumps(targets, ensure_ascii=False),
            batch_context, rubric, language,
        ))
    return issues, len(batches)


def _translate_fixes(units: list[TranslationUnit], provider: TranslationProvider, language: dict,
                     glossary: list[dict[str, str]], issues: list[dict]) -> dict[str, str]:
    translations = {}
    context = "Fix these review findings: " + json.dumps(issues, ensure_ascii=False)
    for batch, batch_context, batch_glossary in translation_batches(units, provider, language, context, glossary):
        result = provider.translate(batch, batch_context, batch_glossary, language)
        if set(result) != {unit.id for unit in batch}:
            raise ValueError("Provider response must contain exactly one translation per requested unit")
        translations.update(result)
    return translations


def _append_round(path: Path, rounds: list[dict], record: dict) -> None:
    rounds.append(record)
    write_json(path, {"rounds": rounds, "latest_round_status": record["latest_round_status"]})


def run_chapter_qa(project: Path, document: Document, provider: TranslationProvider, language: dict, glossary: list[dict[str, str]], section_ids: set[str] | None = None) -> None:
    units_by_section = {}
    for unit in document.units:
        units_by_section.setdefault(unit.section_id, []).append(unit)
    for section in document.sections:
        if section_ids is not None and section.section_id not in section_ids:
            continue
        units = units_by_section.get(section.section_id, [])
        translated = [unit for unit in units if unit.translatable]
        if not translated:
            continue
        if all(unit.qa_status == "passed" and unit.target_text is not None and unit.last_qa_target_hash == content_hash(unit.target_text) for unit in translated):
            continue
        control = project / "qa" / "chapter-controls" / f"{section.section_id}.json"
        rounds = read_json(control).get("rounds", []) if control.exists() else []
        for _ in range(REVIEWS_PER_INVOCATION):
            current_round = len(rounds) + 1
            context = json.dumps({"section_title": section.title, "glossary": glossary}, ensure_ascii=False)
            issues, review_batch_count = _review_issues(translated, provider, language, context, language["review_rubric"])
            if not issues:
                target_hashes = {unit.id: content_hash(unit.target_text or "") for unit in translated}
                record = {
                    "round": current_round,
                    "scope": "FULL_CHAPTER",
                    "issues_found": 0,
                    "fixes_applied": 0,
                    "unresolved_blocking_issues": 0,
                    "latest_round_status": "PASS",
                    "allow_next_chapter": True,
                    "target_hash": _target_hash(translated),
                    "review_batch_count": review_batch_count,
                }
                _append_round(control, rounds, record)
                for unit in translated:
                    unit.qa_status = "passed"
                    unit.last_qa_target_hash = target_hashes[unit.id]
                    unit.last_error = None
                _save_document(project, document)
                break

            known_ids = {unit.id for unit in translated}
            issue_ids = {issue["unit_id"] for issue in issues if issue.get("unit_id") in known_ids}
            if any(issue.get("unit_id") and issue["unit_id"] not in known_ids for issue in issues):
                raise ValueError("Chapter review returned an unknown translation-unit id")
            selected = [unit for unit in translated if not issue_ids or unit.id in issue_ids]
            fixes = _translate_fixes(selected, provider, language, glossary, issues)
            applied = 0
            for unit in selected:
                new_target = fixes[unit.id]
                applied += new_target != unit.target_text
                unit.target_text = new_target
                unit.qa_status = "pending"
                unit.last_qa_target_hash = None
            record = {
                "round": current_round,
                "scope": "FULL_CHAPTER",
                "issues_found": len(issues),
                "fixes_applied": applied,
                "unresolved_blocking_issues": len(issues),
                "latest_round_status": "FIXED_RECHECK_REQUIRED",
                "allow_next_chapter": False,
                "issues": issues,
            }
            _append_round(control, rounds, record)
            _save_document(project, document)
        else:
            for unit in translated:
                unit.qa_status = "failed"
            _save_document(project, document)
            raise RuntimeError(f"Chapter {section.section_id} still needs a zero-issue full recheck; resume to continue")


def run_book_consistency(project: Path, document: Document, provider: TranslationProvider, language: dict, glossary: list[dict[str, str]]) -> None:
    report_path = project / "qa" / "book-consistency.json"
    occurrence_path = project / "qa" / "consistency" / "occurrences.json"
    candidate_path = project / "qa" / "consistency" / "candidates.json"
    translated = [unit for unit in document.units if unit.translatable and unit.target_text is not None]
    translated_by_id = {unit.id: unit for unit in translated}
    current_hash = _target_hash(translated)
    glossary_hash = content_hash(json.dumps(glossary, ensure_ascii=False, sort_keys=True))
    previous = read_json(report_path) if report_path.exists() else {}
    rounds = previous.get("rounds", [])
    for _ in range(REVIEWS_PER_INVOCATION):
        issues = consistency_issues(translated, glossary)
        occurrences = []
        candidates = []
        for term in glossary:
            pattern = re.compile(re.escape(term["source"]), re.IGNORECASE)
            term_occurrences = []
            target_forms = set()
            for unit in translated:
                found = list(pattern.finditer(unit.source_text))
                if not found:
                    continue
                target = unit.target_text or ""
                expected = term["preferred_translation"]
                variants = [value for value in [expected, *term.get("forbidden_translations", "").split("|")] if value and value in target]
                if not variants:
                    variants = ["<preferred translation missing>"]
                target_forms.update(variants)
                term_occurrences.append({
                    "unit_id": unit.id, "section_id": unit.section_id, "count": len(found),
                    "source_text": unit.source_text, "target_text": target,
                })
            if not term_occurrences:
                continue
            row = {
                "term": term["source"], "preferred_translation": term["preferred_translation"],
                "source_occurrences": term_occurrences, "target_variants": sorted(target_forms),
                "section_ids": sorted({item["section_id"] for item in term_occurrences}),
                "unit_ids": [item["unit_id"] for item in term_occurrences],
            }
            occurrences.append(row)
            if sum(item["count"] for item in term_occurrences) > 1:
                candidates.append(row)
        candidate_payload = {
            "candidates": [
                {key: row[key] for key in ("term", "preferred_translation", "target_variants", "section_ids", "unit_ids")}
                for row in candidates
            ],
            "deterministic_issues": issues,
        }
        write_json(occurrence_path, {"terms": occurrences})
        write_json(candidate_path, candidate_payload)
        if previous.get("status") == "PASS" and previous.get("target_hash") == current_hash and previous.get("glossary_hash") == glossary_hash:
            return
        for candidate in candidates:
            candidate_units = [translated_by_id[unit_id] for unit_id in candidate["unit_ids"]]
            context = json.dumps({
                "candidate_term": candidate["term"],
                "preferred_translation": candidate["preferred_translation"],
                "target_variants": candidate["target_variants"],
                "occurrence_count": sum(item["count"] for item in candidate["source_occurrences"]),
            }, ensure_ascii=False)
            candidate_issues, _ = _review_issues(candidate_units, provider, language, context, language["consistency_rubric"])
            issues.extend(candidate_issues)
        current_round = len(rounds) + 1
        if not issues:
            rounds.append({"round": current_round, "issues_found": 0, "fixes_applied": 0, "status": "PASS"})
            write_json(report_path, {"status": "PASS", "target_hash": _target_hash(translated), "glossary_hash": glossary_hash, "rounds": rounds})
            return
        ids = {issue["unit_id"] for issue in issues if issue.get("unit_id")}
        known_ids = set(translated_by_id)
        if ids - known_ids:
            raise ValueError("Book consistency review returned an unknown translation-unit id")
        selected_ids = ids or {unit_id for candidate in candidates for unit_id in candidate["unit_ids"]}
        selected = [unit for unit in translated if unit.id in selected_ids]
        if not selected:
            raise ValueError("Consistency findings must identify candidate units")
        fixes = _translate_fixes(selected, provider, language, glossary, issues)
        applied = 0
        affected_sections = set()
        for unit in selected:
            applied += fixes[unit.id] != unit.target_text
            unit.target_text = fixes[unit.id]
            unit.qa_status = "pending"
            unit.last_qa_target_hash = None
            affected_sections.add(unit.section_id)
        rounds.append({"round": current_round, "issues_found": len(issues), "fixes_applied": applied, "status": "FIXED_RECHECK_REQUIRED", "issues": issues})
        write_json(report_path, {"status": "RECHECK_REQUIRED", "target_hash": _target_hash(translated), "glossary_hash": glossary_hash, "rounds": rounds})
        _save_document(project, document)
        run_chapter_qa(project, document, provider, language, glossary, affected_sections)
        translated = [unit for unit in document.units if unit.translatable and unit.target_text is not None]
        translated_by_id = {unit.id: unit for unit in translated}
    write_json(report_path, {"status": "FAILED", "target_hash": _target_hash(translated), "glossary_hash": glossary_hash, "rounds": rounds})
    raise RuntimeError("Book-level consistency QA needs another correction and review round")


def validate_qa(project: Path, document: Document) -> None:
    failed = [unit.id for unit in document.units if unit.translatable and (
        unit.translation_status != "translated" or unit.qa_status != "passed" or not unit.target_text
        or unit.last_qa_target_hash != content_hash(unit.target_text)
    )]
    if failed:
        raise ValueError(f"Translation or chapter QA is incomplete for {len(failed)} units")
    report = project / "qa" / "book-consistency.json"
    if not report.exists() or read_json(report).get("status") != "PASS":
        raise ValueError("Book-level consistency QA has not passed")
    translated = [unit for unit in document.units if unit.translatable and unit.target_text is not None]
    if read_json(report).get("target_hash") != _target_hash(translated):
        raise ValueError("Book-level consistency QA is stale for the current targets")
